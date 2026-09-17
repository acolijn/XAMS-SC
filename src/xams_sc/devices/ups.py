"""APC UPS, over USB HID. See DESIGN.md §7.4.

Read-only, and the smallest service in the system. It earns its place because
**a power event is one of the few things that can end a run**.

HOW IT READS, AND WHY THIS WAY.

Three routes were tried on 17 September 2026:

  * `Win32_Battery` and the other WMI battery classes — **no instances**.
    PowerChute has claimed the HID and Windows does not register it as a
    system battery.
  * `GetSystemPowerStatus` — answers, but reports `BatteryFlag = 128`,
    "no system battery". Its `ACLineStatus` therefore describes the wall
    socket, not the UPS, and would read "on line power" while the UPS was
    running on battery. **A channel that cannot detect the thing it monitors
    is worse than no channel**, so this route was rejected rather than used
    as an approximation.
  * The HID feature reports directly — works, and works **alongside
    PowerChute** without taking the device away from it. HID input is
    shareable on Windows, so PowerChute keeps doing its safe-shutdown job.

The usages below are the standard USB HID Power Device / Battery System
definitions, not APC extensions, so this should survive a UPS replacement.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..model import Measurement, Quality, utcnow
from ..service import BaseService

log = logging.getLogger(__name__)

APC_VENDOR_ID = 0x051D

# USB HID Power Device usages. Read from feature reports, alongside PowerChute.
USAGE_AC_PRESENT = 0x008500D0
USAGE_BATTERY_PRESENT = 0x008500D1
USAGE_DISCHARGING = 0x00850045
USAGE_CHARGING = 0x00850044
USAGE_REMAINING_CAPACITY = 0x00850066
USAGE_RUNTIME_TO_EMPTY = 0x00850068
USAGE_NEEDS_REPLACEMENT = 0x00840065
USAGE_OVERLOAD = 0x00840069

WANTED = {
    USAGE_AC_PRESENT: "ac_present",
    USAGE_BATTERY_PRESENT: "battery_present",
    USAGE_DISCHARGING: "discharging",
    USAGE_CHARGING: "charging",
    USAGE_REMAINING_CAPACITY: "battery_pct",
    USAGE_RUNTIME_TO_EMPTY: "runtime_s",
    USAGE_NEEDS_REPLACEMENT: "needs_replacement",
    USAGE_OVERLOAD: "overload",
}


class UpsReader:
    """Opens the APC HID and reads its power-summary feature reports."""

    def __init__(self, vendor_id: int = APC_VENDOR_ID, serial: str | None = None):
        self.vendor_id = vendor_id
        self.serial = serial
        self._device = None

    def open(self) -> None:
        import pywinusb.hid as hid

        devices = hid.HidDeviceFilter(vendor_id=self.vendor_id).get_devices()
        if self.serial:
            devices = [d for d in devices
                       if (d.serial_number or "").strip() == self.serial]
        if not devices:
            raise RuntimeError(
                f"no APC HID device with vendor {self.vendor_id:#06x}"
                + (f" and serial {self.serial!r}" if self.serial else ""))
        self._device = devices[0]
        self._device.open()

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None

    def identity(self) -> tuple[str, str] | None:
        if self._device is None:
            return None
        return (self._device.product_name or "",
                (self._device.serial_number or "").strip())

    def read_all(self) -> dict[str, int]:
        """Every wanted usage the UPS reports, by short name.

        A usage that is absent is simply not in the result; the caller decides
        whether its channel is therefore unknown.
        """
        if self._device is None:
            raise RuntimeError("UPS not open")

        found: dict[str, int] = {}
        for report in self._device.find_feature_reports():
            try:
                report.get()
                usages = report.get_usages()
            except Exception:
                # A report that will not read is not fatal; others carry the
                # values we actually need.
                continue
            for usage, value in usages.items():
                name = WANTED.get(usage)
                if name is None or name in found:
                    continue
                if isinstance(value, list):
                    value = value[0] if value else None
                if value is not None:
                    found[name] = int(value)
        return found


class UpsService(BaseService):
    name = "ups"

    def __init__(self, config: Config, bus, simulate: bool = False, **kw):
        super().__init__(config, bus, simulate=simulate, **kw)
        self._spec = config.devices.get("ups", {}) or {}
        self._channels = config.channels_for("ups")
        self._reader: UpsReader | None = None
        self._last_on_battery: bool | None = None

    def verify_identity(self) -> bool:
        if self.simulate:
            log.info("simulate=True: skipping hardware identity check")
            return True

        match = self._spec.get("match", {})
        vendor = int(str(match.get("vid", "051D")), 16)
        serial = str(match.get("serial", "")).strip() or None

        reader = UpsReader(vendor, serial)
        try:
            reader.open()
        except Exception as exc:
            log.critical("FATAL: %s", exc)
            return False

        identity = reader.identity()
        if identity is None:
            log.critical("FATAL: the UPS did not report an identity")
            reader.close()
            return False

        product, found_serial = identity
        if serial and found_serial != serial:
            log.critical(
                "FATAL: UPS serial mismatch — devices.yaml says %s, the device "
                "reports %s. Refusing to start; do not guess.", serial, found_serial)
            reader.close()
            return False

        log.info("UPS %r serial %s confirmed", product, found_serial)
        self._reader = reader

        status = reader.read_all()
        log.info("UPS at startup: %s", ", ".join(f"{k}={v}" for k, v in sorted(status.items())))
        if not status.get("battery_present", 1):
            log.warning("the UPS reports NO BATTERY PRESENT")
        if status.get("needs_replacement"):
            log.warning("the UPS reports that its battery NEEDS REPLACEMENT")
        return True

    def reconnect(self) -> bool:
        if self.simulate:
            return True
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        return self.verify_identity()

    def read(self) -> list[Measurement]:
        if self.simulate:
            return self._read_simulated()
        if self._reader is None:
            raise RuntimeError("UPS not connected")

        status = self._reader.read_all()
        if not status:
            raise RuntimeError("the UPS returned no readable feature reports")

        now = utcnow()

        # On battery is derived from two independent signals. Either one alone
        # can lag: ACPresent drops immediately, Discharging follows. Taking
        # either as true means the alarm fires on the first of them.
        ac_present = status.get("ac_present")
        discharging = status.get("discharging")
        on_battery: float | None = None
        if ac_present is not None or discharging is not None:
            on_battery = float(bool(discharging) or (ac_present == 0))

        if on_battery is not None and bool(on_battery) != self._last_on_battery:
            if on_battery:
                log.warning("UPS IS ON BATTERY — mains power has been lost")
            elif self._last_on_battery is not None:
                log.info("UPS back on line power")
            self._last_on_battery = bool(on_battery)

        values = {
            "ups_on_battery": on_battery,
            "ups_battery_pct": (float(status["battery_pct"])
                                if "battery_pct" in status else None),
            "ups_runtime_min": (status["runtime_s"] / 60.0
                                if "runtime_s" in status else None),
        }

        out = []
        for ch in self._channels:
            value = values.get(ch.name)
            out.append(Measurement(
                t=now, channel=ch.name, value=value, unit=ch.unit,
                raw=value,
                quality=Quality.OK if value is not None else Quality.ERROR))
        return out

    def _read_simulated(self) -> list[Measurement]:
        now = utcnow()
        defaults = {"ups_on_battery": 0.0, "ups_battery_pct": 100.0,
                    "ups_runtime_min": 26.0}
        return [Measurement(t=now, channel=ch.name,
                            value=defaults.get(ch.name, 0.0), unit=ch.unit,
                            raw=defaults.get(ch.name, 0.0),
                            quality=Quality.OK, src="sim")
                for ch in self._channels]

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        log.info("UPS released")
