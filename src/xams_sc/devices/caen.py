"""CAEN DT1470ET high-voltage supplies. See DESIGN.md §7.2.

Two units, both on USB, ASCII protocol over the virtual COM port. No vendor
library.

**READ-ONLY at milestone 5.** This service issues `CMD:MON` only. The control
path is milestone 8, and only after §10's open decision is made. There is
deliberately no code here that can write a setpoint.

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

import logging
import time

import serial

from ..config import Config
from ..model import Measurement, Quality, utcnow
from ..scaling import apply_sign
from ..service import BaseService
from .serial_id import IdentityError, resolve

log = logging.getLogger(__name__)

# The status word and its decoding live in hv_status.py: they are pure logic
# and the web UI needs them, and this module imports `serial`. Re-exported
# here because this is where anyone would look for them.
from ..hv_status import (  # noqa: F401
    BIT_DISABLED, BIT_ON, FAULT_BITS, STAT_BITS, describe_status, is_disabled,
    is_enabled, status_faults)


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

    def _command(self, par: str, channel: int | None = None) -> str | None:
        """Send one CMD:MON and return its VAL, or None.

        MON only. There is no SET path in this class, by design.
        """
        if self._serial is None:
            return None
        cmd = f"$BD:{self.address},CMD:MON,PAR:{par}"
        if channel is not None:
            cmd += f",CH:{channel}"
        try:
            self._serial.reset_input_buffer()
            self._serial.write((cmd + "\r\n").encode("ascii"))
            time.sleep(0.05)
            raw = self._serial.read_until(b"\r\n", 200).decode("ascii", errors="replace")
            if not raw.strip():
                # Some firmware answers slowly; one short retry, then give up.
                time.sleep(0.15)
                raw = self._serial.read(200).decode("ascii", errors="replace")
        except Exception as exc:
            log.debug("%s: %s raised %s", self.device_id, cmd, exc)
            return None
        value = _decode(raw)
        if value is None:
            # Log the raw instrument response, not just the parsed failure —
            # that is what makes protocol bugs findable (§12).
            log.debug("%s: %s -> unparseable %r", self.device_id, cmd, raw)
        return value

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

            par = "VMON" if ch.kind == "hv_vmon" else "IMON"
            magnitude = reader.monitor(index, par)

            if magnitude is None:
                # The device is there but did not answer intelligibly. Publish
                # the absence, never a substituted value.
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, quality=Quality.ERROR))
                continue

            alive[ch.device] = True
            value = apply_sign(magnitude, ch.sign) if ch.kind == "hv_vmon" else magnitude
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
            if ch.kind == "hv_vmon" and ch.limits:
                span = ch.limits["max"] if ch.sign > 0 else ch.limits["min"]
                magnitude = abs(span) * 0.8 + random.gauss(0, 1.0)
                value = apply_sign(magnitude, ch.sign)
            else:
                magnitude = value = abs(random.gauss(0.5, 0.05))
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=magnitude,
                                   quality=Quality.OK, src="sim"))
        return out

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
