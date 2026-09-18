"""Every name the package uses must exist. See DESIGN.md §13.

**This test exists because the same mistake got through twice in one day, and
the second time it was committed.**

A patch that rewrote part of `cli/xams_ctl.py` replaced a span of the file that
was wider than intended, deleting `_print_bus_status` and
`_print_dashboard_drift` while leaving the calls to them in place. Python does
not resolve a global until the line runs, so:

  * every module still imported cleanly,
  * every other test still passed,
  * `xams-ctl status` crashed with `NameError` at the very end of its output,
    after printing everything that looked right.

It survived review because the check that should have caught it was
`xams-ctl status | grep backup`, and grep threw the traceback away.

Unit tests do not catch this. A module that imports is not a module whose
functions can run, and a `NameError` in a branch nothing exercises is
invisible until somebody in the lab runs the command.

Only UNDEFINED NAMES are treated as failures here. Unused imports are left
alone deliberately: `devices/caen.py` re-exports names from `hv_status` on
purpose, and a test that forces every re-export to be deleted would be
actively harmful.
"""

import ast
from pathlib import Path

import pytest

pyflakes = pytest.importorskip(
    "pyflakes",
    reason="pyflakes is not installed; `pip install -e .[dev]`")

from pyflakes import messages as pyflakes_messages  # noqa: E402
from pyflakes.checker import Checker  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "xams_sc"

# The failures that mean "this line will raise when it runs".
FATAL = (
    pyflakes_messages.UndefinedName,
    pyflakes_messages.UndefinedLocal,
    pyflakes_messages.UndefinedExport,
)


def python_files():
    return sorted(p for p in PACKAGE.rglob("*.py")
                  if "__pycache__" not in p.parts)


def test_there_are_files_to_check():
    """A glob that silently matches nothing is a test that always passes."""
    assert len(python_files()) > 10


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_no_undefined_names(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    problems = [m for m in Checker(tree, filename=str(path)).messages
                if isinstance(m, FATAL)]

    assert not problems, "\n".join(
        f"{path.relative_to(PACKAGE.parents[1])}:{m.lineno}: {m.message % m.message_args}"
        for m in problems)
