"""The channel and alarm reference, built from the configuration itself.

Run by the `gen-files` plugin from `mkdocs.yml`.

A hand-written channel table is a table that is wrong within a month. These
are read from `config/*.yaml` at every build, so they cannot drift — the same
argument as `tests/test_grafana_drift.py`, applied to prose.

The generated pages exist only in the built site; nothing is written into
`docs/`, so there is nothing to git-ignore and nothing to forget to
regenerate. Everything else in the manual is an ordinary file under `docs/`.
"""

from __future__ import annotations

from pathlib import Path

import mkdocs_gen_files
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


# ----------------------------------------------------------------- channels

channels = yaml.safe_load((CONFIG / "channels.yaml").read_text(encoding="utf-8"))
by_device: dict[str, list[dict]] = {}
for channel in channels.get("channels", []):
    by_device.setdefault(channel.get("device", "?"), []).append(channel)

with mkdocs_gen_files.open("reference/channels.md", "w") as page:
    page.write("# Channels\n\n")
    page.write(
        "Generated from `config/channels.yaml` at build time. That file is the "
        "single source of truth: channel names are the identity of a "
        "measurement — MQTT topic, JSONL record, database row, UI label — and "
        "**renaming one breaks history**.\n\n"
        "Scaling is `value = (raw - offset) * multiplier`, the LabVIEW "
        "convention. Disabled channels are listed rather than omitted, so the "
        "map is complete and a gap is never left ambiguous.\n\n"
    )
    total = sum(len(v) for v in by_device.values())
    enabled = sum(1 for v in by_device.values() for c in v
                  if c.get("enabled", True))
    page.write(f"**{enabled} enabled channels** of {total} defined, "
               f"across {len(by_device)} devices.\n\n")

    for device in sorted(by_device):
        page.write(f"## {device}\n\n")
        page.write("| Channel | Physical | Kind | Unit | Offset | Multiplier "
                   "| LabVIEW | Description |\n")
        page.write("|---|---|---|---|---|---|---|---|\n")
        for c in by_device[device]:
            name = f"`{c['name']}`"
            if not c.get("enabled", True):
                name += " *(disabled)*"
            page.write(
                f"| {name} "
                f"| `{c.get('phys', '')}` "
                f"| {c.get('kind', '')} "
                f"| {c.get('unit', '')} "
                f"| {c.get('offset', 0.0)} "
                f"| {c.get('multiplier', 1.0)} "
                f"| {c.get('legacy', '')} "
                f"| {c.get('description', '')} |\n"
            )
        page.write("\n")

mkdocs_gen_files.set_edit_path("reference/channels.md", "../config/channels.yaml")


# ------------------------------------------------------------------- alarms

alarms = yaml.safe_load((CONFIG / "alarms.yaml").read_text(encoding="utf-8"))
defaults = alarms.get("defaults", {}) or {}
staleness = alarms.get("staleness", {}) or {}

LEVELS = ["lolo", "low", "high", "hihi"]

with mkdocs_gen_files.open("reference/alarms.md", "w") as page:
    page.write("# Alarms\n\n")
    page.write(
        "Generated from `config/alarms.yaml` at build time. Four thresholds "
        "per channel, EPICS-style. Changing a threshold silently alters what "
        "the system protects against, so this file lives in git and is "
        "applied with `xams-ctl reload` — unlike `recipients.yaml`, which is "
        "edited from the web UI.\n\n"
    )

    page.write("## Defaults\n\n| Setting | Value |\n|---|---|\n")
    for key, value in defaults.items():
        page.write(f"| `{key}` | {value} |\n")
    page.write("\n")

    if staleness:
        notify = ", ".join(staleness.get("notify", []) or [])
        page.write(
            "## Staleness\n\n"
            "A dead sensor must not read as healthy, so staleness is an alarm "
            "at the same severity as a threshold breach.\n\n"
            f"Severity **{staleness.get('severity', '?')}**, "
            f"notifying **{notify or 'nobody'}**.\n\n"
        )

    page.write("## Thresholds\n\n")
    page.write("| Channel | " + " | ".join(LEVELS) + " |\n")
    page.write("|---" * (len(LEVELS) + 1) + "|\n")
    for name, limits in (alarms.get("channels", {}) or {}).items():
        cells = []
        for level in LEVELS:
            limit = (limits or {}).get(level)
            if not limit:
                cells.append("—")
                continue
            notify = ", ".join(limit.get("notify", []) or [])
            cells.append(f"{limit.get('value')}<br>"
                         f"*{limit.get('severity', '?')}* → {notify}")
        page.write(f"| `{name}` | " + " | ".join(cells) + " |\n")
    page.write("\n")

mkdocs_gen_files.set_edit_path("reference/alarms.md", "../config/alarms.yaml")
