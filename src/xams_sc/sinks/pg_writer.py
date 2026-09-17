"""PostgreSQL working store. See DESIGN.md §9.2.

This database is a CACHE, NOT STORAGE. The archive is the files (§9.3, §9.4).
If it is lost, or a different database is wanted later, write a new sink and
replay the Parquet history into it. Nothing in any driver changes.

Grafana reads from here.

A failure to write must never block the bus or the services: the writer logs,
alarms, and keeps going.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from ..bus import TOPIC_MEAS, Bus
from ..model import Measurement

log = logging.getLogger(__name__)

# Two forms of the insert, chosen at connect time by whether the unique index
# actually exists (see _detect_upsert).
#
# ON CONFLICT DO NOTHING needs the unique index on (t, channel, src). The bus
# re-delivers retained messages whenever this writer reconnects, so the last
# value of every channel arrives again with its original timestamp — MQTT
# working as designed, not a fault — and inserting it twice would inflate the
# history.
#
# But the writer MUST NOT hard-depend on that index being present. On
# 17 September 2026 it did, the index creation had failed on a permissions
# problem, and every insert then failed with "there is no unique or exclusion
# constraint matching the ON CONFLICT specification". The database stopped
# receiving data entirely while the archive carried on, which is the right way
# round but is not a state to enter silently.
INSERT_UPSERT = """
INSERT INTO meas (t, channel, value, raw, unit, quality, src)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (t, channel, src) DO NOTHING
"""

INSERT_PLAIN = """
INSERT INTO meas (t, channel, value, raw, unit, quality, src)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

HAS_UNIQUE_INDEX = """
SELECT 1 FROM pg_indexes
WHERE tablename = 'meas' AND indexname = 'meas_unique_reading'
"""


class PgWriter:
    """Batches measurements and flushes them at least every `flush_interval_s`.

    Two of these can run side by side with different addresses — one local,
    one on a Nikhef VM — and that is the whole of the two-writer topology in
    §12. If the VM is unreachable only that writer fails; it buffers and
    catches up, and nothing local notices.
    """

    def __init__(self, bus: Bus, dsn: str, label: str = "postgres",
                 flush_interval_s: float = 10.0, batch_size: int = 500,
                 max_pending: int = 100_000, src: str = "xams"):
        self.bus = bus
        self.dsn = dsn
        self.label = label
        self.flush_interval_s = flush_interval_s
        self.batch_size = batch_size
        self.max_pending = max_pending
        self.src = src

        self._pending: list[tuple] = []
        self._lock = threading.Lock()
        self._conn = None
        self._insert = INSERT_PLAIN
        self._warned_no_index = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._written = 0
        self._dropped = 0

    # ------------------------------------------------------------- connection

    def _connect(self):
        import psycopg

        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
        self._detect_upsert(self._conn)
        log.info("[%s] connected", self.label)
        return self._conn

    def _detect_upsert(self, conn) -> None:
        """Use ON CONFLICT only if the unique index is actually there.

        Adapting beats failing: a missing index costs duplicate protection,
        which is recoverable, whereas refusing to insert costs the data.
        """
        try:
            with conn.cursor() as cur:
                cur.execute(HAS_UNIQUE_INDEX)
                present = cur.fetchone() is not None
        except Exception:
            present = False

        self._insert = INSERT_UPSERT if present else INSERT_PLAIN
        if not present and not self._warned_no_index:
            log.warning(
                "[%s] the unique index meas_unique_reading is missing, so "
                "duplicate readings cannot be rejected by the database. "
                "Writing anyway. Apply sql/schema.sql as the table owner to "
                "restore it.", self.label)
            self._warned_no_index = True

    # ------------------------------------------------------------------ writes

    def add(self, m: Measurement) -> None:
        # The measurement's own provenance wins. self.src is only the default
        # for readings that do not declare one.
        row = (m.t, m.channel, m.value, m.raw, m.unit, m.quality.value,
               m.src or self.src)
        with self._lock:
            if len(self._pending) >= self.max_pending:
                # Bounded, for the same reason the bus buffer is bounded: an
                # unbounded queue turns an outage into a memory leak.
                self._dropped += 1
                return
            self._pending.append(row)

    def flush(self) -> int:
        with self._lock:
            if not self._pending:
                return 0
            batch, self._pending = self._pending[: self.batch_size], \
                self._pending[self.batch_size:]

        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.executemany(self._insert, batch)
            self._written += len(batch)
            return len(batch)
        except Exception as exc:
            # Put the batch back and keep going. The archive is elsewhere, so
            # a database outage costs query convenience, never data.
            with self._lock:
                self._pending[:0] = batch
            log.warning("[%s] write failed, %d rows pending: %s",
                        self.label, len(self._pending), exc)
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
            return 0

    # -------------------------------------------------------------- subscriber

    def start(self) -> None:
        def handler(topic: str, payload: str) -> None:
            try:
                self.add(Measurement.from_payload(json.loads(payload)))
            except Exception:
                log.exception("[%s] could not queue message on %s", self.label, topic)

        self.bus.subscribe(f"{TOPIC_MEAS}/#", handler)

        def pump():
            while not self._stop.is_set():
                self.flush()
                self._stop.wait(self.flush_interval_s)

        self._thread = threading.Thread(target=pump, name=f"pg-{self.label}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        deadline = time.monotonic() + 10
        while self._pending and time.monotonic() < deadline:
            if self.flush() == 0:
                break
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass

    @property
    def stats(self) -> dict:
        return {"written": self._written, "pending": len(self._pending),
                "dropped": self._dropped}
