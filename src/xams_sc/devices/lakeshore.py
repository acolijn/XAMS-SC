"""Lake Shore Model 335 temperature controller. See DESIGN.md §7.3.

**READ-ONLY at milestone 5.** Reads the two sensor inputs, the two heater
outputs, the setpoints, the heater ranges and the PID settings. Nothing here
writes to the instrument; the setpoint control path is milestone 8.

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

import logging
import time

import serial

from ..config import Config
from ..model import Measurement, Quality, utcnow
from ..service import BaseService
from .serial_id import IdentityError, resolve

log = logging.getLogger(__name__)

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
        self._channels = config.channels_for("lakeshore")

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
