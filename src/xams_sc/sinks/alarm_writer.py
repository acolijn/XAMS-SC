"""Alarm state to PostgreSQL. See DESIGN.md §11.

The alarm engine publishes state to `xams/alarm/<channel>`. This stores it, so
Grafana can show current state and history **without being part of the alarm
mechanism** — which is the whole reason Grafana's own alerting is not used
(§11). The engine decides and notifies; this only records what it decided.

An ordinary bus subscriber, like every other sink: adding it touched no driver
and no alarm logic.

Only TRANSITIONS are written, not every repeat. The engine publishes retained
state, and a reconnect re-delivers it; storing that again would fill the table
with rows saying the same thing. A row here means "the state changed at this
moment", which is what makes the history readable.
"""

from __future__ import annotations

import json
import logging
import threading

from ..bus import TOPIC_ALARM, Bus
from ..model import parse_iso, utcnow

log = logging.getLogger(__name__)

INSERT = """
INSERT INTO alarm_events (t, channel, state, threshold, value, message)
VALUES (%s, %s, %s, %s, %s, %s)
"""


class AlarmWriter:
    def __init__(self, bus: Bus, dsn: str, label: str = "alarms"):
        self.bus = bus
        self.dsn = dsn
        self.label = label
        self._conn = None
        self._lock = threading.Lock()
        self._last: dict[str, tuple] = {}
        self._written = 0

    def _connect(self):
        import psycopg

        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
        return self._conn

    def handle(self, channel: str, payload: str) -> None:
        if not payload.strip():
            # An empty retained payload is how MQTT deletes a topic, not a
            # corrupt message. It arrives when tools/clear_retained.py removes
            # an alarm for a channel that no longer exists. Forget the channel
            # so a later genuine alarm is recorded as a fresh transition.
            self._last.pop(channel, None)
            log.debug("retained alarm for %s cleared", channel)
            return
        try:
            d = json.loads(payload)
        except ValueError:
            log.warning("unparseable alarm payload on %s: %r", channel, payload[:80])
            return

        state = d.get("state", "ok")
        threshold = d.get("threshold")
        key = (state, threshold, bool(d.get("acknowledged")))

        with self._lock:
            if self._last.get(channel) == key:
                return          # no change; a retained re-delivery
            self._last[channel] = key

        t = utcnow()
        if d.get("since"):
            try:
                t = parse_iso(d["since"])
            except Exception:
                pass

        message = (f"{channel} {state}"
                   + (f" on {threshold}" if threshold else "")
                   + (" (acknowledged)" if d.get("acknowledged") else ""))
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(INSERT, (t, channel, state, threshold,
                                     d.get("value"), message))
            self._written += 1
            log.info("recorded %s", message)
        except Exception as exc:
            # Failing to record an alarm must never interfere with raising it.
            # The engine has already notified by the time this runs.
            log.warning("[%s] could not record alarm for %s: %s",
                        self.label, channel, exc)
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None

    def start(self) -> None:
        self.bus.subscribe(
            f"{TOPIC_ALARM}/#",
            lambda topic, payload: self.handle(topic.split("/")[-1], payload))

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass

    @property
    def written(self) -> int:
        return self._written
