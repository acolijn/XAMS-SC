"""BaseService — lifecycle, reconnect, heartbeat. See DESIGN.md §6.

Every device service inherits this and implements two methods:

    verify_identity()  ask the device who it is; return False to refuse to start
    read()             return a list of Measurements, timestamped at the read

Everything else — the loop, the backoff, the heartbeat, the single-instance
lock, the averaging window — is here, once.

Five rules from §6 that this class exists to enforce:

  * Startup is read-only. No setpoint written, no channel enabled, no state
    restored. A restart must be invisible to the hardware.
  * A busy device is a clean failure, not a stack trace.
  * One instance only.
  * The service never exits because a device disappeared; it exits only when
    it cannot be identified at startup.
  * A stale value is never left standing as if fresh.
"""

from __future__ import annotations

import logging
import signal
import statistics
import sys
import threading
import time
from pathlib import Path

import json

from .bus import TOPIC_ACK, TOPIC_RELOAD, Bus
from .config import Config
from .model import Measurement, Quality, ServiceState, utcnow

log = logging.getLogger(__name__)


class SingleInstance:
    """A lock file preventing a second copy of a service (§6.1).

    Every device admits only one process, so two copies of a driver is not a
    tidiness problem — it is two processes fighting over an instrument.
    """

    def __init__(self, name: str, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{name}.lock"
        self._fh = None

    def acquire(self) -> bool:
        try:
            # 'x' fails if the file exists; on Windows an open file also
            # cannot be removed by another process, which is what we want.
            self._fh = self.path.open("x")
            self._fh.write(str(threading.get_ident()))
            self._fh.flush()
            return True
        except FileExistsError:
            # A stale lock from a hard kill should not block a restart
            # forever, so try to claim it.
            try:
                self.path.unlink()
                self._fh = self.path.open("x")
                return True
            except Exception:
                return False
        except Exception:
            return False

    def release(self) -> None:
        try:
            if self._fh:
                self._fh.close()
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


class BaseService:
    """The loop every device service runs.

    Sampling and logging rates are separate decisions (§9.1): the hardware is
    read at `interval_s`, and the mean of those samples is published every
    `log_interval_s`. The averaging is not merely data reduction — a mean of
    ten readings is less noisy than any single one, so what is stored is
    better than what is discarded.
    """

    name = "base"

    def __init__(
        self,
        config: Config,
        bus: Bus,
        interval_s: float = 1.0,
        log_interval_s: float = 10.0,
        simulate: bool = False,
        lock_dir: Path | None = None,
    ):
        self.config = config
        self.bus = bus
        self.interval_s = interval_s
        self.log_interval_s = log_interval_s
        self.simulate = simulate

        self._stop = threading.Event()
        self._lock = SingleInstance(self.name, lock_dir or Path("logs"))
        self._window: dict[str, list[Measurement]] = {}
        self._backoff = 1.0
        self._state = ServiceState.STARTING
        self._broker_down_since: float | None = None
        self._next_broker_warning = 0.0
        self._failures = 0

    # ------------------------------------------------------- to be implemented

    def verify_identity(self) -> bool:
        """Ask the device who it is. Return False to refuse to start (§6.2).

        Simulation mode always passes: there is no device to misidentify.
        """
        return True

    def read(self) -> list[Measurement]:
        """One acquisition cycle. Raise on failure; the loop handles backoff."""
        raise NotImplementedError

    def on_reload(self, config: Config) -> bool:
        """Apply a new configuration without restarting. See DESIGN.md §12.

        Return True if this service has fully applied it, False if it needs a
        restart instead. The default is False: a device service holds open
        DAQmx tasks and serial ports built from the channel map, and rebuilding
        those under a running acquisition is not something to do implicitly.

        **Returning False is not a failure.** It is how `xams-ctl reload`
        learns to tell the operator which services still need a restart,
        rather than reporting a success it did not achieve.
        """
        return False

    def _handle_reload(self, topic: str, payload: str) -> None:
        from .config import ConfigError
        from .config import load as load_config

        try:
            config = load_config()
        except ConfigError as exc:
            log.error("reload refused, configuration is invalid: %s", exc)
            self.bus.publish_raw(
                f"{TOPIC_ACK}/{self.name}/reload",
                json.dumps({"service": self.name, "applied": False,
                            "error": str(exc)}))
            return

        try:
            applied = self.on_reload(config)
        except Exception as exc:
            log.exception("reload raised")
            applied = False
            self.bus.publish_raw(
                f"{TOPIC_ACK}/{self.name}/reload",
                json.dumps({"service": self.name, "applied": False,
                            "error": str(exc)}))
            return

        if applied:
            self.config = config
            log.info("configuration reloaded (config %s)", config.config_hash)
        else:
            log.info("configuration changed (config %s) but this service needs "
                     "a restart to apply it", config.config_hash)
        self.bus.publish_raw(
            f"{TOPIC_ACK}/{self.name}/reload",
            json.dumps({"service": self.name, "applied": applied,
                        "config": config.config_hash}))

    def reconnect(self) -> bool:
        """Re-establish the link after a failure, and RE-VERIFY IDENTITY.

        Called by the loop after repeated read failures. The default does
        nothing, which suits a device that cannot be unplugged.

        For serial instruments this must re-run identity verification, not
        merely reopen the port (§6.2 rule 6). A reconnect after a USB glitch
        is precisely where a swapped cable would otherwise slip through: the
        port reopens, readings resume, and they are the wrong instrument's.

        Return False if the device could not be identified. The service keeps
        retrying rather than exiting — it exits only when it cannot be
        identified AT STARTUP (§6.1).
        """
        return True

    def close(self) -> None:
        """Release hardware. Called on shutdown, always."""

    # ------------------------------------------------------------------ helpers

    def set_state(self, state: ServiceState) -> None:
        if state != self._state:
            log.info("state: %s -> %s", self._state.value, state.value)
            self._state = state
        self.bus.publish_state(self.name, state)

    def _accumulate(self, batch: list[Measurement]) -> None:
        for m in batch:
            self._window.setdefault(m.channel, []).append(m)

    def _emit_window(self) -> None:
        """Publish the mean over the log interval, plus min/max where asked.

        Quality is not averaged. If any sample in the window was bad, the
        aggregate says so: a mean that quietly includes an error reading is
        exactly the frozen-plausible-value failure this design refuses.
        """
        for channel, samples in self._window.items():
            if not samples:
                continue
            good = [s for s in samples if s.quality == Quality.OK and s.value is not None]
            last = samples[-1]

            if not good:
                self.bus.publish_measurement(
                    Measurement(
                        t=last.t, channel=channel, value=None, unit=last.unit,
                        quality=last.quality if last.quality != Quality.OK else Quality.ERROR,
                    )
                )
                continue

            values = [s.value for s in good]
            raws = [s.raw for s in good if s.raw is not None]
            quality = Quality.OK if len(good) == len(samples) else Quality.STALE

            self.bus.publish_measurement(
                Measurement(
                    t=good[-1].t,
                    channel=channel,
                    value=statistics.fmean(values),
                    unit=good[-1].unit,
                    raw=statistics.fmean(raws) if raws else None,
                    quality=quality,
                )
            )
        self._window.clear()

    def _publish_error(self, reason: str) -> None:
        """Mark every channel of this service as unreadable.

        Fail loudly, never silently (principle 4): a channel that cannot be
        read is published with quality=error, not left at its last value.
        """
        now = utcnow()
        for ch in self.config.channels_for(self.name):
            self.bus.publish_measurement(
                Measurement(t=now, channel=ch.name, value=None, unit=ch.unit,
                            quality=Quality.ERROR)
            )
        log.error("read failed: %s", reason)

    # --------------------------------------------------------------------- run

    def run(self) -> int:
        """The service loop. Returns a process exit code."""
        if not self._lock.acquire():
            log.critical(
                "FATAL: another %s service is already running (lock: %s). Refusing to start.",
                self.name, self._lock.path,
            )
            return 1

        self._install_signal_handlers()
        self.bus.subscribe(TOPIC_RELOAD, self._handle_reload)
        self.bus.connect()
        self.set_state(ServiceState.STARTING)

        try:
            if not self.verify_identity():
                log.critical(
                    "FATAL: %s could not be identified. Refusing to start. "
                    "Nothing is written to an instrument that has not said who it is.",
                    self.name,
                )
                self.set_state(ServiceState.STOPPED)
                return 2

            log.info(
                "%s started (simulate=%s, read %.1fs, log %.1fs, config %s)",
                self.name, self.simulate, self.interval_s, self.log_interval_s,
                self.config.config_hash,
            )
            self.set_state(ServiceState.RUNNING)
            self._loop()
            return 0
        finally:
            self.set_state(ServiceState.STOPPED)
            try:
                self.close()
            finally:
                self.bus.disconnect()
                self._lock.release()
                log.info("%s stopped", self.name)

    def _warn_if_broker_down(self) -> None:
        """Say so, repeatedly, while the broker is unreachable.

        Acquisition continues regardless (§6.4) — but an outage that produces
        no log line is indistinguishable from a healthy system that happens to
        be storing nothing, and that is the silent failure principle 4 exists
        to forbid.
        """
        if self.bus.connected:
            if self._broker_down_since is not None:
                outage = time.monotonic() - self._broker_down_since
                log.info("broker back after %.0fs; buffered data republished", outage)
                self._broker_down_since = None
            return

        now = time.monotonic()
        if self._broker_down_since is None:
            self._broker_down_since = now
            log.warning(
                "broker unreachable: still reading hardware and buffering to "
                "memory, but nothing is being stored"
            )
            self._next_broker_warning = now + 30.0
            return
        if now >= self._next_broker_warning:
            log.warning(
                "broker still unreachable after %.0fs; nothing is being stored",
                now - self._broker_down_since,
            )
            self._next_broker_warning = now + 30.0

    def _loop(self) -> None:
        next_read = time.monotonic()
        next_log = time.monotonic() + self.log_interval_s

        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_read:
                try:
                    self._accumulate(self.read())
                    if self._state is not ServiceState.RUNNING:
                        log.info("device recovered")
                        self.set_state(ServiceState.RUNNING)
                    self._backoff = 1.0
                    self._failures = 0
                    next_read = now + self.interval_s
                except Exception as exc:
                    # The service never exits because a device disappeared:
                    # publish the failure, back off, and keep trying (§6.1).
                    self._publish_error(str(exc))
                    self.set_state(ServiceState.DEGRADED)
                    self._failures += 1

                    # Retrying a read on a link that is gone will never
                    # succeed. After a couple of failures, try to rebuild the
                    # connection — which for a serial device means asking it
                    # again who it is.
                    if self._failures >= 2:
                        try:
                            if self.reconnect():
                                log.info("reconnected and identity re-verified")
                                self._failures = 0
                                self._backoff = 1.0
                            else:
                                log.warning("reconnect failed; will retry")
                        except Exception:
                            log.exception("reconnect raised")

                    next_read = now + self._backoff
                    self._backoff = min(self._backoff * 2, 30.0)

            if time.monotonic() >= next_log:
                self._emit_window()
                self.bus.publish_heartbeat(self.name)
                self._warn_if_broker_down()
                next_log += self.log_interval_s

            self._stop.wait(min(0.2, self.interval_s))

    def stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            log.info("signal %s received; shutting down", signum)
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass


def setup_logging(service: str, level: str = "INFO", log_dir: Path | None = None) -> None:
    """Rotating file plus stdout. Every line carries the service name (§12)."""
    from logging.handlers import RotatingFileHandler

    d = log_dir or Path("logs")
    d.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        f"%(asctime)s %(levelname)-8s [{service}] %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    fh = RotatingFileHandler(d / f"{service}.log", maxBytes=10_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
