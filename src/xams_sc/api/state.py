"""Live system state, from the bus. See DESIGN.md §8.1.

**Reads MQTT retained topics, never the database.** If PostgreSQL is down the
status page must still work — that is precisely when it is needed. A status
page that fails together with the component it reports on is worthless.

Retained topics make this cheap: on connect the broker replays the current
value of every channel, every heartbeat and every alarm, so the page is
correct within a second of starting rather than after a full log interval.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from ..bus import (ACK_HV_OUTPUT, ACK_HV_VSET, ACK_LS_RANGE, ACK_LS_SETPOINT,
                   TOPIC_ALARM, TOPIC_BACKUP, TOPIC_FLOW_RESET, TOPIC_MEAS,
                   TOPIC_RELOAD, TOPIC_STATUS, Bus)
from ..model import Measurement, Quality, ServiceState, parse_iso, utcnow

log = logging.getLogger(__name__)


@dataclass
class ChannelView:
    """One channel as the UI needs it."""

    name: str
    value: float | None
    unit: str
    quality: str
    age_s: float
    description: str = ""
    device: str = ""
    alarm: str = "ok"
    alarm_threshold: str | None = None
    acknowledged: bool = False

    @property
    def stale(self) -> bool:
        return self.quality == Quality.STALE.value or self.age_s > 60

    @property
    def healthy(self) -> bool:
        return self.quality == Quality.OK.value and not self.stale

    def formatted(self) -> str:
        """A value fit to print, or an honest dash.

        **A stale channel never shows its last number.** Showing a frozen
        value as though it were live is the classic failure of this kind of
        display, and worse than showing nothing: it invites a decision based
        on a reading that stopped being true an hour ago (§8.2).
        """
        if self.value is None or not self.healthy:
            return "—"
        magnitude = abs(self.value)
        if magnitude >= 1000:
            return f"{self.value:,.0f}"
        if magnitude >= 10:
            return f"{self.value:.2f}"
        return f"{self.value:.3f}"


class SystemState:
    """A live snapshot of everything on the bus.

    One subscriber, updated by callbacks, read by the request handlers. The web
    layer never talks to MQTT itself and never blocks on it.
    """

    def __init__(self, config, bus: Bus):
        self.config = config
        self.bus = bus
        self._lock = threading.Lock()
        self._measurements: dict[str, Measurement] = {}
        self._received_at: dict[str, float] = {}
        self._heartbeats: dict[str, float] = {}
        self._states: dict[str, str] = {}
        self._alarms: dict[str, dict] = {}
        self._flow_gaps: float = 0.0
        self._flow_ack: dict | None = None
        self._acks: dict[str, dict] = {}
        self._backup: dict | None = None
        self.started = utcnow()

    # ---------------------------------------------------------------- inputs

    def _on_measurement(self, topic: str, payload: str) -> None:
        try:
            d = json.loads(payload)
            m = Measurement.from_payload(d)
        except Exception:
            return
        with self._lock:
            self._measurements[m.channel] = m
            self._received_at[m.channel] = time.monotonic()
            if m.channel == "fm101_total" and "gaps_s" in d:
                self._flow_gaps = float(d["gaps_s"])

    def _on_status(self, topic: str, payload: str) -> None:
        parts = topic.split("/")
        if len(parts) != 4:
            return
        service, kind = parts[2], parts[3]
        with self._lock:
            if kind == "heartbeat":
                try:
                    self._heartbeats[service] = (
                        utcnow() - parse_iso(payload)).total_seconds()
                except Exception:
                    pass
            elif kind == "state":
                self._states[service] = payload.strip()

    def _on_alarm(self, topic: str, payload: str) -> None:
        channel = topic.split("/")[-1]
        with self._lock:
            if not payload.strip():
                self._alarms.pop(channel, None)
                return
            try:
                self._alarms[channel] = json.loads(payload)
            except ValueError:
                pass

    def _on_reload(self, topic: str, payload: str) -> None:
        """Re-read the configuration without restarting.

        The UI shows units, descriptions and channel names, all of which come
        from channels.yaml. Holding the copy loaded at startup means an edit
        plus `xams-ctl reload` changes the file, changes the alarm engine, and
        leaves the page showing the old text — which looks like the reload did
        not work at all.
        """
        from ..config import ConfigError
        from ..config import load as load_config
        try:
            new_config = load_config()
        except ConfigError as exc:
            log.error("reload refused, configuration is invalid: %s", exc)
            self.bus.publish_raw("xams/ack/webui/reload",
                                 json.dumps({"service": "webui",
                                             "applied": False,
                                             "error": str(exc)}))
            return
        with self._lock:
            self.config = new_config
        log.info("configuration reloaded (config %s)", new_config.config_hash)
        self.bus.publish_raw("xams/ack/webui/reload",
                             json.dumps({"service": "webui", "applied": True,
                                         "config": new_config.config_hash}))

    def reset_flow(self, who: str, timeout_s: float = 8.0) -> dict | None:
        """Close the running flow period and open a new one (§7.5).

        Returns the closed period, or None if the derived service did not
        acknowledge. **Nothing is erased**: the closed period keeps its total
        and its gaps, which is the whole difference between this and zeroing a
        counter.

        The UI does not perform the reset itself — it asks, over the bus, and
        reports what came back. The integrator owns its own state, and a web
        request must not be able to reach around it.
        """
        self._flow_ack = None
        self.bus.publish_raw(TOPIC_FLOW_RESET, json.dumps({"by": who}))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._flow_ack is not None:
                return self._flow_ack
            time.sleep(0.15)
        return None

    def command(self, topic: str, ack_topic: str, payload: dict,
                timeout_s: float = 10.0) -> dict:
        """Send a command and wait for the service to acknowledge it (§10).

        The UI never touches an instrument. It asks, over the bus, and reports
        what came back — so validation, the read-back and the audit record all
        happen in the one place that owns the hardware, whether the request
        arrived from this page or from the CLI.

        A timeout is reported as failure, NOT as success. If the service is
        down the write did not happen, and saying otherwise would leave
        somebody believing a setpoint had moved.
        """
        with self._lock:
            self._acks.pop(ack_topic, None)
        self.bus.publish_raw(topic, json.dumps(payload))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if ack_topic in self._acks:
                    return self._acks.pop(ack_topic)
            time.sleep(0.1)
        return {"ok": False, "reason": "the %s service did not answer within "
                                       "%.0f s; nothing was changed"
                                       % (topic.split("/")[2], timeout_s)}

    def _on_ack(self, topic: str, payload: str) -> None:
        try:
            with self._lock:
                self._acks[topic] = json.loads(payload)
        except ValueError:
            pass

    def _on_backup(self, topic: str, payload: str) -> None:
        try:
            with self._lock:
                self._backup = json.loads(payload)
        except ValueError:
            pass

    def backup_status(self, overdue_hours: float = 30.0) -> dict:
        """What the nightly backup last did (docs/operating/backup.md).

        Returns {"state": ok|overdue|failed|unknown, "detail", "age_h"}.

        **`unknown` is not `ok`.** Nothing published means the backup has never
        run, or its retained message was cleared - not that all is well. The
        classic way to lose data is a backup that stopped months ago and said
        nothing, so the absence of news is reported as an absence, never as
        good news.

        `overdue` at 30 hours: a daily job that has not reported for more than
        a day and a bit has missed one, and one missed night is worth a look
        before it becomes thirty.
        """
        with self._lock:
            status = dict(self._backup) if self._backup else None
        if status is None:
            return {"state": "unknown", "age_h": None,
                    "detail": "the backup has never reported"}

        age_h = None
        try:
            age_h = (utcnow() - parse_iso(status["t"])).total_seconds() / 3600.0
        except Exception:
            pass

        detail = str(status.get("detail") or "")
        if not status.get("ok"):
            return {"state": "failed", "age_h": age_h,
                    "detail": detail or "the last run failed"}
        if age_h is not None and age_h > overdue_hours:
            return {"state": "overdue", "age_h": age_h,
                    "detail": "last success %.0f hours ago: %s" % (age_h, detail)}
        return {"state": "ok", "age_h": age_h, "detail": detail}

    def _on_flow_ack(self, topic: str, payload: str) -> None:
        try:
            self._flow_ack = json.loads(payload)
        except ValueError:
            pass

    def start(self) -> None:
        self.bus.subscribe(TOPIC_BACKUP, self._on_backup)
        self.bus.subscribe("xams/ack/derived/flow_reset", self._on_flow_ack)
        # EVERY ack topic a command may use. `command()` waits for the reply
        # on one of these, so an unsubscribed topic makes every write time out
        # after ten seconds and report "the service did not answer" - about a
        # write that in fact succeeded. Adding a command means adding its ack
        # here, and forgetting is silent until somebody presses the button.
        self.bus.subscribe(ACK_LS_SETPOINT, self._on_ack)
        self.bus.subscribe(ACK_LS_RANGE, self._on_ack)
        self.bus.subscribe(ACK_HV_VSET, self._on_ack)
        self.bus.subscribe(ACK_HV_OUTPUT, self._on_ack)
        self.bus.subscribe(f"{TOPIC_MEAS}/#", self._on_measurement)
        self.bus.subscribe(f"{TOPIC_STATUS}/#", self._on_status)
        self.bus.subscribe(f"{TOPIC_ALARM}/#", self._on_alarm)
        self.bus.subscribe(TOPIC_RELOAD, self._on_reload)
        self.bus.connect()
        self.bus.publish_state("webui", ServiceState.RUNNING)

    # --------------------------------------------------------------- outputs

    def channel(self, name: str) -> ChannelView | None:
        with self._lock:
            m = self._measurements.get(name)
            received = self._received_at.get(name, 0.0)
            alarm = self._alarms.get(name, {})
        cfg = self.config.channels.get(name)
        if m is None:
            if cfg is None:
                return None
            return ChannelView(name=name, value=None, unit=cfg.unit,
                               quality="no data", age_s=float("inf"),
                               description=cfg.description, device=cfg.device)
        return ChannelView(
            name=name, value=m.value, unit=m.unit, quality=m.quality.value,
            age_s=time.monotonic() - received,
            description=cfg.description if cfg else "",
            device=cfg.device if cfg else "",
            alarm=alarm.get("state", "ok"),
            alarm_threshold=alarm.get("threshold"),
            acknowledged=bool(alarm.get("acknowledged")))

    def channels(self, device: str | None = None) -> list[ChannelView]:
        names = [c.name for c in self.config.enabled_channels()
                 if device is None or c.device == device]
        views = [self.channel(n) for n in names]
        return [v for v in views if v is not None]

    def services(self) -> list[dict]:
        known = ["cdaq", "caen", "lakeshore", "ups", "derived", "alarms", "sinks"]
        with self._lock:
            beats, states = dict(self._heartbeats), dict(self._states)
        out = []
        for name in known:
            age = beats.get(name)
            out.append({
                "name": name,
                "state": states.get(name, "unknown"),
                "age_s": age,
                # sinks and alarms are not BaseService and publish no
                # heartbeat, so absence there is not evidence of a problem.
                "expects_heartbeat": name not in ("sinks", "alarms"),
                "healthy": (age is not None and age < 60)
                           if name not in ("sinks", "alarms") else None,
            })
        return out

    def active_alarms(self) -> list[dict]:
        with self._lock:
            items = dict(self._alarms)
        active = []
        for channel, a in items.items():
            if a.get("state", "ok") == "ok":
                continue
            cfg = self.config.channels.get(channel)
            active.append({
                "channel": channel, "state": a.get("state"),
                "threshold": a.get("threshold"), "value": a.get("value"),
                "acknowledged": bool(a.get("acknowledged")),
                "description": cfg.description if cfg else "",
            })
        order = {"critical": 0, "major": 1, "minor": 2}
        active.sort(key=lambda a: (order.get(a["state"], 9), a["channel"]))
        return active

    def unhealthy_channels(self) -> list[ChannelView]:
        return [c for c in self.channels() if not c.healthy]

    def known_faults(self) -> list[dict]:
        """Channels disabled in the configuration, with the reason.

        A sensor that is known to be broken produces NO data, so it cannot
        appear in anything driven by measurements — it simply vanishes, which
        is how tt202 disappeared from view after being disabled. This reads
        the configuration instead, so a known fault stays visible without
        having to be a permanently active alarm that people learn to ignore.
        """
        faults = []
        for ch in self.config.channels.values():
            if ch.enabled:
                continue
            text = (ch.description or "").lower()
            # A disabled channel is usually just an input nobody wired up.
            # Only one that says it FAILED is a fault worth showing: listing
            # spare inputs here would bury the real thing among them.
            if not any(word in text for word in ("fail", "broken", "fault", "dead")):
                continue
            faults.append({"channel": ch.name, "device": ch.device,
                           "phys": ch.phys, "description": ch.description})
        return sorted(faults, key=lambda f: f["channel"])

    def flow_total(self) -> dict:
        with self._lock:
            gaps = self._flow_gaps
        return {"view": self.channel("fm101_total"),
                "rate": self.channel("fm101"),
                "gaps_s": gaps}

    def uptime(self) -> str:
        seconds = (utcnow() - self.started).total_seconds()
        days, rest = divmod(int(seconds), 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def overall(self) -> tuple[str, str]:
        """One word for the top of the page, and a CSS class."""
        if self.active_alarms():
            worst = self.active_alarms()[0]["state"]
            return (f"{worst.upper()} ALARM", "bad" if worst != "minor" else "warn")
        broken = [s for s in self.services()
                  if s["expects_heartbeat"] and not s["healthy"]]
        if broken:
            return (f"{len(broken)} SERVICE(S) NOT REPORTING", "bad")
        if self.unhealthy_channels():
            return (f"{len(self.unhealthy_channels())} CHANNEL(S) NOT OK", "warn")
        return ("ALL OK", "good")
