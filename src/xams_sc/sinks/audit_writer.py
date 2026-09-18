"""Store the audit trail of every write. See DESIGN.md §10 rule 5.

An ordinary MQTT subscriber, like every other sink (§2.1). The service that
performed the write publishes the record; this stores it. Nothing in a driver
touches the database, and adding this touched no driver.

**Rejected writes are stored too, and that is deliberate.** An attempt that
was refused — a setpoint outside the permitted range, a command naming the
output this cryostat does not use — is exactly as interesting six months later
as one that succeeded. A log of only the successes answers "what did the
system do" but not "what did somebody try to make it do", and the second
question is the one asked after something has gone wrong.

The record is in the JSONL archive regardless, because it went over the bus
(§9). This table is the queryable index over it, in the same relationship as
`meas` to the archive: if PostgreSQL is lost, the audit trail is not.
"""

from __future__ import annotations

import json
import logging

from ..bus import TOPIC_AUDIT, Bus
from ..model import parse_iso, utcnow

log = logging.getLogger(__name__)

INSERT = """
INSERT INTO audit (t, actor, action, target, old_value, new_value, result, detail)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""


class AuditWriter:
    def __init__(self, bus: Bus, dsn: str):
        self.bus = bus
        self.dsn = dsn
        self._conn = None
        self._written = 0

    def _connect(self):
        import psycopg

        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
        return self._conn

    def handle(self, payload: str) -> None:
        try:
            d = json.loads(payload)
        except ValueError:
            log.warning("unparseable audit record: %r", payload[:120])
            return

        # A record with no timestamp is still worth keeping; it is stamped on
        # arrival rather than dropped. Losing the fact that a write happened
        # because its clock field was malformed would be the wrong trade.
        when = utcnow()
        if d.get("t"):
            try:
                when = parse_iso(d["t"])
            except Exception:
                log.warning("audit record has an unreadable timestamp %r; "
                            "stamping it on arrival", d.get("t"))

        row = (when, str(d.get("actor") or "unknown"),
               str(d.get("action") or "unknown"), d.get("target"),
               d.get("old"), d.get("new"),
               str(d.get("result") or "unknown"), d.get("detail") or "")
        try:
            with self._connect().cursor() as cur:
                cur.execute(INSERT, row)
            self._written += 1
        except Exception as exc:
            # Loud. This is the one sink whose failure means a write to an
            # instrument happened with no durable record of it in the database.
            log.error("COULD NOT STORE AN AUDIT RECORD (%s): %s", exc, row)

    def start(self) -> None:
        self.bus.subscribe(TOPIC_AUDIT,
                           lambda topic, payload: self.handle(payload))

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass

    @property
    def written(self) -> int:
        return self._written
