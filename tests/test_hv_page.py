"""How /hv reports a channel's state. See DESIGN.md §7.2, §8.1.

The decoding itself is covered in `test_hv_status.py`; what is pinned here is
what a person actually reads on the page, because that is where two mistakes
in a row were made.

**First:** the page inferred on/off from `VMON > 1`, so a channel energised at
zero volts read as "off".

**Second:** the fix used STAT bit 0 as "enabled" — but bit 0 is the OUTPUT.
The front-panel enable switch is bit 10. A channel whose switch had been
flipped on but which was not energised matched nothing and fell through to
"off" again, telling the operator their switch had not worked.

Four states, and the middle two are the ones worth care:

    disabled   bit 10          the front-panel switch is off
    enabled    neither bit     switch on, output NOT energised
    ON 0 V     bit 0, VMON~0   energised, sitting at zero volts
    ON         bit 0, VMON!=0  energised with volts out
"""

import pytest
from fastapi.testclient import TestClient

from xams_sc.api import app as app_module
from xams_sc.api.state import ChannelView


class FakeBus:
    def subscribe(self, topic, handler): pass
    def connect(self): pass
    def disconnect(self): pass
    def publish_state(self, service, state): pass
    def publish_heartbeat(self, service): pass
    def publish_measurement(self, m): pass
    def publish_raw(self, topic, payload, retain=False): pass


class StubDrift:
    def get(self):
        return {"state": "ok", "dashboards": [], "detail": "stubbed"}


def view(name, value, unit="V"):
    return ChannelView(name=name, value=value, unit=unit, quality="ok",
                       age_s=1.0)


@pytest.fixture
def page(monkeypatch):
    """Render /hv with the status word and voltage of our choosing."""
    monkeypatch.setattr(app_module, "Bus", lambda **kw: FakeBus())
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    app = app_module.create_app()
    state = app.state.system

    def render(stat_word, vmon):
        def channel(name):
            if name.endswith("_stat"):
                return None if stat_word is None else \
                    view(name, float(stat_word), "bits")
            if name.endswith("_imon"):
                return view(name, 0.0, "uA")
            return view(name, vmon)

        monkeypatch.setattr(state, "channel", channel)
        response = TestClient(app).get("/hv")
        assert response.status_code == 200
        # ONLY the status cells of the tables. The explanatory card at the
        # bottom of the page spells out "ON", "enabled" and "off" in prose,
        # so searching the whole page matches the explanation rather than the
        # reading and every assertion below would pass regardless.
        return status_cells(response.text)

    return render


def status_cells(html):
    """The rendered Status column, one entry per channel row.

    The column is located by its HEADER, not by index. An earlier version
    hardcoded `tds[4]`, and adding a VSET column silently moved Status to
    position 5 - so eight tests failed on a page that was perfectly correct.
    A test coupled to column order breaks every time the table grows, and
    teaches people that a red suite is normal.
    """
    import html as htmlmod
    import re

    cells = []
    for table in re.findall(r'<table class="channels">(.*?)</table>', html,
                            re.S):
        headers = [" ".join(re.sub(r"<[^>]+>", " ", h).split())
                   for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
        assert "Status" in headers, "the table has no Status column"
        index = headers.index("Status")

        for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(tds) <= index:
                continue                      # the header row
            text = re.sub(r"<[^>]+>", " ", tds[index])
            cells.append(" ".join(htmlmod.unescape(text).split()))
    assert cells, "no channel rows rendered"
    return cells


class TestTheFourStates:
    """The vocabulary changed on 18 September 2026, and the reason matters.

    This used to test three states, built on the belief that STAT bit 0 meant
    "enabled". **It does not.** Bit 0 is the OUTPUT being energised; the
    front-panel enable switch is bit 10. The two are different things, and a
    channel can be permitted without being energised.

    That gap had no label, so it fell through to "off" - and a channel whose
    switch had just been flipped on displayed as though it were still switched
    off. Which is exactly what was seen in the lab.

        disabled   bit 10          the front-panel switch is off
        enabled    neither bit     switch on, output NOT energised
        ON 0 V     bit 0, VMON~0   energised, sitting at zero volts
        ON         bit 0, VMON!=0  energised with volts out
    """

    def test_energised_with_volts_out_is_on(self, page):
        assert page(stat_word=1, vmon=-99.8) == ["ON"] * 8

    def test_energised_at_zero_says_so(self, page):
        """Live, and nothing on it yet. Not "off": it is one setpoint away
        from volts on an electrode."""
        assert page(stat_word=1, vmon=0.0) == ["ON 0 V"] * 8

    def test_the_switch_on_but_not_energised_reads_enabled(self, page):
        """THE CASE THAT WAS WRONG. Neither bit set: the operator has flipped
        the enable, and the channel is doing nothing until it is energised.

        Showing "off" here told them their switch had not worked.
        """
        assert page(stat_word=0, vmon=0.0) == ["enabled"] * 8

    def test_the_switch_off_reads_disabled(self, page):
        """Bit 10. Named for what it is - the switch - rather than "off",
        which is what the output does."""
        assert page(stat_word=1024, vmon=0.0) == ["disabled"] * 8

    def test_disabled_wins_over_a_decaying_voltage(self, page):
        """Bit 10 is the board's own word about the switch. The page does not
        second-guess it from a monitor still reading volts on the way down."""
        assert page(stat_word=1024, vmon=-500.0) == ["disabled"] * 8

    def test_enabled_is_not_reported_as_off(self, page):
        """The regression, stated as such: these are different states and
        must not share a word."""
        enabled = page(stat_word=0, vmon=0.0)
        disabled = page(stat_word=1024, vmon=0.0)

        assert enabled != disabled
        assert "off" not in enabled


class TestFaultsAndGaps:
    def test_a_trip_is_shown(self, page):
        assert page(stat_word=(1 << 0) | (1 << 7), vmon=0.0) == ["TRIP"] * 8

    def test_a_fault_replaces_the_state_rather_than_hiding_behind_it(self, page):
        cells = page(stat_word=(1 << 0) | (1 << 12), vmon=-99.8)
        assert cells == ["INTERLOCK"] * 8
        assert "ON" not in cells

    def test_several_faults_are_all_shown(self, page):
        cells = page(stat_word=(1 << 7) | (1 << 3), vmon=0.0)
        assert cells == ["OVER_CURRENT, TRIP"] * 8

    def test_no_status_word_says_so_instead_of_guessing(self, page):
        """With no STAT the page must NOT fall back to inferring from the
        voltage. That inference is the bug this whole change removes."""
        assert page(stat_word=None, vmon=-99.8) == ["no reading"] * 8
