"""The daily report email. See DESIGN.md §11.

    python -m xams_sc.alarms.daily                  # render and send
    python -m xams_sc.alarms.daily --preview out.html   # render only, send nothing
    python -m xams_sc.alarms.daily --to me@x.nl     # send to one address

Run from Task Scheduler, like the backup. NOT from the alarm service: a report
is a convenience and the alarm engine is not, and a scheduler bug that wedged
a thread inside the engine would take the alarms down with it. Separate
process, separate failure.

**The state comes from retained MQTT, never from the database** (§8.1). The
report must be sendable when PostgreSQL is down, because that is one of the
things worth being told about.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import yaml

from ..api.state import SystemState
from ..bus import Bus
from ..config import CONFIG_DIR, load
from ..model import utcnow
from . import mail
from .notify import Notifier

log = logging.getLogger(__name__)

# Retained messages arrive in a burst on subscribe. Long enough for all of
# them on a quiet broker, short enough that a scheduled task is not held open.
SETTLE_S = 4.0


def _read_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("could not read %s", name)
        return {}


def gather(broker: str, port: int) -> SystemState:
    """Connect, wait for the retained state, and hand back the snapshot."""
    state = SystemState(load(), Bus(client_id="xams-daily", host=broker, port=port))
    state.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not state.bus.connected:
        time.sleep(0.2)
    if not state.bus.connected:
        raise RuntimeError("the broker at %s:%d is not reachable, so there is "
                           "nothing to report" % (broker, port))
    time.sleep(SETTLE_S)
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--broker", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--preview", metavar="FILE",
                        help="write the HTML to FILE and send nothing")
    parser.add_argument("--to", metavar="ADDRESS",
                        help="send to this address only, ignoring recipients.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    try:
        state = gather(args.broker, args.port)
    except Exception as exc:
        log.error("%s", exc)
        return 1

    try:
        subject, html, text = mail.digest(state, utcnow())
    finally:
        state.bus.disconnect()

    if args.preview:
        Path(args.preview).write_text(html, encoding="utf-8")
        print(f"subject: {subject}")
        print(f"written: {args.preview}  ({len(html):,} bytes)")
        print("nothing was sent")
        return 0

    secrets = _read_yaml("secrets.yaml")
    if not (secrets.get("email") or {}).get("smtp_host"):
        log.error("no smtp_host in secrets.yaml; nothing can be sent")
        return 1

    if args.to:
        people = [{"name": args.to, "email": args.to, "enabled": True}]
    else:
        people = [r for r in (_read_yaml("recipients.yaml").get("recipients") or [])
                  if r.get("enabled") and r.get("email")]

    if not people:
        # Not a silent no-op: a report nobody receives is the same as no
        # report, and the reason is a one-line fix in recipients.yaml.
        log.error("no enabled recipients with an email address; nothing sent")
        return 1

    notifier = Notifier(secrets, lambda: people)
    sent = 0
    for person in people:
        if notifier.send_email(person["email"], subject, text, html=html):
            sent += 1
            log.info("sent to %s", person["email"])
        else:
            log.error("FAILED to send to %s", person["email"])

    log.info("daily report: %d of %d delivered", sent, len(people))
    # Partial delivery is a failure, so the scheduled task shows it rather
    # than reporting success because somebody got it.
    return 0 if sent == len(people) else 1


if __name__ == "__main__":
    sys.exit(main())
