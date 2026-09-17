"""How /hv reports a channel's state. See DESIGN.md §7.2, §8.1.

The decoding itself is covered in `test_hv_status.py`; what is pinned here is
what a person actually reads on the page, because that is where the original
mistake was. The page inferred on/off from `VMON > 1` and so showed "off" for
a channel that was switched on at zero volts.

Three distinct states, and the difference between the first two matters:

    ON       putting volts out
    enabled  switched on, sitting at zero  — LIVE, not off
    off      output disabled
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
    """The rendered Status column, one entry per channel row."""
    import html as htmlmod
    import re

    cells = []
    for table in re.findall(r'<table class="channels">(.*?)</table>', html,
                            re.S):
        for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(tds) < 5:
                continue                      # the header row
            text = re.sub(r"<[^>]+>", " ", tds[4])
            cells.append(" ".join(htmlmod.unescape(text).split()))
    assert cells, "no channel rows rendered"
    return cells


class TestTheThreeStates:
    def test_enabled_and_at_voltage_is_on(self, page):
        assert page(stat_word=1, vmon=-99.8) == ["ON"] * 8

    def test_enabled_and_at_zero_says_enabled_not_off(self, page):
        """The case the original page got wrong.

        It must not say "off": the output is switched on and one setpoint
        away from putting volts on an electrode.
        """
        assert page(stat_word=1, vmon=0.0) == ["enabled"] * 8

    def test_enabled_and_at_zero_does_not_say_on(self, page):
        """"enabled" is the word asked for. "ON" would overstate it — there
        are no volts on the output."""
        assert "ON" not in page(stat_word=1, vmon=0.0)

    def test_disabled_is_off(self, page):
        assert page(stat_word=1024, vmon=0.0) == ["off"] * 8

    def test_disabled_at_voltage_is_still_off(self, page):
        """Bit 10 wins over any voltage still on the monitor as it decays.
        The word comes from the board; the page does not second-guess it."""
        assert page(stat_word=1024, vmon=-500.0) == ["off"] * 8


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
