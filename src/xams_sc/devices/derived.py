"""The flow integrator. See DESIGN.md §7.5.

Subscribes to `fm101` and publishes `fm101_total`: the mass that has passed
through since the period started.

**This is the only stateful component in the system.** Everything else is
restartable without consequence; an integrator is not. That raises three
requirements, none of which may be skipped.

**It survives a restart.** After every publish the accumulator is written to
disk with the timestamp of the last sample processed, and read back on startup.
A Windows update that reset the total to zero would make the feature useless.

**Gaps are recorded, never invented.** If the service was down for two hours,
what flowed during those two hours is unknown. Extrapolating the last value is
tempting and wrong. Such intervals are excluded and counted separately, so the
total carries the evidence that it is an underestimate instead of that having
to be reconstructed months later. Samples whose quality is not `ok` are
likewise not integrated.

**Reset closes a period; it does not erase.** Rather than zeroing a counter,
the running period is closed and a new one opened, so the history of how much
passed through during each period survives.

UNITS. `fm101` is a mass flow in **grams per minute**. The integral is
`sum(flow * dt)` with `dt` in **minutes**, giving grams. A `dt` in seconds
produces a factor-60 error that looks entirely plausible — see
`scaling.integrate_step`, where the conversion lives once.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ..bus import TOPIC_FLOW_PERIOD, TOPIC_FLOW_RESET, TOPIC_MEAS, Bus
from ..model import Measurement, Quality, iso, parse_iso, utcnow
from ..scaling import integrate_step
from ..service import BaseService

log = logging.getLogger(__name__)

# A sample further from the last one than this is treated as a gap rather than
# an interval to integrate over. Ten log intervals: long enough to ride out a
# slow cycle, short enough that a real outage is not quietly integrated across.
MAX_INTERVAL_S = 120.0


@dataclass
class IntegratorState:
    total_g: float = 0.0
    gaps_s: float = 0.0
    last_sample_t: str | None = None
    period_start: str | None = None

    @classmethod
    def load(cls, path: Path) -> "IntegratorState":
        if not path.exists():
            return cls(period_start=iso(utcnow()))
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            # A corrupt state file must not silently restart the total at
            # zero: that is exactly the data loss this class exists to avoid.
            log.exception("could not read %s — refusing to start from zero", path)
            raise

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-and-rename, so a power loss mid-write cannot truncate the file
        # to something that parses as a smaller total.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)


class FlowIntegrator:
    """Accumulates mass from a flow channel. Bus consumer, not a driver."""

    def __init__(self, bus: Bus, config, source: str = "fm101",
                 output: str = "fm101_total",
                 state_path: Path | str = "data/fm101_total.json"):
        self.bus = bus
        self.config = config
        self.source = source
        self.output = output
        self.state_path = Path(state_path)
        self.state = IntegratorState.load(self.state_path)
        self._lock = threading.Lock()

        # The last sample time at FULL precision, held in memory.
        #
        # The persisted copy goes through iso(), which is millisecond
        # precision (§5). Computing dt against the truncated value makes the
        # previous sample look very slightly earlier, so every dt is slightly
        # too long and the total drifts UPWARD by up to half a millisecond of
        # flow per sample. Tiny, but systematic and always the same direction,
        # which in an integrator accumulates forever.
        #
        # So: full precision while running, the persisted value only after a
        # restart, where a sub-millisecond error is irrelevant next to the
        # gap that the restart itself created.
        self._last_t = None
        if self.state.period_start is None:
            self.state.period_start = iso(utcnow())

        started = self.state.last_sample_t
        log.info("integrator resumed at %.3f g (gaps %.0fs), last sample %s",
                 self.state.total_g, self.state.gaps_s, started or "never")

    def on_measurement(self, m: Measurement) -> None:
        if m.channel != self.source:
            return
        with self._lock:
            self._accumulate(m)

    def _accumulate(self, m: Measurement) -> None:
        previous = self._last_t
        if previous is None and self.state.last_sample_t is not None:
            previous = parse_iso(self.state.last_sample_t)
        self._last_t = m.t
        self.state.last_sample_t = iso(m.t)

        if m.quality is not Quality.OK or m.value is None:
            # Not integrated, and the interval it covers is a gap: we do not
            # know what flowed while the sensor was not reporting.
            if previous is not None:
                self.state.gaps_s += (m.t - previous).total_seconds()
            self._publish(m)
            return

        if previous is None:
            # First sample ever, or first after a restart with no history.
            self._publish(m)
            self.state.save(self.state_path)
            return

        dt = (m.t - previous).total_seconds()
        if dt <= 0:
            return
        if dt > MAX_INTERVAL_S:
            # A gap, not an interval. Integrating across it would invent the
            # flow that happened while nothing was watching.
            self.state.gaps_s += dt
            log.warning("gap of %.0fs in %s — excluded from the total, "
                        "which is now an underestimate", dt, self.source)
        else:
            self.state.total_g += integrate_step(m.value, dt)

        self._publish(m)
        self.state.save(self.state_path)

    def _publish(self, m: Measurement) -> None:
        payload = Measurement(
            t=m.t, channel=self.output, value=self.state.total_g, unit="g",
            quality=Quality.OK, src=m.src)
        # gaps_s rides along so the total always carries the evidence of how
        # much is missing from it.
        d = payload.to_payload()
        d["gaps_s"] = round(self.state.gaps_s, 1)
        self.bus.publish_raw(f"{TOPIC_MEAS}/{self.output}",
                             json.dumps(d, separators=(",", ":")), retain=True)

    def reset(self, who: str) -> dict:
        """Close the running period and open a new one.

        Returns the closed period so the caller can record it. Nothing is
        erased: the number somebody would otherwise have discarded is the
        history of how much passed through during that period.
        """
        with self._lock:
            closed = {
                "start": self.state.period_start,
                "stop": iso(utcnow()),
                "total_g": self.state.total_g,
                "gaps_s": self.state.gaps_s,
                "reset_by": who,
            }
            self.state = IntegratorState(
                total_g=0.0, gaps_s=0.0,
                last_sample_t=self.state.last_sample_t,
                period_start=iso(utcnow()))
            # _last_t is deliberately left alone: a reset closes a period, it
            # does not create a gap in the sampling.
            self.state.save(self.state_path)
        log.warning("flow integrator reset by %s — closed period totalled "
                    "%.3f g with %.0fs of gaps", who, closed["total_g"],
                    closed["gaps_s"])
        return closed

    def start(self) -> None:
        def handler(topic: str, payload: str) -> None:
            try:
                self.on_measurement(Measurement.from_payload(json.loads(payload)))
            except Exception:
                log.exception("integrator could not handle %s", topic)

        self.bus.subscribe(f"{TOPIC_MEAS}/{self.source}", handler)


class DerivedService(BaseService):
    """Runs the integrator as a service, so it has a heartbeat like any other."""

    name = "derived"

    def __init__(self, config, bus, simulate: bool = False, **kw):
        super().__init__(config, bus, simulate=simulate, **kw)
        self.integrator = FlowIntegrator(bus, config)

    def verify_identity(self) -> bool:
        return True

    def _handle_reset(self, topic: str, payload: str) -> None:
        """Close the running period and open a new one. See DESIGN.md §7.5.

        A reset is a CONTROL ACTION, so it is audited and acknowledged like
        any other (§10) — even though it writes nothing to hardware. What it
        changes is the record, and a number somebody quietly discarded is
        exactly what §7.5 exists to prevent.
        """
        try:
            who = (json.loads(payload) or {}).get("by") or "unknown"
        except ValueError:
            who = "unknown"

        closed = self.integrator.reset(who)

        # Published, not written. Nothing here touches a database directly
        # (§2.1) — a sink subscribes and stores it, exactly like a
        # measurement.
        self.bus.publish_raw(TOPIC_FLOW_PERIOD,
                             json.dumps({"channel": self.integrator.output,
                                         **closed}, separators=(",", ":")))
        self.bus.publish_raw(
            "xams/ack/derived/flow_reset",
            json.dumps({"ok": True, **closed}, separators=(",", ":")))

    def run(self) -> int:
        self.integrator.start()
        self.bus.subscribe(TOPIC_FLOW_RESET, self._handle_reset)
        return super().run()

    def read(self) -> list[Measurement]:
        # Nothing to read: this service is driven by the bus, not by a device.
        # The loop still runs so the heartbeat is published and the staleness
        # monitor can see that the integrator is alive.
        return []
