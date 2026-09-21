"""Entry point for the storage sinks: python -m xams_sc.sinks

Runs the JSONL archive and, when a DSN is configured, the PostgreSQL writer.
Both are ordinary MQTT subscribers (§2.1) — adding one touches no driver.

The JSONL writer runs whether or not PostgreSQL is available, because the
files are the truth and the database is an index over them (§9.3).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

import yaml

from ..bus import Bus
from ..config import CONFIG_DIR, LOG_DIR, ConfigError, load
from ..model import ServiceState, utcnow
from ..service import SingleInstance, setup_logging
from .alarm_writer import AlarmWriter
from .audit_writer import AuditWriter
from .flow_writer import FlowPeriodWriter
from .jsonl_writer import JsonlWriter
from .pg_writer import PgWriter

log = logging.getLogger(__name__)


class SinkHealth:
    """Watches the two places data can be lost, and says so out loud.

    Principle 4 is "fail loudly, never silently", and this is the one place
    the principle is about the system's OWN data rather than an instrument's.
    Until now it was the place that obeyed it least: `PgWriter` counted every
    row it discarded at `max_pending`, and the only consumer logged the count
    at DEBUG. The default level is INFO, so the system's stated worst case —
    measurements thrown away — was invisible in normal operation.

    Two counters matter, and both are cumulative:

        pg.stats["dropped"]   rows discarded because the backlog was full
        bus.dropped           publishes lost because the outage buffer was

    A rise in either is real, unrecoverable loss: the row is not in the
    database and never will be. It is reported at ERROR, and the service
    publishes `degraded` so the loss is visible on /status and not only to
    somebody reading the log at the time.

    Reported on CHANGE, not on every cycle. A service that logs the same
    error every five seconds for a week teaches people to filter it out,
    which costs the next one. A reminder is repeated every `remind_after_s`
    so a long outage does not scroll away entirely.

    A backlog that is merely large is a warning, not a loss: the rows are
    still in memory and will be written when the database returns.
    """

    def __init__(self, bus, pg=None, remind_after_s: float = 300.0,
                 backlog_warn: int = 10_000, service: str = "sinks"):
        self.bus = bus
        self.pg = pg
        self.remind_after_s = remind_after_s
        self.backlog_warn = backlog_warn
        self.service = service
        self._seen_pg_dropped = 0
        self._seen_bus_dropped = 0
        self._degraded = False
        self._warned_backlog = False
        self._last_reminder = 0.0

    def _losses(self) -> list[str]:
        """What has been lost since the last look, in words."""
        losses = []
        if self.pg is not None:
            total = self.pg.stats.get("dropped", 0)
            if total > self._seen_pg_dropped:
                losses.append(
                    "%d measurement row(s) discarded because the write backlog "
                    "was full (%d since this service started)"
                    % (total - self._seen_pg_dropped, total))
                self._seen_pg_dropped = total

        total = getattr(self.bus, "dropped", 0)
        if total > self._seen_bus_dropped:
            losses.append(
                "%d message(s) lost to the broker outage buffer overflowing "
                "(%d since this service started)"
                % (total - self._seen_bus_dropped, total))
            self._seen_bus_dropped = total
        return losses

    def check(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        losses = self._losses()

        if losses:
            for loss in losses:
                log.error("DATA LOST: %s", loss)
            self._last_reminder = now
            if not self._degraded:
                # Loud beyond the log: /status shows this, and a service that
                # is throwing data away is not "running".
                self.bus.publish_state(self.service, ServiceState.DEGRADED)
                self._degraded = True
            return

        if self._degraded:
            # Nothing new was lost this cycle. Recovery is only real once the
            # backlog has drained too, or the next full buffer would look like
            # a fresh problem rather than the same one continuing.
            if self._pending() <= self.backlog_warn:
                log.warning("no further loss and the backlog has drained; "
                            "%d row(s) and %d message(s) were lost in total",
                            self._seen_pg_dropped, self._seen_bus_dropped)
                self.bus.publish_state(self.service, ServiceState.RUNNING)
                self._degraded = False
            elif now - self._last_reminder >= self.remind_after_s:
                log.error("STILL DEGRADED: %d row(s) lost so far, %d waiting "
                          "to be written", self._seen_pg_dropped, self._pending())
                self._last_reminder = now
            return

        pending = self._pending()
        if pending > self.backlog_warn:
            if not self._warned_backlog:
                log.warning("the write backlog is %d rows and growing; nothing "
                            "is lost yet, but it will be at %d", pending,
                            self.pg.max_pending if self.pg else 0)
                self._warned_backlog = True
        elif self._warned_backlog:
            log.info("the write backlog has drained")
            self._warned_backlog = False

    def _pending(self) -> int:
        return self.pg.stats.get("pending", 0) if self.pg is not None else 0

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def lost_rows(self) -> int:
        """Measurement rows discarded since this service started."""
        return self._seen_pg_dropped

    @property
    def lost_messages(self) -> int:
        """Publishes lost to the outage buffer overflowing."""
        return self._seen_bus_dropped



def dsn_from_secrets() -> str | None:
    """Build a PostgreSQL DSN from config/secrets.yaml, if it exists.

    Credentials never enter the repository (§11). Absent secrets is a normal
    state, not an error: the archive runs without a database.
    """
    path = CONFIG_DIR / "secrets.yaml"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            secrets = yaml.safe_load(fh) or {}
    except Exception as exc:
        log.warning("could not read secrets.yaml: %s", exc)
        return None

    pg = secrets.get("postgres") or {}
    if not pg.get("database"):
        return None
    parts = [
        f"host={pg.get('host', '127.0.0.1')}",
        f"port={pg.get('port', 5432)}",
        f"dbname={pg['database']}",
        f"user={pg.get('user', 'xams')}",
    ]
    if pg.get("password"):
        parts.append(f"password={pg['password']}")
    return " ".join(parts)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m xams_sc.sinks")
    p.add_argument("--broker", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--data-dir", default="data/raw")
    p.add_argument("--dsn", default=None,
                   help="PostgreSQL DSN; defaults to config/secrets.yaml")
    p.add_argument("--no-postgres", action="store_true",
                   help="archive to JSONL only")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    setup_logging("sinks", args.log_level)

    # ONE INSTANCE ONLY (§6.1). The device services have always had this; the
    # sinks did not, and on 17 September 2026 two copies ran at once — both
    # appending to the same JSONL file and both inserting into PostgreSQL.
    # Every reading was stored twice.
    #
    # Duplication here is worse than for a driver: two drivers fighting over an
    # instrument fail loudly, whereas two writers succeed quietly and corrupt
    # the archive in a way that only shows up as a row count.
    lock = SingleInstance("sinks", LOG_DIR)
    if not lock.acquire():
        log.critical(
            "FATAL: another sinks process is already running (lock: %s). "
            "Refusing to start — two writers would duplicate every record.",
            lock.path)
        return 1

    try:
        config = load()
    except ConfigError as exc:
        print(f"FATAL: configuration is invalid: {exc}", file=sys.stderr)
        lock.release()
        return 2

    bus = Bus(client_id="sinks", host=args.broker, port=args.port)

    jsonl = JsonlWriter(bus, config, directory=Path(args.data_dir))
    jsonl.start()
    log.info("JSONL archive -> %s", args.data_dir)

    pg = None
    alarm_writer = None
    flow_writer = None
    dsn = args.dsn or dsn_from_secrets()
    if args.no_postgres:
        log.info("PostgreSQL writer disabled (--no-postgres)")
    elif dsn:
        pg = PgWriter(bus, dsn)
        pg.start()
        log.info("PostgreSQL writer started")
        # Alarm state, recorded so Grafana can show it without being part of
        # the alarm path (§11). Another subscriber; no driver or alarm logic
        # is touched by its existence.
        alarm_writer = AlarmWriter(bus, dsn)
        alarm_writer.start()
        log.info("alarm-state writer started")
        # Closed flow-integrator periods (§7.5). A reset closes a period
        # rather than zeroing a counter, and this is what preserves it.
        flow_writer = FlowPeriodWriter(bus, dsn)
        flow_writer.start()
        flow_writer.ensure_open_period("fm101_total", utcnow())
        log.info("flow-period writer started")
        # Every write to an instrument, successful or refused (section 10
        # rule 5). The first writes in this system are the Lake Shore setpoint
        # and heater range.
        audit_writer = AuditWriter(bus, dsn)
        audit_writer.start()
        log.info("audit writer started")
    else:
        # Not an error. Milestone 1 is expected to run before the database
        # exists, and the archive is what matters.
        log.warning(
            "no PostgreSQL DSN (config/secrets.yaml has no postgres.database); "
            "archiving to JSONL only"
        )

    bus.connect()
    # Say we are running. The bus registers a retained last-will of "stopped",
    # so without this the status page shows a healthy service as stopped for
    # its entire life — the will is only correct once the process is gone.
    bus.publish_state("sinks", ServiceState.RUNNING)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass

    health = SinkHealth(bus, pg)
    try:
        while not stop.is_set():
            stop.wait(5)
            jsonl.flush()
            health.check()
            # A heartbeat, like every other service. Without one the only
            # evidence this process is alive is a RETAINED `running` that
            # outlives it: a sinks that HANGS rather than exits publishes no
            # will, so the page went on saying `running` indefinitely. A
            # frozen plausible value is worse than a gap (principle 4), and
            # the archive is the last thing that should be able to die
            # quietly.
            bus.publish_heartbeat("sinks")
    finally:
        log.info("shutting down; %d records archived this run", jsonl.written)
        # Do not let a loss go unsaid because it happened hours ago and the
        # ERROR has scrolled off. The last line of a run should name it.
        if health.degraded or health.lost_rows or health.lost_messages:
            log.error("THIS RUN LOST DATA: %d measurement row(s) discarded, "
                      "%d message(s) lost to buffer overflow",
                      health.lost_rows, health.lost_messages)
        jsonl.close()
        if pg:
            pg.close()
        if alarm_writer:
            log.info("%d alarm transition(s) recorded this run", alarm_writer.written)
            alarm_writer.close()
        if flow_writer:
            flow_writer.close()
        bus.publish_state("sinks", ServiceState.STOPPED)
        bus.disconnect()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
