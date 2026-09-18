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


# -------------------------------------------------------------- command line

# Walked, not transcribed. `xams_ctl.build_parser()` exists for this: a page
# of options typed out beside the parser is a page that disagrees with the
# parser, and the disagreement is invisible until somebody trusts the page.
import argparse  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, str(ROOT / "src"))
from xams_sc.cli import xams_ctl  # noqa: E402

# Exit codes are the one thing the parser does not know: they live in the
# `cmd_*` return values. Kept here, next to the page that states them, rather
# than scattered through the prose.
EXIT_CODES = [
    ("0", "it did what was asked"),
    ("1", "it did not — a service that would not start or stop, a command "
          "nothing acknowledged, a write an instrument refused"),
    ("2", "the configuration is invalid and nothing was attempted "
          "(`status`, `reload`, `check`), or a service refused the reload; "
          "also what argparse itself returns for an unusable command line"),
]


def _arguments(parser: argparse.ArgumentParser) -> tuple[list, list]:
    """The positionals and the options of one subcommand, minus `-h`."""
    positional, optional = [], []
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if action.option_strings:
            optional.append(action)
        else:
            positional.append(action)
    return positional, optional


def _usage(name: str, positional: list, optional: list) -> str:
    parts = ["xams-ctl", name]
    for action in positional:
        parts.append(f"<{action.dest}>" if action.nargs != "?"
                     else f"[{action.dest}]")
    for action in optional:
        flag = action.option_strings[-1]
        parts.append(f"[{flag}]" if action.nargs == 0
                     else f"[{flag} <{action.dest}>]")
    return " ".join(parts)


parser = xams_ctl.build_parser()
subparsers = next(a for a in parser._actions
                  if isinstance(a, argparse._SubParsersAction))
# `choices` preserves the order the verbs were added in, which is the order
# somebody uses them in — start before stop, and the HV verbs together.
verbs = list(subparsers.choices.items())
helps = {choice.dest: choice.help for choice in subparsers._choices_actions}

with mkdocs_gen_files.open("reference/cli.md", "w") as page:
    page.write("# Command line\n\n")
    page.write(
        "Generated at build time by walking `xams_ctl.build_parser()`, so it "
        "cannot disagree with the command it documents. Everything below is "
        "also available as `xams-ctl <command> --help`.\n\n"
        "Run it with the virtual environment's interpreter on the lab PC:\n\n"
        "```\n.\\.venv\\Scripts\\python.exe -m xams_sc.cli.xams_ctl status\n"
        "```\n\n"
    )

    _, globals_ = _arguments(parser)
    page.write("## Everywhere\n\n| Option | Default | |\n|---|---|---|\n")
    for action in globals_:
        page.write(f"| `{action.option_strings[-1]}` | `{action.default}` "
                   f"| {action.help or ''} |\n")
    page.write("\n")

    page.write("## Commands\n\n")
    page.write("| Command | |\n|---|---|\n")
    for name, _ in verbs:
        page.write(f"| [`{name}`](#{name}) | {helps.get(name) or ''} |\n")
    page.write("\n")

    for name, sub_parser in verbs:
        positional, optional = _arguments(sub_parser)
        page.write(f"### `{name}`\n\n")
        if helps.get(name):
            page.write(f"{helps[name]}\n\n")
        page.write(f"```\n{_usage(name, positional, optional)}\n```\n\n")
        if positional or optional:
            page.write("| Argument | | |\n|---|---|---|\n")
            for action in positional:
                required = "required" if action.nargs != "?" else "optional"
                page.write(f"| `{action.dest}` | {required} "
                           f"| {action.help or ''} |\n")
            for action in optional:
                flag = action.option_strings[-1]
                kind = "flag" if action.nargs == 0 else "value"
                page.write(f"| `{flag}` | {kind} | {action.help or ''} |\n")
            page.write("\n")

    page.write("## Exit codes\n\n")
    page.write(
        "Worth checking from a script or a scheduled task: these commands are "
        "quiet about success and a failure that nobody reads is a failure "
        "that did not happen.\n\n"
    )
    page.write("| Code | |\n|---|---|\n")
    for code, meaning in EXIT_CODES:
        page.write(f"| `{code}` | {meaning} |\n")
    page.write("\n")

mkdocs_gen_files.set_edit_path("reference/cli.md", "../src/xams_sc/cli/xams_ctl.py")
