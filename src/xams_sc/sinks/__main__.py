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
from pathlib import Path

import yaml

from ..bus import Bus
from ..config import CONFIG_DIR, ConfigError, load
from ..service import SingleInstance, setup_logging
from .alarm_writer import AlarmWriter
from .jsonl_writer import JsonlWriter
from .pg_writer import PgWriter

log = logging.getLogger(__name__)


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
    lock = SingleInstance("sinks", Path("logs"))
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
    else:
        # Not an error. Milestone 1 is expected to run before the database
        # exists, and the archive is what matters.
        log.warning(
            "no PostgreSQL DSN (config/secrets.yaml has no postgres.database); "
            "archiving to JSONL only"
        )

    bus.connect()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass

    try:
        while not stop.is_set():
            stop.wait(5)
            jsonl.flush()
            if pg:
                log.debug("postgres: %s", pg.stats)
    finally:
        log.info("shutting down; %d records archived this run", jsonl.written)
        jsonl.close()
        if pg:
            pg.close()
        if alarm_writer:
            log.info("%d alarm transition(s) recorded this run", alarm_writer.written)
            alarm_writer.close()
        bus.disconnect()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
