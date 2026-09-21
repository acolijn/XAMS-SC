"""The HV confirmation says what is about to change. See §8.1, §10a.

**The most dangerous control in the system had the weakest confirmation.**
It was one sentence - "Apply the setpoints you have entered?" - on a form
that can move eight channels at once. DESIGN §8.1 asks for the new value to
be RETYPED as deliberate friction, and that was never built, so the document
promised a safeguard the page did not have. That is its own hazard: somebody
reading the design trusts friction that is not there.

What the dialog catches, and what it does not. An extra zero - the failure
§8.1 cites - is already refused twice before it reaches an electrode: by the
software range in channels.yaml (caen.py) and by the board's own MAXV. It
cannot be the reason for this. The mistake nothing downstream can catch is
the WRONG-BUT-LEGAL value: the gate's voltage typed into the cathode's box.
It is in range, the supply accepts it, and the only place it can be caught
is in front of the person who typed it, by naming the channel and the two
numbers.

The behavioural half runs the real function, sliced out of hv.html, under
node. The static half holds down the things a browser would only reveal at
the worst moment: that the handler names a function the page actually
defines, and that the limits quoted are the VSET channel's, which is the
pair the supply validates against.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from doubles import RecordingBus, StubDrift, ui_client
from xams_sc.api import app as app_module
from xams_sc.api.state import ChannelView
from xams_sc.config import load
from xams_sc.hv_status import BIT_ON

HV_TEMPLATE = (pathlib.Path(app_module.__file__).parent / "templates"
               / "hv.html")


@pytest.fixture
def energised_page(monkeypatch):
    """/hv with a readable status word, so the boxes actually render.

    Without a supply answering, every channel shows "no status" and its box
    is disabled - correctly, since a channel whose state is unknown must not
    be given a setpoint (§10a). A test that rendered that page would find no
    boxes and assert nothing, which is how this file first passed.
    """
    monkeypatch.setattr(app_module, "Bus", lambda **kw: RecordingBus())
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    app = app_module.create_app()
    state = app.state.system

    def channel(name):
        unit = "bits" if name.endswith("_stat") else "V"
        value = float(1 << BIT_ON) if name.endswith("_stat") else -2250.0
        return ChannelView(name=name, value=value, unit=unit, quality="ok",
                           age_s=1.0)

    monkeypatch.setattr(state, "channel", channel)
    response = ui_client(app).get("/hv")
    assert response.status_code == 200
    return response.text


class TestTheHandlerIsWiredUp:
    def test_the_page_defines_the_function_its_handler_calls(self, client):
        """An inline handler that throws does NOT cancel the submit.

        So a missing `confirmSetpoints` is not a broken dialog, it is a form
        that applies high voltage with no confirmation at all - silently.
        This is the same failure class test_template_handlers.py exists for.
        """
        body = client.get("/hv").text
        assert "confirmSetpoints(this)" in body
        assert "function confirmSetpoints" in body, (
            "the handler calls a function this page does not define; the "
            "form would submit unconfirmed")

    def test_every_setpoint_box_carries_what_the_dialog_needs(
            self, energised_page):
        boxes = re.findall(r"<input[^>]*class=\"vset-box\"[^>]*>",
                           energised_page)
        fillable = [b for b in boxes if "name=" in b]
        assert fillable, "no channel could take a setpoint on this page"
        for box in fillable:
            for attribute in ("data-label", "data-now", "data-energised",
                              "data-min", "data-max"):
                assert attribute in box, (attribute, box[:120])

    def test_the_range_quoted_is_the_one_the_supply_enforces(
            self, energised_page):
        """The VSET channel's limits, not the VMON channel's.

        They carry the same pair today. Taking the VMON one because it is
        already in scope is exactly how `channels_for(self.name)` came to
        mark nothing at all - a coincidence doing the work of a decision.
        """
        config = load()
        body = energised_page
        for name, channel in config.channels.items():
            if channel.kind != "hv_vset" or not channel.limits:
                continue
            box = re.search(r"<input[^>]*name=\"%s\"[^>]*>" % re.escape(name),
                            body)
            if box is None:
                continue          # this channel cannot take one right now
            assert 'data-min="%s"' % channel.limits["min"] in box.group(0)
            assert 'data-max="%s"' % channel.limits["max"] in box.group(0)


class TestWhatTheDialogSays:
    """The real function, against a fake form."""

    @pytest.fixture
    def ask(self, tmp_path):
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed; the static checks still run")

        source = HV_TEMPLATE.read_text(encoding="utf-8")
        start = source.index("function confirmSetpoints")
        end = source.index("// THE SECOND CLICK")

        harness = tmp_path / "harness.js"
        harness.write_text(source[start:end] + """
var shown = null, alerted = null;
global.confirm = function (text) { shown = text; return true; };
global.alert = function (text) { alerted = text; };
var boxes = JSON.parse(process.argv[2]).map(function (b) {
  return {name: b.name, value: b.value,
          classList: {contains: function (c) { return c === 'vset-box'; }},
          dataset: b.dataset};
});
var answer = confirmSetpoints({elements: boxes});
console.log(JSON.stringify({shown: shown, alerted: alerted, answer: answer}));
""", encoding="utf-8")

        def run(boxes):
            out = subprocess.run([node, str(harness), json.dumps(boxes)],
                                 capture_output=True, text=True, check=True)
            return json.loads(out.stdout)
        return run

    def box(self, name, value, label=None, now="-2250.0", energised="1",
            lo="-2500.0", hi="0.0"):
        return {"name": name, "value": value,
                "dataset": {"label": label or name, "now": now,
                            "energised": energised, "min": lo, "max": hi}}

    def test_it_names_the_channel_and_both_numbers(self, ask):
        """THE POINT. Not "the setpoints you have entered"."""
        result = ask([self.box("hv_cathode_vset", "-1750", label="cathode")])
        assert "cathode" in result["shown"]
        assert "-2250.0" in result["shown"], "what it holds now is missing"
        assert "-1750" in result["shown"], "what it is moving to is missing"

    def test_an_empty_box_is_not_mentioned(self, ask):
        result = ask([self.box("hv_cathode_vset", "-1750", label="cathode"),
                      self.box("hv_gate_vset", "", label="gate")])
        assert "cathode" in result["shown"]
        assert "gate" not in result["shown"]
        assert "Apply 1 setpoint?" in result["shown"]

    def test_it_says_which_channels_move_now(self, ask):
        """§10a's middle state: switched on, not energised, so the value is
        stored and nothing ramps until somebody turns it on."""
        result = ask([
            self.box("hv_cathode_vset", "-1750", label="cathode",
                     energised="1"),
            self.box("hv_gate_vset", "-500", label="gate", energised="0"),
        ])
        lines = {line.split(":")[0].strip(): line
                 for line in result["shown"].splitlines() if ":" in line}
        assert "ramps now" in lines["cathode"]
        assert "stored until you turn it on" in lines["gate"]

    def test_a_value_out_of_range_is_flagged_but_not_refused(self, ask):
        """Reported, never blocked. The supply is the authority on the range
        (§8.1); a page that refused on its own would be a second gate to keep
        in step with channels.yaml."""
        result = ask([self.box("hv_cathode_vset", "-9000", label="cathode")])
        assert "WARNING" in result["shown"]
        assert "-2500" in result["shown"] and "outside" in result["shown"]
        assert result["answer"] is True, "the page refused on its own"

    def test_something_that_is_not_a_number_is_named(self, ask):
        result = ask([self.box("hv_cathode_vset", "twelve", label="cathode")])
        assert "not a number" in result["shown"]

    def test_an_unknown_present_value_is_not_invented(self, ask):
        """A channel whose VSET could not be read says so rather than
        showing a number that is not on the board."""
        result = ask([self.box("hv_cathode_vset", "-1750", label="cathode",
                               now="")])
        assert "unknown" in result["shown"]

    def test_every_box_empty_stops_before_the_round_trip(self, ask):
        result = ask([self.box("hv_cathode_vset", ""),
                      self.box("hv_gate_vset", "")])
        assert result["answer"] is False, "an empty form was submitted"
        assert result["shown"] is None, "a confirmation was asked for nothing"
        assert "nothing to apply" in result["alerted"]
