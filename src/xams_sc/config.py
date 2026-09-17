"""Load and validate the YAML configuration. See DESIGN.md §4.

Configuration is data, not code (principle 2). Adding a sensor touches no
Python — it is an entry in channels.yaml.

Validation is strict and fails at startup rather than at the first bad read.
A typo in a channel name is cheap to find here and expensive to find in six
months of history.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Repository root, found relative to this file: src/xams_sc/config.py -> ../../
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("XAMS_CONFIG_DIR", ROOT / "config"))

VALID_KINDS = {
    "voltage", "current", "rtd", "hv_vmon", "hv_imon", "temperature", "status",
    "power",
}


class ConfigError(Exception):
    """Raised for any problem that should stop a service from starting."""


@dataclass(frozen=True)
class Channel:
    name: str
    device: str
    phys: str
    kind: str
    unit: str
    offset: float = 0.0
    multiplier: float = 1.0
    sign: int = 1
    rtd: dict | None = None
    limits: dict | None = None
    derive: dict | None = None
    """Compute this channel from another, via a named transform.

    {from: <channel>, transform: <name in scaling.TRANSFORMS>, **parameters}

    Used where the relationship is not linear and so cannot be expressed as
    an offset and a multiplier — the Lake Shore heater wattage, which goes as
    the square of the percentage.
    """
    legacy: str | None = None
    enabled: bool = True
    description: str = ""
    log_minmax: bool = False
    on_pid: bool = True
    """Whether this channel appears on the P&ID (§8.2).

    Default true for anything that measures a point in the plant. Set false
    for a reading that legitimately has no place on a piping drawing — the
    ambient temperature of the room, for instance. Without this the mimic
    drift check reports it as missing forever, and a warning that is always
    on is one nobody reads.
    """

    def in_limits(self, value: float) -> bool:
        """Software write range (§8.1). Convenience, NOT protection.

        The instrument's own MAXV applies underneath regardless and always
        wins. A channel with no `limits` accepts nothing: a write range that
        was never specified is not permission to write anything.
        """
        if not self.limits:
            return False
        return self.limits["min"] <= value <= self.limits["max"]


@dataclass
class Config:
    channels: dict[str, Channel]
    devices: dict[str, Any]
    alarms: dict[str, Any]
    recipients: dict[str, Any] = field(default_factory=dict)
    config_hash: str = ""

    def enabled_channels(self) -> list[Channel]:
        return [c for c in self.channels.values() if c.enabled]

    def channels_for(self, device: str) -> list[Channel]:
        return [c for c in self.enabled_channels() if c.device == device]

    def stale_after_seconds(self) -> float:
        return float(self.alarms.get("defaults", {}).get("stale_after_seconds", 60))


def _read(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"missing configuration file: {path}")
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        # A malformed file is a clean failure with the line number, not a
        # traceback. The same principle as a busy device in §6.1: the reader
        # needs to know what to fix, not where the parser gave up.
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" at line {mark.line + 1}, column {mark.column + 1}"
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise ConfigError(f"{path.name} is not valid YAML{where}: {problem}") from exc
    if data is not None and not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping, got {type(data).__name__}")
    return data if data is not None else {}


def _normalise(obj: Any) -> Any:
    """Canonical form for hashing: sorted keys, no formatting, no comments.

    Reformatting a YAML file or moving a channel must not change the hash;
    changing a value must.
    """
    if isinstance(obj, dict):
        return {k: _normalise(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, list):
        return [_normalise(v) for v in obj]
    return obj


def compute_hash(channels: dict, devices: dict, alarms: dict) -> str:
    """SHA-256 over the parsed, normalised config; first 7 hex characters (§4.5).

    recipients.yaml is excluded: it changes often and does not affect the data.
    """
    blob = json.dumps(
        _normalise({"channels": channels, "devices": devices, "alarms": alarms}),
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:7]


def _parse_channels(raw: dict, devices: dict) -> dict[str, Channel]:
    entries = raw.get("channels") or []
    if not entries:
        raise ConfigError("channels.yaml defines no channels")

    known_devices = set(devices) | {d["id"] for d in devices.get("caen", []) if "id" in d}
    known_devices.discard("caen")
    known_devices.add("derived")

    channels: dict[str, Channel] = {}
    seen_phys: dict[tuple[str, str], str] = {}

    for e in entries:
        if not isinstance(e, dict):
            raise ConfigError(f"channel entry is not a mapping: {e!r}")
        for key in ("name", "device", "phys", "kind", "unit"):
            if key not in e:
                raise ConfigError(f"channel {e.get('name', e)!r}: missing required key {key!r}")

        name = str(e["name"])
        if name in channels:
            raise ConfigError(f"duplicate channel name: {name!r}")
        if name != name.lower() or " " in name:
            raise ConfigError(f"channel {name!r}: names are lowercase with no spaces (§3)")

        kind = str(e["kind"])
        if kind not in VALID_KINDS:
            raise ConfigError(
                f"channel {name!r}: unknown kind {kind!r}; expected one of {sorted(VALID_KINDS)}"
            )

        device = str(e["device"])
        if device not in known_devices:
            raise ConfigError(
                f"channel {name!r}: unknown device {device!r}; known: {sorted(known_devices)}"
            )

        if kind == "rtd" and not e.get("rtd"):
            raise ConfigError(f"channel {name!r}: kind 'rtd' requires an `rtd:` block")

        sign = int(e.get("sign", 1))
        if sign not in (-1, 1):
            raise ConfigError(f"channel {name!r}: sign must be -1 or 1, got {sign!r}")

        derive = e.get("derive")
        if derive is not None:
            from .scaling import TRANSFORMS
            for key in ("from", "transform"):
                if key not in derive:
                    raise ConfigError(
                        f"channel {name!r}: derive block needs {key!r}")
            if derive["transform"] not in TRANSFORMS:
                raise ConfigError(
                    f"channel {name!r}: unknown transform "
                    f"{derive['transform']!r}; known: {sorted(TRANSFORMS)}")

        limits = e.get("limits")
        if limits is not None:
            if not {"min", "max"} <= set(limits):
                raise ConfigError(f"channel {name!r}: limits need both `min` and `max`")
            if limits["min"] > limits["max"]:
                raise ConfigError(f"channel {name!r}: limits min exceeds max")

        # Two channels on one physical input is legitimate for HV (vmon and
        # imon share an index) but a mistake anywhere else.
        # A derived channel shares the physical input it is computed from,
        # exactly as hv_vmon and hv_imon share a channel index.
        phys_key = (device, str(e["phys"]))
        shares_legitimately = kind.startswith("hv_") or derive is not None
        if phys_key in seen_phys and not shares_legitimately:
            raise ConfigError(
                f"channel {name!r}: physical input {phys_key[1]!r} on {device!r} "
                f"is already used by {seen_phys[phys_key]!r}"
            )
        seen_phys.setdefault(phys_key, name)

        channels[name] = Channel(
            name=name,
            device=device,
            phys=str(e["phys"]),
            kind=kind,
            unit=str(e["unit"]),
            offset=float(e.get("offset", 0.0)),
            multiplier=float(e.get("multiplier", 1.0)),
            sign=sign,
            rtd=e.get("rtd"),
            limits=limits,
            derive=derive,
            legacy=e.get("legacy"),
            enabled=bool(e.get("enabled", True)),
            description=str(e.get("description", "")),
            log_minmax=bool(e.get("log_minmax", False)),
            on_pid=bool(e.get("on_pid", True)),
        )

    return channels


def load(config_dir: Path | str | None = None) -> Config:
    """Load every configuration file, validate, and compute the config hash."""
    d = Path(config_dir) if config_dir else CONFIG_DIR

    raw_channels = _read(d / "channels.yaml")
    devices = _read(d / "devices.yaml")
    alarms = _read(d / "alarms.yaml")

    # recipients.yaml is optional and excluded from the hash: it does not
    # affect the data, and it is edited from the web UI (§4.4).
    recipients_path = d / "recipients.yaml"
    recipients = _read(recipients_path) if recipients_path.exists() else {"recipients": []}

    channels = _parse_channels(raw_channels, devices)

    return Config(
        channels=channels,
        devices=devices,
        alarms=alarms,
        recipients=recipients,
        config_hash=compute_hash(raw_channels, devices, alarms),
    )
