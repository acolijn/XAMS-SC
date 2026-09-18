"""Lake Shore Model 335 temperature controller. See DESIGN.md §7.3.

Reads the two sensor inputs, the two heater outputs, the setpoints, the heater
ranges and the PID settings.

**This driver WRITES — the setpoint and the heater range, on output 1 only.**
It is the first write path in the system (§10), added once §10's open decision
was resolved. Everything else here is still read-only, and the two are kept
visibly apart: `query` asks, `send` instructs, and they are separate methods so
that a typo cannot turn one into the other.

Every write is VALIDATED against channels.yaml, READ BACK from the instrument
before it is called successful, ACKNOWLEDGED on the bus and AUDITED. Output 2
is not used on this cryostat and is refused.

**One conversation at a time.** A serial instrument matches a reply to a query
only by order of arrival, so the poll loop and the control path must not talk
over each other. They did, on 18 September 2026, and a setpoint read back as
the heater percentage. See the lock in `LakeShore.__init__`.

Serial settings are unusual and worth stating: the 335 uses **57600 baud,
7 data bits, odd parity, 1 stop bit**. 8-N-1 will connect and return nothing
intelligible, which looks like a dead instrument rather than a wrong setting.

UNITS ARE CELSIUS. `CRDG?` is used, not `KRDG?`. An earlier draft of
channels.yaml declared these channels Kelvin; the imported LabVIEW history then
showed them ranging from -90 to +21.8, and there is no negative Kelvin. They
also track the cryostat RTDs closely. See the note in channels.yaml.

A sensor that is disconnected or out of range makes the 335 return a reading
with a status flag rather than a number; `RDGST?` is read alongside so that is
reported as quality=error instead of a plausible-looking zero.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import serial

from ..bus import (ACK_LS_RANGE, ACK_LS_SETPOINT, TOPIC_AUDIT,
                   TOPIC_LS_RANGE, TOPIC_LS_SETPOINT)
from ..config import Config
from ..model import Measurement, Quality, iso, utcnow
from ..service import BaseService
from .serial_id import IdentityError, resolve

log = logging.getLogger(__name__)

# RANGE <output>,<n>. The 335 takes 0-3; the operator is offered off and high,
# which is what the procedure actually uses.
HEATER_RANGES = {"off": 0, "low": 1, "medium": 2, "high": 3}
RANGE_NAMES = {v: k for k, v in HEATER_RANGES.items()}

# THE ONLY WRITABLE OUTPUT. Output 2 is not used on this cryostat, so a
# command naming it is a mistake rather than an instruction, and is refused.
# It is still READ: an unused heater that starts doing something is exactly
# the surprise worth catching, and refusing to write it does not mean
# refusing to look at it.
WRITABLE_OUTPUTS = (1,)

# A setpoint that comes back different from what was sent means the write did
# not take. 0.01 C is far below anything that matters and far above the
# instrument's rounding.
SETPOINT_TOLERANCE_C = 0.01

# RDGST? bit meanings. Any non-zero value means the reading is not valid data.
READING_STATUS = {
    0: "invalid reading", 4: "temp underrange", 5: "temp overrange",
    6: "sensor units zero", 7: "sensor units overrange",
}


def describe_reading_status(word: int) -> str:
    flags = [name for bit, name in READING_STATUS.items() if word & (1 << bit)]
    return ", ".join(flags) if flags else "ok"


class LakeShore:
    """A thin wrapper over the 335's ASCII command set."""

    def __init__(self, port: str, baud: int = 57600):
        self.port = port
        self.baud = baud
        self._serial: serial.Serial | None = None
        # ONE CONVERSATION AT A TIME.
        #
        # A serial instrument has no request ids: a reply is matched to a
        # query only by being the next thing to arrive. Two threads talking at
        # once therefore do not merely interleave, they get each other's
        # answers — and the answers are plausible numbers, so nothing looks
        # wrong.
        #
        # This is not hypothetical. The control path arrived on the MQTT
        # callback thread while the 1 Hz poll loop was mid-query, and a
        # setpoint read back as -0.0, and as 52.3, which was the heater
        # percentage, and as -92.373, which was tt402. The read-back caught it
        # and refused the write, which is exactly what a read-back is for,
        # but the right answer is not to race in the first place.
        #
        # Reentrant, so a command can hold it across write-then-read-back and
        # still call query() underneath.
        self._lock = threading.RLock()

    def open(self) -> None:
        # 7-O-1: the 335's factory setting. Not a typo.
        self._serial = serial.Serial(
            self.port, self.baud, bytesize=serial.SEVENBITS,
            parity=serial.PARITY_ODD, stopbits=serial.STOPBITS_ONE,
            timeout=1.5, write_timeout=1.5)

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def query(self, command: str) -> str | None:
        """Send a query and return the reply, or None if it did not answer."""
        if self._serial is None:
            return None
        with self._lock:
            try:
                self._serial.reset_input_buffer()
                self._serial.write((command + "\r\n").encode("ascii"))
                time.sleep(0.05)
                raw = self._serial.read_until(b"\r\n", 200)
            except Exception as exc:
                log.debug("%s raised %s", command, exc)
                return None
        text = raw.decode("ascii", errors="replace").strip()
        if not text:
            log.debug("%s -> no reply", command)
            return None
        return text

    def send(self, command: str) -> bool:
        """Send a command that expects NO reply. See DESIGN.md §10.

        Separate from `query` on purpose. A command that writes and a command
        that asks are different things, and a driver where they share a method
        is one where a typo turns a question into an instruction.

        Returns whether the bytes went out — NOT whether the instrument did
        what was asked. Only a read-back can tell you that, and every caller
        here does one.
        """
        if self._serial is None:
            return False
        with self._lock:
            try:
                self._serial.reset_input_buffer()
                self._serial.write((command + "\r\n").encode("ascii"))
                time.sleep(0.1)      # the 335 needs a moment before a query
                return True
            except Exception as exc:
                log.warning("%s raised %s", command, exc)
                return False

    def number(self, command: str) -> float | None:
        text = self.query(command)
        if text is None:
            return None
        try:
            return float(text.split(",")[0])
        except ValueError:
            log.debug("%s -> non-numeric %r", command, text)
            return None

    def identity(self) -> tuple[str, str] | None:
        """(model, instrument serial) from *IDN?, or None.

        The 335 answers with four comma-separated fields:

            LSCI,MODEL335,335A12T/#######,3.2

        The third field is NOT one serial number. It is
        `<instrument serial>/<option card serial>`, and when no option card is
        fitted the second half is literally '#######'. Only the part before the
        slash identifies the instrument, and that is what matches the serial in
        its USB descriptor.

        Getting this wrong is not dangerous, because §6.2 rule 3 makes an
        unexpected identity a refusal rather than a guess — the service simply
        would not start. It cost one confused startup on 17 September 2026.
        """
        text = self.query("*IDN?")
        if not text:
            return None
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 3 or not parts[1]:
            return None
        instrument_serial = parts[2].split("/")[0].strip()
        if not instrument_serial:
            return None
        return (parts[1], instrument_serial)


class LakeShoreService(BaseService):
    name = "lakeshore"

    def __init__(self, config: Config, bus, simulate: bool = False, **kw):
        super().__init__(config, bus, simulate=simulate, **kw)
        self._spec = config.devices.get("lakeshore", {}) or {}
        self._device: LakeShore | None = None
        all_channels = config.channels_for("lakeshore")
        # Read channels come from the instrument; derived ones are computed
        # from a read channel after the fact.
        self._channels = [c for c in all_channels if c.derive is None]
        self._derived = [c for c in all_channels if c.derive is not None]

    def verify_identity(self) -> bool:
        if self.simulate:
            log.info("simulate=True: skipping hardware identity check")
            return True

        match = self._spec.get("match", {})
        vid, pid = match.get("vid"), match.get("pid")
        baud = int(self._spec.get("baud", 57600))
        wanted_model = str(self._spec.get("idn_contains", "MODEL335"))
        wanted_serial = str(match.get("serial", "")).strip()

        def ask(port: str) -> tuple[str, ...] | None:
            probe = LakeShore(port, baud)
            try:
                probe.open()
                return probe.identity()
            except Exception as exc:
                log.warning("could not probe %s: %s", port, exc)
                return None
            finally:
                probe.close()

        # The 335 exposes a USB serial number in its descriptor AND answers
        # *IDN?, so it is verified on both counts — unlike the CAEN units,
        # where the query is the only identification available (§6.2).
        try:
            probed: dict[str, tuple[str, ...]] = {}
            from .serial_id import candidates
            ports = candidates(vid, pid)
            if not ports:
                log.critical(
                    "FATAL: no serial port with USB id %s:%s. Is the Lake "
                    "Shore powered and connected?", vid, pid)
                return False
            for cand in ports:
                identity = ask(cand.port)
                if identity is not None:
                    probed[cand.port] = identity
                    log.info("%s reports model %s serial %s",
                             cand.port, identity[0], identity[1])

            if not probed:
                log.critical(
                    "FATAL: the Lake Shore did not answer *IDN? on any "
                    "candidate port. Check that nothing else holds the port "
                    "(LabVIEW?) and that the instrument is set to %d baud, "
                    "7 data bits, odd parity.", baud)
                return False

            expected = {"lakeshore": (wanted_model, wanted_serial)}
            # Fall back to matching on the model alone when devices.yaml does
            # not pin a serial, so the config can bootstrap itself (§6.2).
            if not wanted_serial:
                port = next(p for p, i in probed.items()
                            if wanted_model in i[0])
                mapping = {"lakeshore": port}
                log.warning("devices.yaml pins no Lake Shore serial number; "
                            "matched on model alone. Record the serial and "
                            "this becomes a full identity check.")
            else:
                mapping = resolve(vid, pid, ask, expected)
        except IdentityError as exc:
            log.critical("FATAL: %s", exc)
            return False
        except StopIteration:
            log.critical("FATAL: no port reported model containing %r",
                         wanted_model)
            return False

        self._device = LakeShore(mapping["lakeshore"], baud)
        try:
            self._device.open()
        except Exception as exc:
            log.critical("FATAL: could not open %s: %s", mapping["lakeshore"], exc)
            return False

        self._log_settings()
        return True

    def _log_settings(self) -> None:
        """Record the control settings at startup. Displayed, never written."""
        d = self._device
        if d is None:
            return
        for output in (1, 2):
            setpoint = d.query(f"SETP? {output}")
            heat_range = d.query(f"RANGE? {output}")
            pid = d.query(f"PID? {output}")
            log.info("output %d: setpoint=%s range=%s PID=%s",
                     output, setpoint, heat_range, pid)

    def read(self) -> list[Measurement]:
        if self.simulate:
            return self._read_simulated()

        d = self._device
        if d is None:
            raise RuntimeError("Lake Shore not connected")

        now = utcnow()
        out: list[Measurement] = []
        failures = 0

        for ch in self._channels:
            if ch.kind == "temperature":
                inp = ch.phys                      # 'A' or 'B'
                status = d.number(f"RDGST? {inp}")
                value = d.number(f"CRDG? {inp}")   # Celsius, see module docstring
                if value is None or status is None:
                    failures += 1
                    out.append(Measurement(t=now, channel=ch.name, value=None,
                                           unit=ch.unit, quality=Quality.ERROR))
                    continue
                if int(status) != 0:
                    log.warning("%s: %s", ch.name, describe_reading_status(int(status)))
                    out.append(Measurement(t=now, channel=ch.name, value=None,
                                           unit=ch.unit, quality=Quality.ERROR))
                    continue
                out.append(Measurement(t=now, channel=ch.name, value=value,
                                       unit=ch.unit, raw=value, quality=Quality.OK))
            elif ch.kind == "setpoint":
                # What the instrument has been TOLD to hold. A query, never a
                # write: there is no SET path in this driver, by design.
                value = d.number(f"SETP? {ch.phys}")
                if value is None:
                    failures += 1
                    out.append(Measurement(t=now, channel=ch.name, value=None,
                                           unit=ch.unit, quality=Quality.ERROR))
                    continue
                out.append(Measurement(t=now, channel=ch.name, value=value,
                                       unit=ch.unit, raw=value,
                                       quality=Quality.OK))
            else:
                # Heater output, as a percentage.
                value = d.number(f"HTR? {ch.phys}")
                if value is None:
                    failures += 1
                    out.append(Measurement(t=now, channel=ch.name, value=None,
                                           unit=ch.unit, quality=Quality.ERROR))
                    continue
                out.append(Measurement(t=now, channel=ch.name, value=value,
                                       unit=ch.unit, raw=value, quality=Quality.OK))

        if out and failures == len(out):
            raise RuntimeError("Lake Shore did not answer any query; link lost")

        out.extend(self._derive_from(out, now))
        return out

    def _derive_from(self, readings: list[Measurement],
                     now) -> list[Measurement]:
        """Compute the derived channels from the readings just taken.

        Same timestamp as their source, deliberately: a derived value and the
        reading it came from must line up exactly, or a plot of the two will
        show a phantom lag.

        A derived channel inherits its source's quality. If the heater
        percentage could not be read, its wattage is not zero — it is unknown.
        """
        from ..scaling import TRANSFORMS

        by_name = {m.channel: m for m in readings}
        out = []
        for ch in self._derived:
            source = by_name.get(ch.derive["from"])
            if source is None:
                continue
            if source.quality is not Quality.OK or source.value is None:
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, quality=source.quality))
                continue
            params = {k: v for k, v in ch.derive.items()
                      if k not in ("from", "transform")}
            try:
                value = TRANSFORMS[ch.derive["transform"]](source.value, **params)
            except Exception:
                log.exception("could not derive %s from %s", ch.name, source.channel)
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, quality=Quality.ERROR))
                continue
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=source.value,
                                   quality=Quality.OK, src=source.src))
        return out

    def _read_simulated(self) -> list[Measurement]:
        import random
        now = utcnow()
        out = []
        for ch in self._channels:
            value = (random.gauss(-90.0, 0.2) if ch.kind == "temperature"
                     else abs(random.gauss(50.0, 2.0)))
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=value,
                                   quality=Quality.OK, src="sim"))
        return out

    # ------------------------------------------------------------- control
    #
    # The first write path in this system (DESIGN.md section 10). Every
    # command takes the same four steps, and none is optional:
    #
    #   1. VALIDATE    connected, writable output, value inside channels.yaml
    #   2. WRITE       one SCPI command
    #   3. READ BACK   ask the instrument what it now holds
    #   4. ACK + AUDIT say what happened, and record it
    #
    # Step 3 is the one that is easy to skip and must not be. `send` reports
    # that bytes left the port, which is NOT the instrument having obeyed: a
    # framing error, a value the 335 rejects, or the front panel being used a
    # second earlier all produce a successful write and the wrong state.
    # Reporting success for a write that did not take is the failure mode
    # worth the most effort to avoid here.

    def _audit(self, action, target, old, new, result, detail="", actor=""):
        """Publish an audit record for a sink to store (section 2.1, rule 5).

        Published rather than written: an audit record then survives the
        database being down, because it is in the JSONL archive either way.
        """
        self.bus.publish_raw(TOPIC_AUDIT, json.dumps(
            {"t": iso(utcnow()), "actor": actor or "unknown", "action": action,
             "target": target, "old": None if old is None else str(old),
             "new": None if new is None else str(new),
             "result": result, "detail": detail}, separators=(",", ":")))

    def _reject(self, ack_topic, action, target, reason, actor, old=None,
                new=None):
        """Refuse a command, out loud.

        A rejected command is acknowledged with a reason, never silently
        dropped (section 10) - and it is audited, because an attempted write
        that was refused is exactly as interesting afterwards as one that
        succeeded.
        """
        log.warning("refused %s by %s: %s", action, actor or "unknown", reason)
        self.bus.publish_raw(ack_topic, json.dumps(
            {"ok": False, "reason": reason, "by": actor},
            separators=(",", ":")))
        self._audit(action, target, old, new, "rejected", reason, actor)
        return None

    def _check_writable(self, payload, ack_topic, action):
        """Validation shared by both commands.

        Returns (command, output, actor), or None if it was refused.
        """
        try:
            d = json.loads(payload) or {}
        except ValueError:
            return self._reject(ack_topic, action, None,
                                "unparseable command", "")
        actor = str(d.get("by") or "").strip() or "unknown"
        try:
            output = int(d.get("output", 1))
        except (TypeError, ValueError):
            return self._reject(ack_topic, action, None,
                                "output is not a number", actor)
        if output not in WRITABLE_OUTPUTS:
            return self._reject(
                ack_topic, action, "output %s" % output,
                "output %d is not writable on this cryostat; only %s is"
                % (output, ", ".join(str(o) for o in WRITABLE_OUTPUTS)), actor)
        if self.simulate:
            return self._reject(ack_topic, action, "output %d" % output,
                                "this service is simulating and holds no "
                                "instrument to write to", actor)
        if self._device is None:
            return self._reject(ack_topic, action, "output %d" % output,
                                "not connected to the instrument", actor)
        return d, output, actor

    def _handle_setpoint(self, topic: str, payload: str) -> None:
        action = "lakeshore_setpoint"
        checked = self._check_writable(payload, ACK_LS_SETPOINT, action)
        if checked is None:
            return
        d, output, actor = checked
        target = "ls_setpoint_%d" % output

        try:
            wanted = float(d["value"])
        except (KeyError, TypeError, ValueError):
            self._reject(ACK_LS_SETPOINT, action, target,
                         "no usable 'value' in the command", actor)
            return

        # The write range from channels.yaml. A channel with no limits refuses
        # everything, and that is the intended default: a range nobody wrote
        # down is not permission to write anything (section 8.1).
        channel = self.config.channels.get(target)
        if channel is None or not channel.enabled:
            self._reject(ACK_LS_SETPOINT, action, target,
                         "%s is not an enabled channel" % target, actor)
            return
        if not channel.in_limits(wanted):
            limits = channel.limits or {}
            self._reject(
                ACK_LS_SETPOINT, action, target,
                "%.3f C is outside the permitted range %s to %s set in "
                "channels.yaml" % (wanted, limits.get("min", "unset"),
                                   limits.get("max", "unset")),
                actor, new=wanted)
            return

        device = self._device
        # Held across read-old / write / read-back, so the 1 Hz poll cannot
        # land in the middle and hand us its reply instead of ours.
        with device._lock:
            before = device.number("SETP? %d" % output)
            sent = device.send("SETP %d,%.3f" % (output, wanted))
            after = device.number("SETP? %d" % output) if sent else None
        if not sent:
            self._reject(ACK_LS_SETPOINT, action, target,
                         "the command could not be sent", actor,
                         old=before, new=wanted)
            return

        if after is None or abs(after - wanted) > SETPOINT_TOLERANCE_C:
            self._reject(
                ACK_LS_SETPOINT, action, target,
                "read back %s after asking for %.3f C; the write did not take"
                % (after, wanted), actor, old=before, new=wanted)
            return

        log.warning("setpoint %d: %s -> %.3f C, by %s",
                    output, before, after, actor)
        self.bus.publish_raw(ACK_LS_SETPOINT, json.dumps(
            {"ok": True, "output": output, "old": before, "new": after,
             "by": actor}, separators=(",", ":")))
        self._audit(action, target, before, after, "ok", "", actor)

    def _handle_range(self, topic: str, payload: str) -> None:
        action = "lakeshore_range"
        checked = self._check_writable(payload, ACK_LS_RANGE, action)
        if checked is None:
            return
        d, output, actor = checked
        target = "ls_heater_%d" % output

        name = str(d.get("range", "")).strip().lower()
        if name not in HEATER_RANGES:
            self._reject(ACK_LS_RANGE, action, target,
                         "unknown heater range %r; expected one of %s"
                         % (name, ", ".join(sorted(HEATER_RANGES))), actor)
            return
        wanted = HEATER_RANGES[name]

        device = self._device
        with device._lock:
            before = device.number("RANGE? %d" % output)
            sent = device.send("RANGE %d,%d" % (output, wanted))
            after = device.number("RANGE? %d" % output) if sent else None
        before_name = (RANGE_NAMES.get(int(before), before)
                       if before is not None else None)
        if not sent:
            self._reject(ACK_LS_RANGE, action, target,
                         "the command could not be sent", actor,
                         old=before_name, new=name)
            return

        if after is None or int(after) != wanted:
            got = (RANGE_NAMES.get(int(after), after)
                   if after is not None else None)
            self._reject(
                ACK_LS_RANGE, action, target,
                "read back %s after asking for %s; the write did not take"
                % (got, name), actor, old=before_name, new=name)
            return

        log.warning("heater range %d: %s -> %s, by %s",
                    output, before_name, name, actor)
        self.bus.publish_raw(ACK_LS_RANGE, json.dumps(
            {"ok": True, "output": output, "old": before_name, "new": name,
             "by": actor}, separators=(",", ":")))
        self._audit(action, target, before_name, name, "ok", "", actor)

    def run(self) -> int:
        self.bus.subscribe(TOPIC_LS_SETPOINT, self._handle_setpoint)
        self.bus.subscribe(TOPIC_LS_RANGE, self._handle_range)
        return super().run()

    def reconnect(self) -> bool:
        """Re-resolve the port and re-confirm *IDN? (§6.2 rule 6)."""
        if self.simulate:
            return True
        if self._device is not None:
            self._device.close()
            self._device = None
        return self.verify_identity()

    def close(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None
        log.info("Lake Shore port released")
