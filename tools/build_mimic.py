"""Build the P&ID mimic SVG from the drawing. See DESIGN.md §8.2.

    python tools/build_mimic.py

Converts `docs/xams_piping_and_instrumentation.pdf` into
`src/xams_sc/api/static/xams_pid.svg`, and adds an empty `<text>` node beside
every instrument tag that corresponds to a channel. The web page fills those
in; the SVG itself is a static file.

**Re-run this when the P&ID is revised.** The drawing will eventually change,
and a mimic quietly out of date with the plant is a liability (§8.2). The tag
check below is what makes that visible rather than silent.

TAG NAMES. The drawing is the authoritative list (§3), and mostly the tags are
our channel names in lower case. The pressures are the exception: the P&ID
calls them **PT101–PT104** while `channels.yaml` calls them **p101–p104**,
following what the LabVIEW system logged. Both refer to the same transmitters.
The mapping is declared in TAG_ALIASES rather than inferred, so that the
divergence is stated rather than hidden inside a regular expression.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xams_sc.config import load  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "docs" / "xams_piping_and_instrumentation.pdf"
OUT = ROOT / "src" / "xams_sc" / "api" / "static" / "xams_pid.svg"

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

# A P&ID tag that is not simply the channel name lower-cased.
TAG_ALIASES = {
    "PT101": "p101", "PT102": "p102", "PT103": "p103", "PT104": "p104",
    # PT201 on the drawing is what channels.yaml calls pmain, the detector
    # pressure. Confirmed by A.P. Colijn, 17 September 2026. Without this the
    # most important channel in the system has no place on the mimic.
    "PT201": "pmain",
}

TAG_PATTERN = re.compile(r"[A-Z]{1,3}\d{2,3}")


def channel_for(tag: str, channels: set[str]) -> str | None:
    if tag in TAG_ALIASES:
        return TAG_ALIASES[tag] if TAG_ALIASES[tag] in channels else None
    return tag.lower() if tag.lower() in channels else None


def main() -> int:
    config = load()
    # Every channel in the file, not just the enabled ones: a disabled channel
    # is still a real instrument on the drawing and should get a place to show
    # that it is out of service.
    known = set(config.channels)

    doc = pymupdf.open(PDF)
    page = doc[0]
    width, height = page.rect.width, page.rect.height

    svg_text = page.get_svg_image(text_as_path=False)
    root = ET.fromstring(svg_text)

    # ROTATE THE DRAWING 90 DEGREES COUNTER-CLOCKWISE.
    #
    # The PDF is stored portrait with the drawing on its side: 88 of its 90
    # text lines run vertically (direction 0,1). Rotating the page object does
    # not help — pymupdf still emits the original orientation — so the
    # rotation is applied here, in the SVG.
    #
    # rotate(-90) sends (x, y) to (y, -x); translate(0, width) brings it back
    # on screen, so the content ends up spanning (0..height, 0..width) and the
    # viewBox swaps its dimensions.
    body = ET.Element(f"{{{SVG_NS}}}g")
    body.set("transform", f"translate(0,{width:.1f}) rotate(-90)")
    for child in list(root):
        root.remove(child)
        body.append(child)
    root.append(body)

    root.set("viewBox", f"0 0 {height:.0f} {width:.0f}")
    root.set("width", f"{height:.0f}")
    root.set("height", f"{width:.0f}")

    def rotated(x: float, y: float) -> tuple[float, float]:
        """Where a point in the original drawing ends up after the rotation."""
        return (y, width - x)

    # Instrument bubbles: the empty circles the drawing puts beside each tag.
    # A value belongs INSIDE its bubble — that is what makes a mimic read as a
    # mimic rather than as a drawing with numbers scattered over it.
    bubbles = []
    for item in page.get_drawings():
        r = item["rect"]
        if not (18 <= r.width <= 40 and 18 <= r.height <= 40):
            continue
        if abs(r.width - r.height) > 0.25 * max(r.width, r.height):
            continue
        bubbles.append(r)

    def bubble_near(cx: float, cy: float):
        """The instrument bubble belonging to a tag, if it has one.

        Not every tag does. TT201-TT207 are labels with leader lines pointing
        into the detector vessel, and their nearest circle is over 100 pt away
        — a value dropped there would land on an unrelated instrument. Those
        get their value beside the label instead.
        """
        best, best_d2 = None, 30.0 ** 2
        for r in bubbles:
            bx, by = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
            d2 = (bx - cx) ** 2 + (by - cy) ** 2
            if d2 < best_d2:
                best, best_d2 = (bx, by), d2
        return best

    placed, unmatched = {}, []
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        if not TAG_PATTERN.fullmatch(word):
            continue
        channel = channel_for(word, known)
        if channel is None:
            unmatched.append(word)
            continue
        if channel in placed:
            continue

        # The value goes OUTSIDE the rotated group, in final coordinates.
        # Inside it, it would be rotated with everything else and end up
        # sideways — readable numbers are the whole point of this page.
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        bubble = bubble_near(cx, cy)
        if bubble is not None:
            fx, fy = rotated(*bubble)
            fy += 3.2                       # optical centre of the circle
        else:
            fx, fy = rotated(x1, cy)        # just past the end of the label
            fx += 16
            fy += 3.2
        # TWO elements: a white outline underneath, then the number on top.
        #
        # The obvious way to get a halo is paint-order="stroke" on one element,
        # but that attribute is not universally supported — where it is
        # ignored the stroke paints OVER the glyph and a 2.5 px outline
        # swallows a 9 px character entirely. Drawing the halo as a separate
        # element underneath works everywhere, which matters for a page that
        # has to keep working for years without anyone maintaining it.
        common = {
            "x": f"{fx:.1f}", "y": f"{fy:.1f}", "text-anchor": "middle",
            "font-family": "Consolas, monospace", "font-size": "9",
            "font-weight": "bold",
        }
        halo = ET.SubElement(root, f"{{{SVG_NS}}}text")
        # id first: ElementTree writes attributes in the order they are set,
        # and a file where every element starts with its id is far easier to
        # grep and to read three years from now.
        halo.set("id", f"h-{channel}")
        for k, v in common.items():
            halo.set(k, v)
        halo.set("fill", "none")
        halo.set("stroke", "#ffffff")
        halo.set("stroke-width", "3")
        halo.set("stroke-linejoin", "round")
        halo.text = "—"

        value = ET.SubElement(root, f"{{{SVG_NS}}}text")
        value.set("id", f"v-{channel}")
        for k, v in common.items():
            value.set(k, v)
        value.set("fill", "#8b95a6")
        value.text = "—"
        placed[channel] = (word, "bubble" if bubble is not None else "beside")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))

    print(f"  wrote {OUT.relative_to(ROOT)}  ({OUT.stat().st_size/1024:.0f} kB)")
    print(f"  rotated 90 CCW: {width:.0f}x{height:.0f} -> {height:.0f}x{width:.0f}")
    print(f"  {len(placed)} value placeholders added")
    for channel, (tag, how) in sorted(placed.items()):
        note = f"  (drawing calls it {tag})" if tag.lower() != channel else ""
        print(f"    v-{channel:10s} {how:7s}{note}")
    print(f"\n  {len(set(unmatched))} tag(s) on the drawing are not instrumented:")
    print("   ", ", ".join(sorted(set(unmatched))))
    print("\n  That is informative in itself: it shows at a glance how much of")
    print("  the plant the slow control actually sees (§8.2).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
