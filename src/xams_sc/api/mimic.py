"""Does the P&ID drawing still match the configuration? See DESIGN.md §8.2.

A drift check, in the same spirit as the Grafana one (§12): a mimic that has
quietly stopped showing a channel is worse than one that never showed it,
because it looks complete. Reported at startup rather than enforced — a
drawing lagging a new sensor by a day is not a reason to refuse to serve the
page.
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).parent

def check_mimic_tags(config) -> list[str]:
    """Compare the SVG's ids against channels.yaml, in BOTH directions (§8.2).

    The SVG is a copy of a drawing that will eventually change, and a mimic
    quietly out of date with the plant is a liability. This is what makes that
    visible rather than silent — roughly ten lines, and it is what makes the
    page survivable three years from now.

    Both directions matter and they fail differently:

      * an id with no channel  — the drawing shows an instrument this system
        does not read, and the bubble would sit empty forever
      * a channel with no id   — a reading nobody can find on the drawing,
        which is the half-identity §3 warns about

    Returns the problems. Logged at startup; never fatal, because a mimic that
    has drifted is still more useful than no mimic.
    """
    svg_path = HERE / "static" / "xams_pid.svg"
    if not svg_path.exists():
        return ["the mimic SVG has not been built (run tools/build_mimic.py)"]

    ids = set(re.findall(r'id="v-([^"]+)"', svg_path.read_text(encoding="utf-8")))
    problems = []

    for name in sorted(ids - set(config.channels)):
        problems.append(f"the drawing has a value slot for {name!r}, which is "
                        f"not a channel in channels.yaml")

    # Only channels that describe a point in the plant belong on a P&ID. HV,
    # UPS, heaters and derived values have no place on a piping drawing, and
    # listing them as missing would be noise that trains people to ignore this.
    on_drawing = {c.name for c in config.enabled_channels()
                  if c.on_pid and (c.kind in ("rtd", "temperature")
                                   or (c.kind in ("voltage", "current")
                                       and c.device == "cdaq"))}
    for name in sorted(on_drawing - ids):
        problems.append(f"{name!r} is read but has no place on the drawing")
    return problems
