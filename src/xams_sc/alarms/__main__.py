"""Alarm service: python -m xams_sc.alarms

Runs the alarm engine, the notifier and the flight recorder in one process.
They belong together — the recorder must dump at the instant the engine raises
an alarm, and routing that through the bus would add a delay to the one moment
where the data matters most.

Runs LOCALLY and never depends on the Nikhef VM (§12). When the network is down
is exactly when the operator needs to see what is happening.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

import yaml

from ..bus import Bus
from ..config import CONFIG_DIR, ConfigError, load
from ..model import ServiceState
from ..service import SingleInstance, setup_logging
from .engine import AlarmEngine
from .flight_recorder import FlightRecorder
from .notify import Notifier

log = logging.getLogger(__name__)


def read_secrets() -> dict:
    path = CONFIG_DIR / "secrets.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("could not read secrets.yaml")
        return {}


def recipients_provider():
    """Read recipients.yaml at send time, not at startup.

    It is edited from the web UI and applied without restarting anything
    (§4.4), so capturing the list once would mean a change never took effect
    until the next restart — which is the opposite of what that design says.
    """
    path = CONFIG_DIR / "recipients.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data.get("recipients") or []
    except Exception:
        log.exception("could not read recipients.yaml")
        return []


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m xams_sc.alarms")
    p.add_argument("--broker", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--no-notify", action="store_true",
                   help="evaluate and publish, but send nothing")
    args = p.parse_args(argv)

    setup_logging("alarms", args.log_level)

    lock = SingleInstance("alarms", Path("logs"))
    if not lock.acquire():
        log.critical("FATAL: another alarms process is already running "
                     "(lock: %s). Refusing to start — two engines would "
                     "double every notification.", lock.path)
        return 1

    try:
        config = load()
    except ConfigError as exc:
        print(f"FATAL: configuration is invalid: {exc}", file=sys.stderr)
        lock.release()
        return 2

    bus = Bus(client_id="alarms", host=args.broker, port=args.port)

    notifier = None
    if args.no_notify:
        log.warning("--no-notify: alarms will be evaluated and published but "
                    "NOT delivered to anyone")
    else:
        notifier = Notifier(read_secrets(), recipients_provider)
        enabled = [r for r in recipients_provider() if r.get("enabled")]
        if not enabled:
            log.error("NO ENABLED RECIPIENTS — alarms would reach nobody. "
                      "Add one in recipients.yaml (§4.4).")
        else:
            log.info("%d recipient(s) enabled", len(enabled))

    recorder = FlightRecorder(bus, directory=Path("data/events"))
    recorder.start()

    engine = AlarmEngine(
        bus, config, notifier=notifier,
        # The dump happens at the moment the alarm is raised, so the buffer
        # still holds the approach to it (§9.1).
        on_alarm=lambda channel, severity, threshold: recorder.dump(
            f"{severity}_{threshold or 'quality'}", channel))
    engine.start()

    def handle_reload(topic: str, payload: str) -> None:
        try:
            new_config = load()
        except ConfigError as exc:
            log.error("reload refused, configuration is invalid: %s", exc)
            bus.publish_raw("xams/ack/alarms/reload",
                            json.dumps({"service": "alarms", "applied": False,
                                        "error": str(exc)}))
            return
        engine.reload(new_config)
        log.info("thresholds reloaded (config %s)", new_config.config_hash)
        bus.publish_raw("xams/ack/alarms/reload",
                        json.dumps({"service": "alarms", "applied": True,
                                    "config": new_config.config_hash}))

    bus.subscribe("xams/cmd/all/reload", handle_reload)

    bus.connect()
    bus.publish_state("alarms", ServiceState.RUNNING)
    log.info("alarm engine running (stale after %.0fs, repeat every %.0f min)",
             engine.stale_after, engine.min_repeat_s / 60)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass

    try:
        while not stop.is_set():
            stop.wait(30)
            active = engine.active()
            if active:
                log.info("%d active alarm(s): %s", len(active),
                         ", ".join(f"{s.channel}={s.state}" for s in active))
    finally:
        log.info("shutting down; %d flight-recorder dump(s) this run",
                 recorder.dumps)
        engine.stop()
        bus.publish_state("alarms", ServiceState.STOPPED)
        bus.disconnect()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
