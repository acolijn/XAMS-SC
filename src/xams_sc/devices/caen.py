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

# Status word bits, DT1470ET. Bit 10 is the one seen in practice: every channel
# read 1024 on 17 September 2026 with all outputs off.
STAT_BITS = {
    0: "ON", 1: "RAMP_UP", 2: "RAMP_DOWN", 3: "OVER_CURRENT",
    4: "OVER_VOLTAGE", 5: "UNDER_VOLTAGE", 6: "MAX_V", 7: "TRIP",
    8: "OVER_POWER", 9: "OVER_TEMP", 10: "DISABLED", 11: "KILL",
    12: "INTERLOCK", 13: "UNCALIBRATED",
}


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


def describe_status(word: int) -> str:
    flags = [name for bit, name in STAT_BITS.items() if word & (1 << bit)]
    return ",".join(flags) if flags else "OFF"


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

    # --------------------------------------------------------------- identity

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

                def ask(port: str, vid=vid, pid=pid) -> tuple[str, ...] | None:
                    address = int(self._specs[0].get("board_address", 0))
                    baud = int(self._specs[0].get("baud", 9600))
                    reader = CaenChannelReader("probe", port, address, baud, "", "")
                    try:
                        reader.open()
                        identity = reader.identity()
                    except Exception as exc:
                        log.warning("could not probe %s: %s", port, exc)
                        identity = None
                    finally:
                        reader.close()
                    return identity

                mapping = resolve(vid, pid, ask, expected)

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
        for ch in self._channels:
            reader = self._readers.get(ch.device)
            if reader is None:
                continue
            index = int(ch.phys)
            par = "VMON" if ch.kind == "hv_vmon" else "IMON"
            magnitude = reader.monitor(index, par)

            if magnitude is None:
                # The device is there but did not answer intelligibly. Publish
                # the absence, never a substituted value.
                out.append(Measurement(t=now, channel=ch.name, value=None,
                                       unit=ch.unit, quality=Quality.ERROR))
                continue

            value = apply_sign(magnitude, ch.sign) if ch.kind == "hv_vmon" else magnitude
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=magnitude, quality=Quality.OK))

        if out and all(m.quality is Quality.ERROR for m in out):
            # Every channel failing means the link is gone, not that the
            # readings are bad. Raise so BaseService backs off and retries,
            # and so the unplug test produces a reconnect rather than a
            # stream of errors (§6.1).
            raise RuntimeError("no CAEN channel answered; link lost")
        return out

    def _read_simulated(self) -> list[Measurement]:
        import random
        now = utcnow()
        out = []
        for ch in self._channels:
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

    def reconnect(self) -> bool:
        """Close everything and resolve the supplies again from scratch.

        Deliberately a FULL re-resolution, not a reopen: the ports are
        enumerated again and every board is asked for its BDSNUM. If the two
        cables were swapped while the link was down, this finds each unit
        where it now is instead of reading the wrong supply under the right
        name (§6.2 rule 6).
        """
        if self.simulate:
            return True
        for reader in self._readers.values():
            reader.close()
        self._readers.clear()
        return self.verify_identity()

    def close(self) -> None:
        for reader in self._readers.values():
            reader.close()
        log.info("CAEN ports released")
