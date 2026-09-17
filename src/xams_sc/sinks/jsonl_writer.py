"""JSONL archive — the raw truth. See DESIGN.md §9.3.

One file per UTC day, written INDEPENDENTLY of the database. If PostgreSQL
falls over, no data is lost: the files are the truth, and the database is an
index over them.

Flush and fsync at least every 10 s, so a power loss costs seconds, not hours.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
from datetime import timezone
from pathlib import Path

from ..bus import TOPIC_MEAS, Bus
from ..model import Measurement, iso, utcnow

log = logging.getLogger(__name__)


class JsonlWriter:
    """Appends every measurement to data/raw/<UTC date>.jsonl."""

    def __init__(self, bus: Bus, config, directory: Path | str = "data/raw",
                 fsync_interval_s: float = 10.0, version: str = "0.1.0"):
        self.bus = bus
        self.config = config
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.fsync_interval_s = fsync_interval_s
        self.version = version

        self._fh = None
        self._day = None
        self._lock = threading.Lock()
        self._last_sync = time.monotonic()
        self._written = 0

    # ------------------------------------------------------------------ files

    def _path_for(self, day: str) -> Path:
        return self.directory / f"{day}.jsonl"

    def _header(self) -> str:
        """First line of every file: what produced it (§9.3).

        The config hash is what makes a file self-describing. The LabVIEW
        system kept column meanings in a separate, overwritable header file,
        and that is precisely why its old CSVs cannot be trusted without the
        column-count check of §9.6.
        """
        return json.dumps({
            "t": iso(utcnow()),
            "meta": {
                "config": self.config.config_hash,
                "version": self.version,
                "host": platform.node(),
            },
        }, separators=(",", ":"))

    def _ensure_file(self, day: str) -> None:
        if self._day == day and self._fh is not None:
            return
        if self._fh is not None:
            self._fh.close()
        path = self._path_for(day)
        new = not path.exists()
        self._fh = path.open("a", encoding="utf-8")
        if new:
            self._fh.write(self._header() + "\n")
            self._fh.flush()
        self._day = day
        log.info("writing to %s", path)

    # ------------------------------------------------------------------ writes

    def write(self, m: Measurement) -> None:
        day = m.t.astimezone(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            self._ensure_file(day)
            self._fh.write(m.to_json() + "\n")
            self._written += 1
            if time.monotonic() - self._last_sync >= self.fsync_interval_s:
                self._sync_locked()

    def _sync_locked(self) -> None:
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._last_sync = time.monotonic()

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._sync_locked()

    @property
    def written(self) -> int:
        return self._written

    # -------------------------------------------------------------- subscriber

    def start(self) -> None:
        def handler(topic: str, payload: str) -> None:
            try:
                self.write(Measurement.from_payload(json.loads(payload)))
            except Exception:
                # A malformed message must not stop the archive.
                log.exception("could not archive message on %s", topic)

        self.bus.subscribe(f"{TOPIC_MEAS}/#", handler)

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._sync_locked()
                self._fh.close()
                self._fh = None
