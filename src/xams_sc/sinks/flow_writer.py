"""Closed flow-integrator periods to PostgreSQL. See DESIGN.md §7.5.

A reset closes a period; it does not erase. This stores the closed period so
the history of how much passed through during each one survives:

    start                stop                 total_g  gaps_s  reset_by
    2026-08-01 09:14     2026-09-16 11:02      4213.8     0.0  apc
    2026-09-16 11:02     —                      118.4     0.0  —

Another bus subscriber, like every other sink — the integrator publishes and
does not know this exists (§2.1).

**`gaps_s` is stored with the total, always.** A period that ran with an
outage in it is an underestimate, and the total must carry that evidence
rather than requiring someone to reconstruct it from service logs months
later.
"""

from __future__ import annotations

import json
import logging

from ..bus import TOPIC_FLOW_PERIOD, Bus
from ..model import parse_iso

log = logging.getLogger(__name__)

CLOSE = """
UPDATE flow_periods SET stop = %s, total_g = %s, gaps_s = %s, reset_by = %s
WHERE channel = %s AND stop IS NULL
"""

OPEN = """
INSERT INTO flow_periods (channel, start, total_g, gaps_s)
VALUES (%s, %s, 0, 0)
"""

AUDIT = """
INSERT INTO audit (t, actor, action, target, old_value, new_value, result, detail)
VALUES (%s, %s, 'flow_reset', %s, %s, '0', 'ok', %s)
"""


class FlowPeriodWriter:
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
            log.warning("unparseable flow period: %r", payload[:100])
            return

        channel = d.get("channel", "fm101_total")
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                # Close whatever period was open, then open the next one. If
                # no period was open — first ever reset, or a fresh database —
                # the UPDATE simply affects no rows and the INSERT still runs.
                cur.execute(CLOSE, (parse_iso(d["stop"]), d["total_g"],
                                    d["gaps_s"], d["reset_by"], channel))
                closed = cur.rowcount
                cur.execute(OPEN, (channel, parse_iso(d["stop"])))
                cur.execute(AUDIT, (parse_iso(d["stop"]), d["reset_by"], channel,
                                    f"{d['total_g']:.3f}",
                                    f"closed period totalled {d['total_g']:.3f} g "
                                    f"with {d['gaps_s']:.0f}s of gaps"))
            self._written += 1
            log.info("flow period closed at %.3f g (%.0fs gaps) by %s; "
                     "%d period(s) updated, new one opened",
                     d["total_g"], d["gaps_s"], d["reset_by"], closed)
        except Exception as exc:
            # The reset has already happened in the integrator, whose own
            # state file is the authority. Failing to record it here must not
            # make it look as though it did not happen.
            log.error("could not record the closed flow period: %s", exc)
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None

    def ensure_open_period(self, channel: str, start) -> None:
        """Make sure a period is open, so the first reset has one to close."""
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM flow_periods WHERE channel=%s AND stop IS NULL",
                            (channel,))
                if cur.fetchone() is None:
                    cur.execute(OPEN, (channel, start))
                    log.info("opened the first flow period for %s", channel)
        except Exception as exc:
            log.warning("could not open the initial flow period: %s", exc)

    def start(self) -> None:
        self.bus.subscribe(TOPIC_FLOW_PERIOD,
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
