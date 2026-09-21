"""The ten-second reload, and the guard that decides when to skip it.

**The page stopped reloading altogether and nothing noticed for weeks.** The
guard in `base.html` asks "does this page differ from what the server sent?",
so that a reload never throws away a setpoint half typed. For a `<select>` it
compared `selectedIndex` against `defaultSelectedIndex` - a property that
does not exist. `0 !== undefined` is true every time, so the guard always
answered "yes, unsent input", `tick()` rescheduled itself forever and
`location.reload()` never ran.

One `<select>` exists in this interface, the heater range, and it is on the
Control page - the page showing the high voltage. A setpoint written or a
supply energised therefore sat there looking unchanged until somebody pressed
F5, which is the opposite of what an auto-refreshing control page is for, and
it is exactly the kind of silent client-side failure `test_template_handlers`
was written about.

The behavioural half of this runs the real functions out of `base.html`
under node. Where node is absent the file still holds down the property name
that broke, which is the part that can regress by hand.
"""

import pathlib
import re
import shutil
import subprocess

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / \
    "src" / "xams_sc" / "api" / "templates"
BASE = TEMPLATES / "base.html"

PAGES = ["/", "/status", "/system", "/hv", "/hv/defaults", "/alarms", "/logs"]


def strip_comments(html: str) -> str:
    """The page with its HTML and JavaScript comments removed.

    Prose about a `<select>` is not a `<select>`, and this file is about what
    the browser will actually run.
    """
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    return re.sub(r"/\*.*?\*/", "", html, flags=re.S)


class TestThePropertyThatDoesNotExist:
    def test_no_page_compares_against_defaultselectedindex(self, client):
        """The bug itself, in the rendered output of every page."""
        for path in PAGES:
            assert "defaultSelectedIndex" not in client.get(path).text, path

    def test_the_template_does_not_mention_it_either(self):
        """A comment reintroducing the name would be harmless; the point is
        that nobody reading this file is told it exists."""
        assert "defaultSelectedIndex" not in BASE.read_text(encoding="utf-8")

    def test_every_select_is_covered_by_the_guard(self, client):
        """A page with a `<select>` and no `defaultIndex` is the old bug back.

        Tied to the markup rather than to the one select we know about: the
        next `<select>` added to this site gets the same protection, or this
        fails and says why.

        The mimic is the one page with no guard and needs none - it overrides
        the refresh block and fetches /api/state every five seconds instead -
        so this must find REAL elements. Both comment styles talk about
        `<select>` in prose (the operator box explains why it is not one) and
        matching that text would have failed the mimic for a control it does
        not have.
        """
        for path in PAGES:
            body = strip_comments(client.get(path).text)
            if not re.search(r"<select\b", body):
                continue
            assert "defaultIndex(" in body, path


class TestTheGuardStillGuards:
    """Fixing the reload must not cost the thing the guard exists for.

    A reload that never runs is a stale page; a reload that runs over a
    half-typed setpoint is worse, because the operator watched 600 V turn
    back into an empty box (see the comment in base.html).
    """

    @pytest.fixture
    def run_guard(self, tmp_path):
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed; the static checks still run")

        html = BASE.read_text(encoding="utf-8")
        start = html.index("function defaultIndex")
        end = html.index("function typing()")

        harness = tmp_path / "harness.js"
        harness.write_text(
            # The REAL functions, sliced out of the template that ships, so
            # this cannot drift from the page the way a copy would.
            html[start:end] + """
let ELEMENTS = [];
global.document = { querySelectorAll: () => ELEMENTS };
ELEMENTS = JSON.parse(process.argv[2]).map(function (e) {
  if (e.tagName !== 'SELECT') return e;
  e.options = e.defaults.map(function (d) { return {defaultSelected: d}; });
  return e;
});
console.log(unsent() ? "unsent" : "clean");
""", encoding="utf-8")
        import json

        def run(elements):
            out = subprocess.run([node, str(harness), json.dumps(elements)],
                                 capture_output=True, text=True, check=True)
            return out.stdout.strip() == "unsent"
        return run

    def select(self, index, defaults, multiple=False):
        return {"tagName": "SELECT", "disabled": False, "type": "select-one",
                "selectedIndex": index, "multiple": multiple,
                "defaults": defaults}

    def text(self, value, default):
        return {"tagName": "INPUT", "type": "text", "disabled": False,
                "value": value, "defaultValue": default}

    def checkbox(self, checked, default):
        return {"tagName": "INPUT", "type": "checkbox", "disabled": False,
                "checked": checked, "defaultChecked": default}

    def test_the_heater_select_as_rendered_does_not_block_the_reload(
            self, run_guard):
        """No option carries `selected`, so the browser shows the first one
        and that is what the operator is looking at. THE BUG."""
        assert run_guard([self.select(0, [False, False])]) is False

    def test_a_select_the_operator_moved_does_block_it(self, run_guard):
        assert run_guard([self.select(1, [False, False])]) is True

    def test_a_marked_option_is_the_default_not_the_first_one(self, run_guard):
        assert run_guard([self.select(1, [False, True])]) is False
        assert run_guard([self.select(0, [False, True])]) is True

    def test_a_filled_setpoint_box_still_stops_the_reload(self, run_guard):
        """The reason the guard exists, on the page that has the select."""
        assert run_guard([self.select(0, [False, False]),
                          self.text("600", "")]) is True

    def test_a_ticked_checkbox_still_stops_it(self, run_guard):
        """`remove` on the Alarms page: its value is "on" either way, so only
        `checked` sees it."""
        assert run_guard([self.checkbox(True, False)]) is True

    def test_an_untouched_page_reloads(self, run_guard):
        assert run_guard([self.select(0, [False, False]),
                          self.text("", ""),
                          self.checkbox(False, False)]) is False
