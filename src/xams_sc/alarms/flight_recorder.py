"""Flight recorder. See DESIGN.md §9.1.

Holds the last ten minutes of every channel at full rate in memory, and writes
it out when an alarm fires, a channel trips, or a service restarts
unexpectedly.

**This is what the present system lacks.** After an incident there are only
averaged values and no record of the approach to it — which is precisely the
part you want when working out what happened. The buffer costs a few megabytes
of RAM and is never written unless something goes wrong.

A fixed-length deque, so it cannot grow without bound: the failure this guards
against must not create a new one.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
from datetime import timezone
from pathlib import Path

from ..bus import TOPIC_MEAS, Bus
from ..model import Measurement, utcnow

log = logging.getLogger(__name__)


class FlightRecorder:
    def __init__(self, bus: Bus, directory: Path | str = "data/events",
                 window_s: float = 600.0, expected_rate_hz: float = 1.0,
                 channels: int = 60):
        self.bus = bus
        self.directory = Path(directory)
        self.window_s = window_s
        depth = int(window_s * expected_rate_hz * channels)
        self._buffer: collections.deque[Measurement] = collections.deque(maxlen=depth)
        self._lock = threading.Lock()
        self._dumps = 0
        log.debug("flight recorder holds up to %d measurements", depth)

    def record(self, m: Measurement) -> None:
        with self._lock:
            self._buffer.append(m)

    def dump(self, reason: str, channel: str = "system") -> Path | None:
        """Write the buffer out. Named for the moment it describes.

        data/events/2026-09-17T13-02-11_pmain_hihi.jsonl
        """
        with self._lock:
            snapshot = list(self._buffer)
        if not snapshot:
            log.warning("flight recorder asked to dump but the buffer is empty")
            return None

        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{channel}_{reason}")
        path = self.directory / f"{stamp}_{safe}.jsonl"

        with path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "t": stamp, "meta": {
                    "reason": reason, "channel": channel,
                    "window_s": self.window_s, "measurements": len(snapshot),
                    "first": snapshot[0].t.isoformat(),
                    "last": snapshot[-1].t.isoformat(),
                }}, separators=(",", ":")) + "\n")
            for m in snapshot:
                fh.write(m.to_json() + "\n")

        self._dumps += 1
        log.warning("flight recorder dumped %d measurements to %s",
                    len(snapshot), path.name)
        return path

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def dumps(self) -> int:
        return self._dumps

    def start(self) -> None:
        def handler(topic: str, payload: str) -> None:
            try:
                self.record(Measurement.from_payload(json.loads(payload)))
            except Exception:
                log.exception("flight recorder could not record %s", topic)

        self.bus.subscribe(f"{TOPIC_MEAS}/#", handler)
