"""CAEN DT1470ET high-voltage supplies. See DESIGN.md §7.2.

Two units, both on USB, ASCII protocol over the virtual COM port. No vendor
library.

**This driver writes exactly three things: `VSET`, `ON` and `OFF`** (§10a).
Everything else is read. There is deliberately no code here that can change
`MAXV`, `RUP`, `RDW`, `TRIP` or `ISET` - protection stays configured on the
instrument (§10 rule 2).

**`ON` is not the enable switch, and the difference matters.** Three separate
things decide whether there are volts on an electrode:

  * the **enable switch** on the front panel clears the `DISABLED` bit. It is
    a hand operation and no command here can change it.
  * **`ON`/`OFF`** energises a channel that is already enabled. That is the
    "turn on the HV" step, and it is this driver's business.
  * **`VSET`** decides where it ramps to.

Clearing `DISABLED` alone does nothing visible, which is exactly what was
observed on 18 September 2026 when the `nai` enable was flipped and the
channel sat at `STAT=0`: permitted, but not energised.

A write is validated against `channels.yaml`, refused outright if it would put
a non-zero setpoint on a channel that is not enabled (§10a), **read back from
the board before it is called successful**, acknowledged on the bus and
recorded in the audit trail.

Both units answer at **board address 0**: they are two independent USB
connections, not a daisy chain. They are told apart by which port their
`BDSNUM` came back on, never by address and never by COM number (§6.2).

SIGN CONVENTION. The supplies report `VMON` and `VSET` as **unsigned
magnitudes**, with polarity as a separate `POL` parameter. A cathode at minus
2250 volts answers "2250.0". The sign is applied here, once, and values are
stored signed (§7.2). This is almost certainly what the LabVIEW
`DAISY_polarity_signs.vi` exists to do.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import serial

from ..bus import (ACK_HV_OUTPUT, ACK_HV_VSET, TOPIC_AUDIT, TOPIC_HV_OUTPUT,
                   TOPIC_HV_VSET)
from ..config import Config
from ..model import Measurement, Quality, iso, utcnow
from ..scaling import apply_sign, magnitude_for
from ..service import BaseService
from .serial_id import IdentityError, resolve

log = logging.getLogger(__name__)

# The status word and its decoding live in hv_status.py: they are pure logic
# and the web UI needs them, and this module imports `serial`. Re-exported
# here because this is where anyone would look for them.
from ..hv_status import (  # noqa: F401
    BIT_DISABLED, BIT_ON, FAULT_BITS, STAT_BITS, describe_status, is_disabled,
    is_energised, status_faults)

# A setpoint that reads back further than this from what was asked means the
# write did not take. 0.5 V is far below anything that matters on a kilovolt
# electrode and well above the board's own rounding.
VSET_TOLERANCE_V = 0.5


def _decode(response: str) -> str | None:
    """Extract VAL from '#BD:00,CMD:OK,VAL:19198'.

    Returns None for anything malformed, absent or reporting an error. A
    malformed reply is a read failure, never a silently substituted value
    (§7.2).
    """
    if not response:
        return None
    response = response.strip()
    if not response.startswith("#BD:") or "CMD:OK" not in response:
        return None
    _, _, tail = response.partition("VAL:")
    return tail.strip() or None


class CaenChannelReader:
    """One supply: a port, a board address, and the monitor commands."""

    def __init__(self, device_id: str, port: str, address: int, baud: int,
                 board_name: str, board_serial: str):
        self.device_id = device_id
        self.port = port
        self.address = address
        self.baud = baud
        self.board_name = board_name
        self.board_serial = board_serial
        self._serial: serial.Serial | None = None
        # ONE CONVERSATION AT A TIME, for the same reason as the Lake Shore.
        # A serial instrument matches a reply to a query only by arrival
        # order, so the 1 Hz poll and a command arriving on the MQTT thread
        # hand each other their answers - and the answers are plausible
        # numbers, so nothing looks wrong. On the Lake Shore, on 18 September
        # 2026, a setpoint read back as the heater percentage. Reentrant, so a
        # command can hold it across write-then-read-back.
        self._lock = threading.RLock()

    # ----------------------------------------------------------------- serial

    def open(self) -> None:
        self._serial = serial.Serial(self.port, self.baud, bytesize=8,
                                     parity=serial.PARITY_NONE, stopbits=1,
                                     timeout=1.5, write_timeout=1.5)

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def _talk(self, cmd: str) -> str | None:
        """Send one command, return the RAW reply, or None if it did not answer.

        Shared by MON and SET: the framing, the lock and the retry are
        identical, and two copies of them would drift.
        """
        if self._serial is None:
            return None
        with self._lock:
            try:
                self._serial.reset_input_buffer()
                self._serial.write((cmd + "\r\n").encode("ascii"))
                time.sleep(0.05)
                raw = self._serial.read_until(b"\r\n", 200).decode(
                    "ascii", errors="replace")
                if not raw.strip():
                    # Some firmware answers slowly; one short retry.
                    time.sleep(0.15)
                    raw = self._serial.read(200).decode("ascii", errors="replace")
            except Exception as exc:
                log.debug("%s: %s raised %s", self.device_id, cmd, exc)
                return None
        return raw

    def _command(self, par: str, channel: int | None = None) -> str | None:
        """Read one parameter with CMD:MON, returning its VAL."""
        cmd = f"$BD:{self.address},CMD:MON,PAR:{par}"
        if channel is not None:
            cmd += f",CH:{channel}"
        raw = self._talk(cmd)
        value = _decode(raw) if raw is not None else None
        if value is None:
            # The raw instrument response, not just the parsed failure - that
            # is what makes protocol bugs findable (section 12).
            log.debug("%s: %s -> unparseable %r", self.device_id, cmd, raw)
        return value

    def set_voltage(self, channel: int, magnitude: float) -> bool:
        """Write VSET for one channel. THE ONLY WRITE IN THIS DRIVER.

        `magnitude` is unsigned, as the supply expects. The caller converts
        from the signed value with `scaling.magnitude_for`, which refuses the
        wrong polarity rather than silently taking its absolute value.

        Returns whether the board ACKNOWLEDGED the command - not whether it is
        now holding that value. Only a read-back establishes that, and every
        caller here does one. A value above the board's own MAXV is refused by
        the instrument, which is the protection working as intended.

        Note the reply shape: a successful SET answers `#BD:00,CMD:OK` with no
        `VAL:` field, so `_decode` returns None for it. Success is therefore
        the presence of CMD:OK, not the presence of a value - which is why
        this does not go through `_command`.
        """
        if magnitude < 0:
            raise ValueError("set_voltage takes an unsigned magnitude")
        raw = self._talk(f"$BD:{self.address},CMD:SET,PAR:VSET,"
                         f"CH:{channel},VAL:{magnitude:.1f}")
        if raw is None:
            log.warning("%s ch%d: no reply to SET VSET", self.device_id, channel)
            return False, "the supply did not answer"

        reply = raw.strip()
        if reply.startswith("#BD:") and "CMD:OK" in reply:
            return True, ""

        log.warning("%s ch%d: the board refused SET VSET: %r",
                    self.device_id, channel, reply)

        # The board says WHY, and the reasons are not interchangeable.
        # LOC:ERR means it is in LOCAL mode: the front panel has control and
        # every remote SET is refused, while MON keeps working perfectly -
        # which is why every reading looked fine and only writing failed.
        if "LOC:ERR" in reply:
            return False, ("the supply is in LOCAL mode, so it refuses every "
                           "remote setpoint. Put the board in REMOTE at its "
                           "front panel; nothing in this software can do it, "
                           "by design")
        if "CMD:ERR" in reply:
            return False, "the supply rejected the command itself (%s)" % reply
        if "VAL:ERR" in reply:
            return False, ("the supply rejected the value, most likely above "
                           "its own MAXV (%s)" % reply)
        return False, "the supply refused it: %s" % reply

    # --------------------------------------------------------------- identity

    def identity(self) -> tuple[str, str] | None:
        """(BDNAME, BDSNUM), or None if the board did not answer properly."""
        name = self._command("BDNAME")
        serial_num = self._command("BDSNUM")
        if name is None or serial_num is None:
            return None
        return (name, serial_num)

    # ------------------------------------------------------------------ reads

    def monitor(self, channel: int, par: str) -> float | None:
        value = self._command(par, channel)
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            log.debug("%s ch%d %s: non-numeric %r", self.device_id, channel, par, value)
            return None

    def set_output(self, channel: int, on: bool) -> tuple[bool, str]:
        """Energise or de-energise one channel. NOT the enable switch.

        A channel whose `DISABLED` bit is set cannot be energised by this -
        the enable is a hand operation, and the board refuses. The caller
        checks first so the refusal names the remedy rather than the symptom.
        """
        par = "ON" if on else "OFF"
        raw = self._talk(f"$BD:{self.address},CMD:SET,PAR:{par},CH:{channel}")
        if raw is None:
            return False, "the supply did not answer"
        reply = raw.strip()
        if reply.startswith("#BD:") and "CMD:OK" in reply:
            return True, ""
        log.warning("%s ch%d: the board refused %s: %r",
                    self.device_id, channel, par, reply)
        if "LOC:ERR" in reply:
            return False, ("the supply is in LOCAL mode, so it refuses remote "
                           "commands. Switch the board to REMOTE at its front "
                           "panel; nothing here can do it, by design")
        return False, "the supply refused it: %s" % reply

    def control_mode(self) -> str:
        """LOCAL or REMOTE, from the board's own BDCTR.

        In LOCAL the front panel has control and every remote SET is refused
        with LOC:ERR, while MON keeps answering normally. That asymmetry is
        why a board in LOCAL is indistinguishable from a working one until
        somebody tries to write.
        """
        return (self._command("BDCTR") or "unknown").strip().upper()

    def status(self, channel: int) -> int | None:
        value = self._command("STAT", channel)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None


class CaenService(BaseService):
    """Reads both supplies. One service, two instruments — they share a
    protocol and a resolution pass, and splitting them would mean two
    processes racing to enumerate the same ports."""

    name = "caen"

    def __init__(self, config: Config, bus, simulate: bool = False, **kw):
        super().__init__(config, bus, simulate=simulate, **kw)
        self._readers: dict[str, CaenChannelReader] = {}
        self._specs = config.devices.get("caen", []) or []
        # phys is the 0-based channel index on its board.
        self._channels = [c for c in config.enabled_channels()
                          if c.device in {s["id"] for s in self._specs}]
        # Consecutive read cycles in which a given supply answered nothing.
        # Tracked PER DEVICE: two supplies share this service, and one can
        # lose its USB while the other keeps answering perfectly.
        self._link_down: dict[str, int] = {}
        self._next_relink = 0.0

    # --------------------------------------------------------------- identity

    def _probe(self, port: str) -> tuple[str, ...] | None:
        """Open a port, ask whoever is there who they are, and let go."""
        address = int(self._specs[0].get("board_address", 0))
        baud = int(self._specs[0].get("baud", 9600))
        reader = CaenChannelReader("probe", port, address, baud, "", "")
        try:
            reader.open()
            return reader.identity()
        except Exception as exc:
            log.warning("could not probe %s: %s", port, exc)
            return None
        finally:
            reader.close()

    def verify_identity(self) -> bool:
        if self.simulate:
            log.info("simulate=True: skipping hardware identity check")
            return True

        by_vidpid: dict[tuple[str, str], dict] = {}
        for spec in self._specs:
            key = (spec["match"]["vid"], spec["match"]["pid"])
            by_vidpid.setdefault(key, {})[spec["id"]] = (
                spec["board_name"], str(spec["board_serial"]))

        try:
            for (vid, pid), expected in by_vidpid.items():
                probes: dict[str, CaenChannelReader] = {}

                mapping = resolve(vid, pid, self._probe, expected)

                for spec in self._specs:
                    if spec["id"] not in mapping:
                        continue
                    self._readers[spec["id"]] = CaenChannelReader(
                        device_id=spec["id"], port=mapping[spec["id"]],
                        address=int(spec.get("board_address", 0)),
                        baud=int(spec.get("baud", 9600)),
                        board_name=spec["board_name"],
                        board_serial=str(spec["board_serial"]))
        except IdentityError as exc:
            log.critical("FATAL: %s", exc)
            return False

        for device_id, reader in self._readers.items():
            try:
                reader.open()
            except Exception as exc:
                log.critical("FATAL: could not open %s on %s: %s",
                             device_id, reader.port, exc)
                return False
            log.info("%s: %s serial %s on %s, verified",
                     device_id, reader.board_name, reader.board_serial, reader.port)
            # LOCAL or REMOTE. Logged at startup because it decides whether
            # any setpoint can be written at all, and a board quietly in
            # LOCAL looks identical to a working one until the first write.
            mode = reader.control_mode()
            log.info("%s: control mode %s%s", device_id, mode,
                     "" if mode == "REMOTE" else
                     " - setpoints cannot be written until the front panel "
                     "is switched to REMOTE")
            self._check_expectations(device_id, reader)

        return True

    def _check_expectations(self, device_id: str, reader: CaenChannelReader) -> None:
        """Compare the board's protection settings against devices.yaml.

        The software DISPLAYS and ALARMS on these; it never writes them (§8.3).
        A mismatch means somebody changed a limit on the front panel, which is
        worth knowing about and is not this system's business to correct.
        """
        spec = next((s for s in self._specs if s["id"] == device_id), None)
        expect = (spec or {}).get("expect") or {}
        for channel, wanted in expect.items():
            channel = int(channel)
            for par, want in wanted.items():
                if par == "pol":
                    got = reader._command("POL", channel)
                    ok = got is not None and got.strip().startswith(str(want))
                else:
                    got = reader.monitor(channel, par.upper())
                    ok = got is not None and abs(got - float(want)) <= 0.51
                if not ok:
                    log.warning(
                        "%s ch%d: %s is %s, devices.yaml expects %s. The "
                        "software does not change instrument limits (§8.3) — "
                        "check the front panel.",
                        device_id, channel, par.upper(), got, want)

    # ------------------------------------------------------------------- read

    def read(self) -> list[Measurement]:
        if self.simulate:
            return self._read_simulated()

        now = utcnow()
        out: list[Measurement] = []
        # False until a channel on that supply answers. A supply whose reader
        # is missing entirely - a relink that has not found it yet - stays
        # False, and its channels report error rather than going quietly
        # stale.
        alive = {c.device: False for c in self._channels}

        for ch in self._channels:
            reader = self._readers.get(ch.device)
            if reader is None:
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, quality=Quality.ERROR))
                continue
            index = int(ch.phys)

            if ch.kind == "hv_stat":
                # The status word is a bitmask, not a measurement: stored as
                # it comes off the wire so every flag survives, and decoded
                # where it is displayed.
                word = reader.status(index)
                if word is None:
                    out.append(Measurement(t=now, channel=ch.name, value=None,
                                           unit=ch.unit, quality=Quality.ERROR))
                    continue
                alive[ch.device] = True
                out.append(Measurement(t=now, channel=ch.name,
                                       value=float(word), unit=ch.unit,
                                       raw=float(word), quality=Quality.OK))
                continue

            # VSET is a set parameter, but it is READ here with the ordinary
            # monitor command - CMD:MON,PAR:VSET - which is what makes stage 1
            # of section 10a a read-only change. There is still no SET path in
            # this driver.
            par = {"hv_vmon": "VMON", "hv_vset": "VSET"}.get(ch.kind, "IMON")
            magnitude = reader.monitor(index, par)

            if magnitude is None:
                # The device is there but did not answer intelligibly. Publish
                # the absence, never a substituted value.
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, quality=Quality.ERROR))
                continue

            alive[ch.device] = True
            # Both VMON and VSET come back as unsigned magnitudes with POL
            # separate, and both are stored signed (section 7.2). IMON is a
            # current and has no polarity to apply.
            value = (apply_sign(magnitude, ch.sign)
                     if ch.kind in ("hv_vmon", "hv_vset") else magnitude)
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=magnitude, quality=Quality.OK))

        if out and not any(alive.values()):
            # Nothing anywhere answered: the link is gone, not the readings.
            # Raise so BaseService backs off and retries (§6.1).
            raise RuntimeError("no CAEN channel answered; link lost")

        # ONE supply can lose its USB while the other keeps answering, and it
        # must still be recovered. This used to require EVERY channel in the
        # service to fail, so unplugging a single unit left it publishing
        # errors forever with its healthy neighbour holding the service up -
        # and nothing in the log, because a failed command is logged at debug.
        # Liveness is tracked per device because the link is per device.
        for device_id, ok in alive.items():
            if ok:
                if self._link_down.get(device_id):
                    log.info("%s: answering again", device_id)
                self._link_down[device_id] = 0
                continue
            self._link_down[device_id] = self._link_down.get(device_id, 0) + 1
            if self._link_down[device_id] == 2:
                log.warning("%s stopped answering; its channels now read "
                            "error and a relink will be attempted", device_id)

        if any(n >= 2 for n in self._link_down.values()):
            self._relink()
        return out

    def _read_simulated(self) -> list[Measurement]:
        import random
        now = utcnow()
        out = []
        for ch in self._channels:
            if ch.kind == "hv_stat":
                # 1 = ON. A simulated supply is on, so the page shows the
                # interesting case rather than a wall of DISABLED.
                out.append(Measurement(t=now, channel=ch.name, value=1.0,
                                       unit=ch.unit, raw=1.0,
                                       quality=Quality.OK, src="sim"))
                continue
            if ch.kind in ("hv_vmon", "hv_vset") and ch.limits:
                span = ch.limits["max"] if ch.sign > 0 else ch.limits["min"]
                magnitude = abs(span) * 0.8 + random.gauss(0, 1.0)
                value = apply_sign(magnitude, ch.sign)
            else:
                magnitude = value = abs(random.gauss(0.5, 0.05))
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=magnitude,
                                   quality=Quality.OK, src="sim"))
        return out

    # ------------------------------------------------------------- control
    #
    # THE ONLY WRITE PATH IN THIS DRIVER (section 10a). It writes VSET and
    # nothing else. There is no command here that enables or disables a
    # channel: that is a hand operation at the supply, and a hardware gate
    # the software cannot reach is the last thing standing between a bug and
    # an electrode.

    def _audit(self, target, old, new, result, detail="", actor="",
               action="caen_vset"):
        self.bus.publish_raw(TOPIC_AUDIT, json.dumps(
            {"t": iso(utcnow()), "actor": actor or "unknown",
             "action": action, "target": target,
             "old": None if old is None else str(old),
             "new": None if new is None else str(new),
             "result": result, "detail": detail}, separators=(",", ":")))

    def _refuse(self, target, reason, actor, old=None, new=None,
                ack_topic=None, action="caen_vset"):
        """Say no, out loud, and record it.

        A refused command is acknowledged with a reason and audited, never
        dropped (section 10). What somebody TRIED to put on an electrode is
        worth as much afterwards as what they managed to.
        """
        log.warning("refused %s on %s by %s: %s", action, target,
                    actor or "unknown", reason)
        self.bus.publish_raw(ack_topic or ACK_HV_VSET, json.dumps(
            {"ok": False, "channel": target, "reason": reason, "by": actor},
            separators=(",", ":")))
        self._audit(target, old, new, "rejected", reason, actor, action=action)

    def _handle_vset(self, topic: str, payload: str) -> None:
        try:
            command = json.loads(payload) or {}
        except ValueError:
            self._refuse(None, "unparseable command", "")
            return

        actor = str(command.get("by") or "").strip() or "unknown"
        name = str(command.get("channel") or "").strip()

        channel = self.config.channels.get(name)
        if channel is None or channel.kind != "hv_vset" or not channel.enabled:
            self._refuse(name or None,
                         "%r is not an enabled hv_vset channel" % name, actor)
            return

        try:
            wanted = float(command["value"])
        except (KeyError, TypeError, ValueError):
            self._refuse(name, "no usable 'value' in the command", actor)
            return

        if self.simulate:
            self._refuse(name, "this service is simulating and holds no "
                               "instrument to write to", actor, new=wanted)
            return

        reader = self._readers.get(channel.device)
        if reader is None:
            self._refuse(name, "not connected to %s" % channel.device, actor,
                         new=wanted)
            return

        # The software write range from channels.yaml. A channel with no
        # limits refuses everything, which is the intended default: a range
        # nobody wrote down is not permission to put volts on an electrode.
        if not channel.in_limits(wanted):
            limits = channel.limits or {}
            self._refuse(name, "%+.1f V is outside the permitted range %s to "
                               "%s set in channels.yaml"
                               % (wanted, limits.get("min", "unset"),
                                  limits.get("max", "unset")), actor,
                         new=wanted)
            return

        index = int(channel.phys)

        # THE SECTION 10a RULE. A channel that is not enabled must keep VSET 0,
        # because the enable is a hand operation and the board ramps to VSET
        # the instant it is flipped. Allowing a non-zero setpoint on a
        # disabled channel would let somebody stage 4.2 kV, walk to the
        # supply, flip the switch and get exactly the unannounced ramp this
        # whole design exists to prevent - with the software's blessing.
        #
        # Zero is always allowed. That is how the invariant gets established
        # on a channel whose stored setpoint is currently wrong.
        word = reader.status(index)
        if word is None:
            self._refuse(name, "could not read the channel's status word, so "
                               "whether it is enabled is unknown", actor,
                         new=wanted)
            return
        # is_disabled (bit 10, the front-panel switch), NOT is_energised
        # (bit 0, the output being energised). This first read bit 0 and so
        # refused a setpoint to any channel that was switched on but not yet
        # energised - which is precisely the state the operator is in between
        # flipping the switch and pressing turn-on, and precisely when they
        # want to load a setpoint.
        #
        # The invariant is about the SWITCH: a channel whose switch is off
        # keeps VSET 0, because flipping it would ramp straight there. Once
        # the switch is on, a setpoint may be loaded; energising is a separate,
        # explicit act that reports the voltage it will ramp to.
        if wanted != 0 and is_disabled(word):
            self._refuse(
                name,
                "this channel is disabled at the supply, so its setpoint must "
                "stay at 0 (section 10a). Flip its enable switch first - the "
                "channel stays at zero volts until it is energised - then set "
                "the voltage.", actor, new=wanted)
            return

        try:
            magnitude = magnitude_for(wanted, channel.sign)
        except ValueError as exc:
            self._refuse(name, str(exc), actor, new=wanted)
            return

        # Held across read-old / write / read-back, so the 1 Hz poll cannot
        # land in the middle and hand us its reply instead of ours.
        with reader._lock:
            before_mag = reader.monitor(index, "VSET")
            sent, why = reader.set_voltage(index, magnitude)
            after_mag = reader.monitor(index, "VSET") if sent else None

        before = (apply_sign(before_mag, channel.sign)
                  if before_mag is not None else None)
        if not sent:
            self._refuse(name, why, actor, old=before, new=wanted)
            return

        if after_mag is None:
            self._refuse(name, "the setpoint could not be read back, so "
                               "whether the write took is unknown", actor,
                         old=before, new=wanted)
            return

        after = apply_sign(after_mag, channel.sign)
        if abs(after - wanted) > VSET_TOLERANCE_V:
            self._refuse(
                name, "read back %+.1f V after asking for %+.1f V; the write "
                      "did not take. A value above the board's own MAXV is "
                      "refused by the instrument, which is the protection "
                      "working." % (after, wanted), actor, old=before,
                new=wanted)
            return

        log.warning("VSET %s: %s -> %+.1f V, by %s", name,
                    ("%+.1f" % before) if before is not None else "unknown",
                    after, actor)
        self.bus.publish_raw(ACK_HV_VSET, json.dumps(
            {"ok": True, "channel": name, "old": before, "new": after,
             "by": actor}, separators=(",", ":")))
        self._audit(name, before, after, "ok", "", actor)

    def _resolve(self, name, ack_topic, actor):
        """(channel, reader, index) for an hv_vset name, or None after refusing."""
        channel = self.config.channels.get(name)
        if channel is None or channel.kind != "hv_vset" or not channel.enabled:
            self._refuse(name or None,
                         "%r is not an enabled hv_vset channel" % name, actor,
                         ack_topic=ack_topic, action="caen_output")
            return None
        if self.simulate:
            self._refuse(name, "this service is simulating and holds no "
                               "instrument to write to", actor,
                         ack_topic=ack_topic, action="caen_output")
            return None
        reader = self._readers.get(channel.device)
        if reader is None:
            self._refuse(name, "not connected to %s" % channel.device, actor,
                         ack_topic=ack_topic, action="caen_output")
            return None
        return channel, reader, int(channel.phys)

    def _stat_channel_for(self, channel):
        """The hv_stat channel watching the same physical output, or None.

        Matched on device AND phys rather than by rewriting the name: the
        naming convention is a convention, and config.py already guarantees
        that one (device, phys) pair is used once per kind (§6.1).
        """
        for ch in self._channels:
            if (ch.kind == "hv_stat" and ch.device == channel.device
                    and int(ch.phys) == int(channel.phys)):
                return ch
        return None

    def _publish_status_now(self, channel, word: int) -> None:
        """Publish a status word read back from a write, out of cadence.

        WHY THIS EXISTS. The poll reads every second and publishes every ten,
        so a channel energised just after a publish stayed "not energised" on
        the web page for most of the following ten seconds - the operator saw
        nothing happen, and clicked the button again. By then the row may
        have refreshed into "turn off", so the second click de-energised the
        channel the first had just started. The read-back is already in hand
        here and was already trusted enough to verify the write against; the
        only thing missing was saying so.

        The window is dropped for this channel deliberately. It holds samples
        from BOTH sides of the switch, and _emit_window means them - a mean of
        a bitmask is not a bitmask, and `int(0.4)` is 0, so that aggregate
        would have published the channel as off again a few seconds after this
        said it was on. Ten seconds of samples are lost at the instant of a
        command; the ack and the audit record the transition exactly, and a
        meaningless average is not worth keeping over that.
        """
        stat = self._stat_channel_for(channel)
        if stat is None:
            return
        self._window.pop(stat.name, None)
        self.bus.publish_measurement(
            Measurement(t=utcnow(), channel=stat.name, value=float(word),
                        unit=stat.unit, raw=float(word), quality=Quality.OK))

    def _handle_output(self, topic: str, payload: str) -> None:
        """Energise or de-energise a channel - the "turn ON HV" of section 10a.

        The safety of this rests on one thing: **it says what will happen.**
        Turning a channel on ramps it to whatever VSET holds, so the
        acknowledgement carries that voltage. Turning on at VSET 0 - which is
        the resting state the invariant guarantees - energises at zero and
        moves nothing, and that is a perfectly reasonable thing to do.
        """
        try:
            command = json.loads(payload) or {}
        except ValueError:
            self._refuse(None, "unparseable command", "",
                         ack_topic=ACK_HV_OUTPUT, action="caen_output")
            return

        actor = str(command.get("by") or "").strip() or "unknown"
        name = str(command.get("channel") or "").strip()
        if "on" not in command:
            self._refuse(name or None, "the command must say on: true or false",
                         actor, ack_topic=ACK_HV_OUTPUT, action="caen_output")
            return
        wanted_on = bool(command["on"])

        resolved = self._resolve(name, ACK_HV_OUTPUT, actor)
        if resolved is None:
            return
        channel, reader, index = resolved

        with reader._lock:
            word = reader.status(index)
            vset_mag = reader.monitor(index, "VSET")

            if word is None:
                self._refuse(name, "could not read the channel's status word",
                             actor, ack_topic=ACK_HV_OUTPUT,
                             action="caen_output")
                return

            # A channel whose enable is off cannot be energised, and the board
            # would refuse anyway - but saying so here names the remedy rather
            # than reporting the symptom.
            if wanted_on and is_disabled(word):
                self._refuse(
                    name, "this channel is disabled at the supply. Flip its "
                          "enable switch on the front panel first - nothing "
                          "here can do that, by design (section 10a)", actor,
                    ack_topic=ACK_HV_OUTPUT, action="caen_output")
                return

            was_on = is_energised(word)
            sent, why = reader.set_output(index, wanted_on)
            after = reader.status(index) if sent else None

        vset = (apply_sign(vset_mag, channel.sign)
                if vset_mag is not None else None)

        if not sent:
            self._refuse(name, why, actor, old="on" if was_on else "off",
                         new="on" if wanted_on else "off",
                         ack_topic=ACK_HV_OUTPUT, action="caen_output")
            return

        # THE READ-BACK IS ASYMMETRIC, and deliberately so.
        #
        # Turning ON sets bit 0 immediately, even when the channel then spends
        # a minute ramping, so it can be verified at once.
        #
        # Turning OFF starts a ramp DOWN, and bit 0 stays set until the
        # channel actually reaches zero. Demanding it clear immediately would
        # report a failure for a command that worked perfectly - and, worse,
        # would train somebody to re-send OFF to a channel already on its way
        # down. So OFF is verified as "the board accepted it", and the state
        # is reported as it is.
        now_on = is_energised(after) if after is not None else None
        if wanted_on and now_on is not True:
            self._refuse(
                name, "asked the channel to turn on, but it did not report ON "
                      "(status %s). The write did not take." % after, actor,
                old="on" if was_on else "off", new="on",
                ack_topic=ACK_HV_OUTPUT, action="caen_output")
            return

        ramping = vset is not None and abs(vset) > 1.0
        detail = ""
        if wanted_on and ramping:
            detail = "ramping to %+.1f V" % vset
        elif wanted_on:
            detail = "energised at 0 V; nothing will move until a setpoint is set"
        elif now_on:
            detail = "ramping down at the board's own rate"

        log.warning("output %s: %s -> %s%s, by %s", name,
                    "on" if was_on else "off", "on" if wanted_on else "off",
                    (" (%s)" % detail) if detail else "", actor)
        # Before the ack, not after: the ack is what releases the web request,
        # and the page it then renders must not be built from the state this
        # command has just made obsolete.
        if after is not None:
            self._publish_status_now(channel, after)
        self.bus.publish_raw(ACK_HV_OUTPUT, json.dumps(
            {"ok": True, "channel": name, "on": wanted_on, "vset": vset,
             "detail": detail, "by": actor}, separators=(",", ":")))
        self._audit(name, "on" if was_on else "off",
                    "on" if wanted_on else "off", "ok", detail, actor,
                    action="caen_output")

    def run(self) -> int:
        self.bus.subscribe(TOPIC_HV_VSET, self._handle_vset)
        self.bus.subscribe(TOPIC_HV_OUTPUT, self._handle_output)
        return super().run()

    def _relink(self, force: bool = False) -> bool:
        """Re-resolve the supplies after a link loss, keeping what still works.

        Deliberately a FULL re-resolution, never a bare reopen. A replugged
        unit can come back on a different COM number, and the handle held
        before the unplug is dead regardless; the ports are enumerated again
        and every board is asked for its BDSNUM, so if the two cables were
        swapped while the link was down this finds each unit where it now is
        rather than reading the wrong supply under the right name (§6.2
        rule 6).

        Unlike startup this is FORGIVING, and that is the point. Startup is
        strict by design - refuse to run rather than guess (§6.1) - and
        verify_identity() clears every reader before it raises, so reusing it
        here would drop a healthy supply on the floor the moment its
        neighbour went missing. Each supply is resolved on its own: one that
        is still unplugged costs the other nothing.

        Returns True if at least one supply is connected afterwards.
        """
        if self.simulate:
            return True

        now = time.monotonic()
        if not force and now < self._next_relink:
            return bool(self._readers)
        # Enumerating and probing costs a second or two, during which the
        # healthy supply is not read. Once every 10 s keeps a dead cable from
        # starving a live one.
        self._next_relink = now + 10.0

        for reader in self._readers.values():
            reader.close()
        self._readers.clear()

        for spec in self._specs:
            vid, pid = spec["match"]["vid"], spec["match"]["pid"]
            expected = {spec["id"]: (spec["board_name"], str(spec["board_serial"]))}
            try:
                mapping = resolve(vid, pid, self._probe, expected)
            except IdentityError as exc:
                log.warning("%s: not found; leaving it disconnected (%s)",
                            spec["id"], str(exc).split(".")[0])
                continue
            reader = CaenChannelReader(
                device_id=spec["id"], port=mapping[spec["id"]],
                address=int(spec.get("board_address", 0)),
                baud=int(spec.get("baud", 9600)),
                board_name=spec["board_name"],
                board_serial=str(spec["board_serial"]))
            try:
                reader.open()
            except Exception as exc:
                log.warning("%s: could not open %s: %s",
                            spec["id"], reader.port, exc)
                continue
            self._readers[spec["id"]] = reader
            self._link_down[spec["id"]] = 0
            log.info("%s: relinked on %s, serial %s re-verified",
                     spec["id"], reader.port, reader.board_serial)
            self._check_expectations(spec["id"], reader)

        return bool(self._readers)

    def reconnect(self) -> bool:
        """Called by BaseService when every channel has failed (§6.1)."""
        return self._relink(force=True)

    def close(self) -> None:
        for reader in self._readers.values():
            reader.close()
        log.info("CAEN ports released")
