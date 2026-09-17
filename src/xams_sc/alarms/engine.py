"""Alarm evaluation. See DESIGN.md §11.

Subscribes to `xams/meas/#`, evaluates against `alarms.yaml`, and publishes
alarm state to `xams/alarm/<channel>`. It decides and notifies; Grafana only
displays the result.

**Grafana's own alerting is deliberately not used.** It works by querying the
database on a schedule, which puts PostgreSQL and Grafana inside the alarm
path: if either is down or slow, notifications do not go out. Alarms are the
part of this system that must be most reliable, so the path is kept as short as
possible — measurement, bus, engine, SMS — with no database and no web server
involved.

Four threshold levels per channel, EPICS-style: `lolo`, `low`, `high`, `hihi`.

**Staleness is an alarm at the same severity as a threshold breach.** A dead
sensor must not read as healthy, and a channel that has stopped arriving is
indistinguishable from one that never had a problem unless something says so.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..bus import TOPIC_ALARM, TOPIC_MEAS, Bus
from ..model import Measurement, Quality, iso, utcnow

log = logging.getLogger(__name__)

# Ordered least to most severe. Used to decide whether a transition is an
# escalation (notify immediately) or a de-escalation.
SEVERITY_ORDER = ["ok", "minor", "major", "critical"]

# Which threshold names mean "too low" — they compare with < rather than >.
LOW_THRESHOLDS = {"lolo", "low"}


@dataclass
class ChannelState:
    """What the engine remembers about one channel."""

    channel: str
    state: str = "ok"
    threshold: str | None = None
    since: datetime | None = None
    last_notified: float = 0.0
    acknowledged: bool = False
    silenced_until: float = 0.0
    last_seen: float = field(default_factory=time.monotonic)
    last_value: float | None = None
    stale: bool = False


class AlarmEngine:
    """Evaluates measurements against thresholds and raises alarms.

    Notification is injected rather than imported, so the engine can be tested
    without a gateway and so what sits underneath SMS is replaceable without
    touching alarm logic (§11).
    """

    def __init__(self, bus: Bus, config, notifier=None, on_alarm=None):
        self.bus = bus
        self.config = config
        self.notifier = notifier
        # Called when an alarm is raised — the flight recorder subscribes here
        # so the dump happens at the moment of the alarm (§9.1).
        self.on_alarm = on_alarm

        alarms = config.alarms or {}
        self.defaults = alarms.get("defaults") or {}
        self.thresholds = alarms.get("channels") or {}
        self.routing = alarms.get("routing") or {}
        self.staleness = alarms.get("staleness") or {}

        self.stale_after = float(self.defaults.get("stale_after_seconds", 60))
        self.min_repeat_s = float(self.defaults.get("min_repeat_minutes", 15)) * 60
        self.hysteresis_fraction = float(self.defaults.get("hysteresis", 0.02))

        self._states: dict[str, ChannelState] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- evaluation

    def _threshold_for(self, channel: str, value: float, current: str | None):
        """Return (threshold_name, severity) for a value, or (None, 'ok').

        Hysteresis: a channel already in alarm must come back past the
        threshold by `hysteresis` of its own magnitude before clearing. This
        is what stops a value sitting exactly on a limit from producing a
        stream of notifications.
        """
        limits = self.thresholds.get(channel) or {}
        if not limits:
            return None, "ok"

        # Most severe first, so a value past hihi reports hihi rather than high.
        for name in ("hihi", "lolo", "high", "low"):
            spec = limits.get(name)
            if not spec:
                continue
            limit = float(spec["value"])
            margin = abs(limit) * self.hysteresis_fraction
            if name in LOW_THRESHOLDS:
                # Already alarming on this threshold: require recovery past
                # limit + margin before it clears.
                trip = limit + margin if current == name else limit
                if value < trip:
                    return name, str(spec.get("severity", "minor"))
            else:
                trip = limit - margin if current == name else limit
                if value > trip:
                    return name, str(spec.get("severity", "minor"))
        return None, "ok"

    def on_measurement(self, m: Measurement) -> None:
        """Evaluate one reading. Called from the bus handler."""
        with self._lock:
            first_time = m.channel not in self._states
            state = self._states.setdefault(m.channel, ChannelState(m.channel))
            state.last_seen = time.monotonic()

            if first_time:
                # PUBLISH THE STATE THE FIRST TIME A CHANNEL IS SEEN, even when
                # it is ok.
                #
                # Otherwise a channel that was in alarm when the engine last
                # stopped keeps that as its recorded state forever: on restart
                # the channel is healthy, nothing "changes", so no transition
                # is published and the stored history still says alarm. The
                # Grafana table then shows permanent phantom alarms for
                # channels that are perfectly fine — which is worse than no
                # table, because it trains people to ignore it.
                #
                # One extra row per channel per engine start is a cheap price
                # for the recorded state matching reality after a restart.
                state.since = utcnow()
                self._publish(state, m.value)
            state.last_value = m.value
            if state.stale:
                state.stale = False
                log.info("%s is being published again", m.channel)

            # A reading that is not OK is itself an alarm condition: the
            # sensor is not reporting, which is not the same as being fine.
            if m.quality is not Quality.OK or m.value is None:
                self._transition(state, "major", f"quality={m.quality.value}",
                                  m.value)
                return

            threshold, severity = self._threshold_for(
                m.channel, m.value, state.threshold)
            self._transition(state, severity, threshold, m.value)

    def _transition(self, state: ChannelState, severity: str,
                    threshold: str | None, value: float | None) -> None:
        """Move a channel to a new state and notify if it deserves it."""
        now = time.monotonic()
        changed = severity != state.state or threshold != state.threshold

        if changed:
            previous = state.state
            state.state = severity
            state.threshold = threshold
            state.since = utcnow()
            # A NEW condition clears an acknowledgement. Acknowledging "high"
            # must not silence the "hihi" that follows it.
            if SEVERITY_ORDER.index(severity) > SEVERITY_ORDER.index(previous):
                state.acknowledged = False
            log.log(logging.WARNING if severity != "ok" else logging.INFO,
                    "%s: %s -> %s (%s, value=%s)",
                    state.channel, previous, severity, threshold or "-", value)
            self._publish(state, value)
            if severity != "ok":
                self._notify(state, value)
                if self.on_alarm is not None:
                    try:
                        self.on_alarm(state.channel, severity, threshold)
                    except Exception:
                        log.exception("on_alarm hook raised")
            return

        # Unchanged and still in alarm: repeat at the configured interval,
        # unless acknowledged or silenced. Deduplication (§11).
        if severity != "ok" and not state.acknowledged and now >= state.silenced_until:
            if now - state.last_notified >= self.min_repeat_s:
                self._notify(state, value)

    # ---------------------------------------------------------------- output

    def _publish(self, state: ChannelState, value: float | None) -> None:
        payload = json.dumps({
            "state": state.state,
            "threshold": state.threshold,
            "value": value,
            "since": iso(state.since) if state.since else None,
            "acknowledged": state.acknowledged,
        }, separators=(",", ":"))
        # Retained, so a UI connecting later sees the true current state (§11).
        self.bus.publish_raw(f"{TOPIC_ALARM}/{state.channel}", payload, retain=True)

    def _notify(self, state: ChannelState, value: float | None) -> None:
        state.last_notified = time.monotonic()
        if self.notifier is None:
            return
        text = (f"XAMS {state.state.upper()}: {state.channel} "
                f"{state.threshold or ''} value={value}")
        channels = self._routes_for(state)
        try:
            self.notifier.send(text, channels)
        except Exception:
            # A failing gateway must not stop the engine evaluating.
            log.exception("notification failed for %s", state.channel)

    def _routes_for(self, state: ChannelState) -> list[str]:
        limits = self.thresholds.get(state.channel) or {}
        spec = limits.get(state.threshold or "") or {}
        if spec.get("notify"):
            return list(spec["notify"])
        if state.threshold is None:          # quality or staleness
            return list(self.staleness.get("notify") or ["email"])
        return ["email"]

    # -------------------------------------------------------------- staleness

    def check_staleness(self) -> None:
        """Raise an alarm for any channel that has stopped arriving.

        This is why the engine cannot be purely event-driven: the absence of a
        message is the signal, and nothing will deliver it.
        """
        now = time.monotonic()
        severity = str(self.staleness.get("severity", "major"))
        with self._lock:
            for state in self._states.values():
                age = now - state.last_seen
                if age > self.stale_after and not state.stale:
                    state.stale = True
                    log.warning("%s has not been published for %.0fs — stale",
                                state.channel, age)
                    self._transition(state, severity, "staleness", None)

    # ----------------------------------------------------------------- reload

    def reload(self, config) -> bool:
        """Apply new thresholds without restarting. See DESIGN.md §12.

        This is the service where reload matters most: a threshold change is
        the commonest configuration edit, and restarting the engine to apply
        one loses every channel's alarm state — including which alarms are
        acknowledged, which would start the notifications again.

        Per-channel state is therefore KEPT across a reload. A channel whose
        threshold moved is re-evaluated on its next reading, which arrives
        within the log interval.
        """
        alarms = config.alarms or {}
        self.config = config
        self.defaults = alarms.get("defaults") or {}
        self.thresholds = alarms.get("channels") or {}
        self.routing = alarms.get("routing") or {}
        self.staleness = alarms.get("staleness") or {}
        self.stale_after = float(self.defaults.get("stale_after_seconds", 60))
        self.min_repeat_s = float(self.defaults.get("min_repeat_minutes", 15)) * 60
        self.hysteresis_fraction = float(self.defaults.get("hysteresis", 0.02))

        # A channel that no longer has any threshold must not stay in alarm on
        # a limit that has been deleted.
        with self._lock:
            for state in self._states.values():
                if (state.threshold in (None, "staleness")
                        or state.channel in self.thresholds):
                    continue
                log.info("%s: thresholds removed, clearing its alarm", state.channel)
                state.state, state.threshold = "ok", None
                self._publish(state, state.last_value)

        self._publish_limits()
        return True

    # ------------------------------------------------------------- operations

    def acknowledge(self, channel: str) -> bool:
        """Stop the repeating notification. The condition stays active.

        Acknowledging is not fixing, and the two must never look alike (§8.1).
        """
        with self._lock:
            state = self._states.get(channel)
            if state is None or state.state == "ok":
                return False
            state.acknowledged = True
            log.info("%s acknowledged — still %s", channel, state.state)
            self._publish(state, state.last_value)
            return True

    def silence(self, channel: str, minutes: float) -> bool:
        """Suppress notifications for a deliberate intervention."""
        with self._lock:
            state = self._states.get(channel)
            if state is None:
                return False
            state.silenced_until = time.monotonic() + minutes * 60
            log.info("%s silenced for %.0f minutes", channel, minutes)
            return True

    def active(self) -> list[ChannelState]:
        with self._lock:
            return [s for s in self._states.values() if s.state != "ok"]

    # ------------------------------------------------------------------- run

    def start(self) -> None:
        def handler(topic: str, payload: str) -> None:
            try:
                self.on_measurement(Measurement.from_payload(json.loads(payload)))
            except Exception:
                log.exception("could not evaluate message on %s", topic)

        self.bus.subscribe(f"{TOPIC_MEAS}/#", handler)
        self._publish_limits()

        def pump():
            while not self._stop.is_set():
                try:
                    self.check_staleness()
                except Exception:
                    log.exception("staleness check raised")
                self._stop.wait(5.0)

        self._thread = threading.Thread(target=pump, name="alarm-staleness",
                                        daemon=True)
        self._thread.start()

    def _publish_limits(self) -> None:
        """Publish the thresholds actually loaded (§11).

        The real risk is not a missing line on a plot; it is believing a
        threshold is 2.0 when it is 20. This exposes what is in force.
        """
        summary = {
            channel: {name: spec.get("value")
                      for name, spec in (limits or {}).items()}
            for channel, limits in self.thresholds.items()
        }
        self.bus.publish_raw("xams/status/limits",
                             json.dumps(summary, separators=(",", ":")),
                             retain=True)
        if not summary:
            log.warning(
                "NO PHYSICS THRESHOLDS ARE CONFIGURED. Staleness and bad-quality "
                "alarms still work, but no channel has a lolo/low/high/hihi "
                "limit. Fill alarms.yaml from the LabVIEW Error and Alarm tab.")
        else:
            log.info("thresholds in force for %d channel(s): %s",
                     len(summary), ", ".join(sorted(summary)))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
