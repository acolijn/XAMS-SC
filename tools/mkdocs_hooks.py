"""Translate the PDF pipeline's Markdown extensions for MkDocs.

`notes/md2pdf.py` defines two extensions used by the design documents, and
neither means anything to MkDocs:

  * `:::warn`, `:::box`, `:::note` callout blocks. Worse than merely ignored —
    `:::` is also **mkdocstrings' autodoc marker**, so `:::box` is read as a
    request to document a Python module named `box` and the build fails.
  * `{+}` and `{-}` cell markers, which md2pdf colours green and red.

The alternative was to change the sources to Material's `!!!` admonitions,
which would have broken the PDFs — and the PDFs are what gets sent to people
who are not going to clone a repository. So the sources stay as they are and
the translation happens here, on the way into the site. One syntax, two
renderers.

If the PDF pipeline is ever retired, delete this file and rewrite the blocks
in the sources.
"""

from __future__ import annotations

import re

# md2pdf kind -> Material admonition type. `box` is bordered and `note` grey
# in the PDFs; the nearest Material equivalents are used rather than inventing
# new ones with custom CSS.
KINDS = {"box": "note", "note": "info", "warn": "warning"}

BLOCK = re.compile(
    r"^:::(box|note|warn)[ \t]*\n(.*?)^:::[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def _callout(match: re.Match) -> str:
    kind = KINDS[match.group(1)]
    body = match.group(2).strip("\n")

    # A callout usually opens with its own bolded title line. Promote it to
    # the admonition title, which is where Material expects it, rather than
    # leaving a bold line inside the box repeating what the colour says.
    lines = body.split("\n")
    title = ""
    if lines and re.fullmatch(r"\*\*(.+?)\*\*", lines[0].strip()):
        title = lines[0].strip()[2:-2]
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)

    header = f'!!! {kind} "{title}"' if title else f"!!! {kind}"
    indented = "\n".join(("    " + line) if line.strip() else ""
                         for line in lines)
    return f"{header}\n\n{indented}\n"


def on_page_markdown(markdown: str, **kwargs) -> str:
    markdown = BLOCK.sub(_callout, markdown)
    # No colour without custom CSS, so the marker becomes a glyph instead.
    # The point of these is to be legible at a glance in a comparison table,
    # and a tick does that where a stripped marker would not.
    markdown = markdown.replace("{+}", "&#10004;&nbsp;")
    markdown = markdown.replace("{-}", "&#10008;&nbsp;")
    return markdown
