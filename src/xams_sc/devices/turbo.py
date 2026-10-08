"""Pfeiffer HiPace 80 turbo pump, by LISTENING to its RS-485 link. See docs/drivers/turbo.md.

The pump (TC 110 electronics) is controlled by the DCU of its HiCube 80 Eco
pumping station, which is the master on a 2-wire RS-485 bus and polls the pump
about twice a second. An EXSYS EX-13009 (FTDI) sits on a Y-piece in that link
and this service reads what passes by.

**THIS DRIVER NEVER TRANSMITS. Not a command, not a query, not a line break.**

The bus already has a master. A second one would collide with the DCU's polls
at best, and at worst a valid Pfeiffer telegram from this PC is a command the
pump obeys - parameter 010 switches the pumping station, 023 the motor. So:

  * the port is wrapped in `_ReceiveOnlyPort`, which has no `write` at all, so
    no code path in this module can send a byte by mistake;
  * RTS and DTR are held low before the port is opened;
  * nothing in this file builds a telegram, only parses them.

Consequences of listening rather than asking, all deliberate:

  * **Only what the DCU asks for can be read.** It always polls ten status
    parameters (001, 002, 010, 300, 302-307), plus the pump parameter on its
    display and the one on its service line (DCU parameter 795). With the
    service line on 309 the speed is on the bus; checked 2026-10-08. A channel
    whose parameter the DCU stops polling is published as `quality=error`, not
    left at its last value.
  * **Identity is the adapter, not the pump.** The pump cannot be asked who it
    is (that would be a transmission), and the DCU never polls its name. The
    adapter is matched on VID/PID AND its USB serial, and the service starts
    only once valid, checksummed replies from the configured pump address are
    heard on it. This is a narrower check than the `*IDN?` of the other serial
    drivers (DESIGN.md section 6.2), and is documented as such.
  * **The vacuum pressure is not on this bus.** The DCU reads the PKR gauge on
    its own X3 input and shows it as its own parameter 340. It is read through
    the gauge's analog output instead (reference/HiPace80 in the workspace).

Telegram format (TC 110 operating instructions, "Pfeiffer Vacuum protocol"):

    aaa  ad  ppp  ll  data...  ccc  CR
    address, action (00 query/command from the master, 10 reply from the pump),
    parameter, data length, data, checksum = byte sum of everything before it
    mod 256, three decimal digits.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from ..config import Config
from ..model import Measurement, Quality, utcnow
from ..service import BaseService
from .serial_id import candidates

log = logging.getLogger(__name__)

# What the pump sends in reply. Queries from the DCU carry action "00".
ACTION_REPLY = "10"

# Pfeiffer parameter number -> (name, data type). Only parameters a channel
# may name; one not listed here is refused at startup rather than decoded by
# guess. The data types are the protocol's own:
#   bool   boolean_old, "000000" / "111111"
#   uint   u_integer, six digits
#   ureal  u_real, six digits, two implied decimals
#   error  six characters: "000000" / "no Err" / "Err001" / "Wrn007"
PARAMS: dict[int, tuple[str, str]] = {
    1: ("Heating", "bool"),
    2: ("Standby", "bool"),
    10: ("PumpgStatn", "bool"),
    300: ("RemotePrio", "bool"),
    302: ("SpdSwPtAtt", "bool"),
    303: ("Error code", "error"),
    304: ("OvTempElec", "bool"),
    305: ("OvTempPump", "bool"),
    306: ("SetSpdAtt", "bool"),
    307: ("PumpAccel", "bool"),
    309: ("ActualSpd", "uint"),      # Hz
    310: ("DrvCurrent", "ureal"),    # A
    311: ("OpHrsPump", "uint"),      # h
    316: ("DrvPower", "uint"),       # W
    326: ("TempElec", "uint"),       # C
    330: ("TempPmpBot", "uint"),     # C
    342: ("TempBearng", "uint"),     # C
    346: ("TempMotor", "uint"),      # C
    398: ("ActualSpd rpm", "uint"),  # 1/min
}

# A received buffer with no CR in it for this long is noise, not a telegram.
MAX_PENDING = 256


@dataclass(frozen=True)
class Telegram:
    address: int
    action: str
    param: int
    data: str


def parse_telegram(text: str) -> Telegram | None:
    """One telegram without its CR, or None if it is not a valid one.

    Everything is checked - field digits, the length byte against the data
    actually present, and the checksum - because a sniffer joining mid-stream
    or a byte lost to the bus sees fragments, and a fragment decoded as a
    number is a plausible wrong value.
    """
    if len(text) < 13:
        return None
    head, checksum = text[:-3], text[-3:]
    if not (head[:10].isdigit() and checksum.isdigit()):
        return None
    length = int(head[8:10])
    if len(head) != 10 + length:
        return None
    if sum(head.encode("ascii", errors="replace")) % 256 != int(checksum):
        return None
    return Telegram(address=int(head[0:3]), action=head[3:5],
                    param=int(head[5:8]), data=head[10:])


def decode(param: int, data: str) -> float:
    """A reply's data field in engineering units. Raises ValueError if malformed."""
    kind = PARAMS[param][1]
    if kind == "bool":
        if data == "000000":
            return 0.0
        if data == "111111":
            return 1.0
        raise ValueError(f"{data!r} is not a Pfeiffer boolean")
    if kind == "uint":
        if len(data) != 6 or not data.isdigit():
            raise ValueError(f"{data!r} is not a u_integer")
        return float(int(data))
    if kind == "ureal":
        if len(data) != 6 or not data.isdigit():
            raise ValueError(f"{data!r} is not a u_real")
        return int(data) / 100.0
    if kind == "error":
        text = data.strip()
        if text in ("000000", "no Err", "noErr") or text.lower().startswith("no"):
            return 0.0
        digits = "".join(c for c in text if c.isdigit())
        if not digits or not text[:3].isalpha() and not text.isdigit():
            raise ValueError(f"{data!r} is not a Pfeiffer error code")
        return float(int(digits))
    raise ValueError(f"no decoder for data type {kind!r}")


class _ReceiveOnlyPort:
    """A serial port that can be read and closed, and nothing else.

    There is deliberately no `write`. The bus has a master already (the DCU),
    and a byte from this PC is at best a collision and at worst a command.
    Making the method absent rather than "not called" means a later edit
    cannot send something by accident: it would raise AttributeError.
    """

    def __init__(self, port: str, baud: int):
        import serial

        s = serial.Serial()
        s.port = port
        s.baudrate = baud
        s.bytesize = serial.EIGHTBITS
        s.parity = serial.PARITY_NONE
        s.stopbits = serial.STOPBITS_ONE
        s.timeout = 0
        s.xonxoff = s.rtscts = s.dsrdtr = False
        # Held low BEFORE opening, so the adapter's lines never toggle.
        s.dtr = False
        s.rts = False
        s.open()
        # The service backs off up to 30 s on errors; at 9600 baud that is
        # ~30 kB, more than the default Windows receive buffer. A lost tail
        # only costs telegrams (the checksum drops fragments), but a bigger
        # buffer costs nothing either.
        try:
            s.set_buffer_size(rx_size=65536)
        except (AttributeError, Exception):
            pass
        self._s = s

    def read_available(self) -> bytes:
        n = self._s.in_waiting
        return self._s.read(n) if n else b""

    def close(self) -> None:
        self._s.close()


class PfeifferTap:
    """Collects the pump's replies as they pass on the bus.

    `latest` maps parameter number to (data, monotonic time received). Only
    REPLIES from the configured pump address are kept; the DCU's queries say
    which parameters are being polled, not what their values are.
    """

    def __init__(self, port, address: int):
        self.port = port
        self.address = address
        self.latest: dict[int, tuple[str, float]] = {}
        self.polled: set[int] = set()
        self.last_reply: float | None = None
        self.rejected = 0
        self._pending = b""

    def poll(self, now: float | None = None) -> int:
        """Read what has arrived and parse every complete telegram. Returns replies taken."""
        now = time.monotonic() if now is None else now
        self._pending += self.port.read_available()
        *lines, self._pending = self._pending.split(b"\r")
        if len(self._pending) > MAX_PENDING:
            self._pending = b""
            self.rejected += 1
        taken = 0
        for raw in lines:
            if not raw:
                continue
            try:
                t = parse_telegram(raw.decode("ascii"))
            except UnicodeDecodeError:
                t = None
            if t is None:
                # A fragment, a collision or the half-telegram the sniffer
                # joined in the middle of. Dropped, never guessed at.
                self.rejected += 1
                continue
            if t.address != self.address:
                continue
            if t.action == ACTION_REPLY:
                self.latest[t.param] = (t.data, now)
                self.last_reply = now
                taken += 1
            elif t.data == "=?":
                self.polled.add(t.param)
        return taken


class TurboService(BaseService):
    name = "turbo"

    def __init__(self, config: Config, bus, simulate: bool = False, **kw):
        super().__init__(config, bus, simulate=simulate, **kw)
        self._spec = config.devices.get("turbo", {}) or {}
        self._channels = config.channels_for("turbo")
        self._address = int(self._spec.get("address", 1))
        self._baud = int(self._spec.get("baud", 9600))
        self._max_age = float(self._spec.get("max_age_s", 5.0))
        self._identify_s = float(self._spec.get("identify_s", 5.0))
        self._tap: PfeifferTap | None = None
        self._missing: set[str] = set()
        self._last_error_code: float | None = None
        self._t0 = time.monotonic()

    # ------------------------------------------------------------ identity

    def _check_channels(self) -> bool:
        ok = True
        for ch in self._channels:
            try:
                param = int(ch.phys)
            except ValueError:
                param = -1
            if param not in PARAMS:
                log.critical("FATAL: channel %s names parameter %r, which this "
                             "driver does not know how to decode. Refusing to "
                             "start rather than guess.", ch.name, ch.phys)
                ok = False
        return ok

    def _find_port(self) -> str | None:
        match = self._spec.get("match", {})
        vid, pid = str(match.get("vid", "")), str(match.get("pid", ""))
        want = str(match.get("serial", "")).strip()
        if not (vid and pid and want):
            log.critical("FATAL: devices.yaml turbo.match needs vid, pid AND "
                         "serial. An FTDI VID/PID alone is shared by countless "
                         "adapters, so it identifies nothing.")
            return None
        found = [c for c in candidates(vid, pid) if (c.usb_serial or "") == want]
        if not found:
            log.critical("FATAL: no port with USB id %s:%s and serial %s. Is the "
                         "RS-485 adapter plugged in?", vid, pid, want)
            return None
        if len(found) > 1:
            log.critical("FATAL: serial %s on more than one port (%s); refusing "
                         "to guess.", want, ", ".join(c.port for c in found))
            return None
        return found[0].port

    def verify_identity(self) -> bool:
        if not self._check_channels():
            return False
        if self.simulate:
            log.info("simulate=True: skipping hardware identity check")
            return True

        port = self._find_port()
        if port is None:
            return False
        try:
            tap = PfeifferTap(_ReceiveOnlyPort(port, self._baud), self._address)
        except Exception as exc:
            # Busy port: a clean failure, not a stack trace (section 6.1).
            log.critical("FATAL: cannot open %s: %s", port, exc)
            return False

        deadline = time.monotonic() + self._identify_s
        while time.monotonic() < deadline and tap.last_reply is None:
            tap.poll()
            time.sleep(0.2)
        if tap.last_reply is None:
            log.critical(
                "FATAL: %s opened, but no valid reply from pump address %03d in "
                "%.0f s (%d fragments rejected). Is the DCU on, and are D+/D- "
                "the right way round? Nothing was sent.", port, self._address,
                self._identify_s, tap.rejected)
            tap.port.close()
            return False

        log.info("turbo tap on %s: pump %03d answering; DCU polls %s",
                 port, self._address, sorted(tap.polled))
        self._tap = tap
        return True

    def reconnect(self) -> bool:
        if self.simulate:
            return True
        self.close()
        return self.verify_identity()

    # ---------------------------------------------------------------- read

    def read(self) -> list[Measurement]:
        if self.simulate:
            return self._read_simulated()
        if self._tap is None:
            raise RuntimeError("turbo tap not open")

        mono = time.monotonic()
        self._tap.poll(mono)
        now = utcnow()

        last = self._tap.last_reply
        if last is None or mono - last > self._max_age:
            raise RuntimeError(
                f"no reply from pump {self._address:03d} for "
                f"{'ever' if last is None else f'{mono - last:.0f} s'}: DCU off, "
                f"cable unplugged, or the pump electronics unpowered")

        out = []
        for ch in self._channels:
            param = int(ch.phys)
            entry = self._tap.latest.get(param)
            value = None
            if entry is not None and mono - entry[1] <= self._max_age:
                try:
                    value = decode(param, entry[0])
                except ValueError as exc:
                    log.warning("%s: %s", ch.name, exc)
            self._note_missing(ch.name, param, value is None)
            if param == 303 and value is not None:
                self._note_error_code(value, entry[0])
            out.append(Measurement(
                t=now, channel=ch.name, value=value, unit=ch.unit,
                raw=value,
                quality=Quality.OK if value is not None else Quality.ERROR))
        return out

    def _note_missing(self, channel: str, param: int, missing: bool) -> None:
        """Say once when a parameter stops or starts being polled."""
        if missing and channel not in self._missing:
            self._missing.add(channel)
            log.warning(
                "%s: parameter %03d is not being polled by the DCU. Only the "
                "status set, the display parameter and the service line (DCU "
                "795) are on the bus - set one of them to %03d.",
                channel, param, param)
        elif not missing and channel in self._missing:
            self._missing.discard(channel)
            log.info("%s: parameter %03d is on the bus again", channel, param)

    def _note_error_code(self, value: float, text: str) -> None:
        if value != self._last_error_code:
            if value:
                log.warning("TURBO REPORTS %r", text.strip())
            elif self._last_error_code is not None:
                log.info("turbo error cleared")
            self._last_error_code = value

    def _read_simulated(self) -> list[Measurement]:
        now = utcnow()
        elapsed = time.monotonic() - self._t0
        out = []
        for ch in self._channels:
            param = int(ch.phys)
            if param == 309:
                value = 1500.0 + 2.0 * math.sin(elapsed / 30.0)
            elif param in (10, 306):
                value = 1.0
            else:
                value = 0.0
            out.append(Measurement(t=now, channel=ch.name, value=value,
                                   unit=ch.unit, raw=value, quality=Quality.OK,
                                   src="sim"))
        return out

    def close(self) -> None:
        if self._tap is not None:
            try:
                self._tap.port.close()
            except Exception:
                pass
            self._tap = None
            log.info("turbo tap released")
