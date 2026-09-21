"""The P&ID page's column and its liveness. See DESIGN.md §8.2.

Two things are pinned here, and the second is the one that matters.

**Order.** The alarm card grows and shrinks as alarms come and go. Anything
below it therefore moves, so it sits at the BOTTOM and the cards whose height
never changes sit above it. What replaces it at the top is a fixed-height line
that says only whether something is wrong — glance at that, read the card
afterwards.

**Liveness.** Every channel that stops reading already goes to a dash rather
than its last good number. The case that was NOT handled is the page itself
going quiet: `refresh` caught its own fetch failure and returned, keeping what
was drawn, so a dead web UI or a dropped network left a screen full of frozen
plausible values looking live. On the one page that is left open on a display
all day, that is the exact failure principle 4 names.
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture
def page(client):
    response = client.get("/mimic")
    assert response.status_code == 200
    return response.text


def headings(html):
    """The card titles, without the "change ->" links that live inside them."""
    return [re.sub(r"<[^>]+>", "", re.sub(r"<a\b.*?</a>", "", h, flags=re.S)).strip()
            for h in re.findall(r"<h2>(.*?)</h2>", html, re.S)]


class TestTheColumnOrder:
    def test_the_cards_are_in_the_order_somebody_reads_them(self, page):
        sidebar = [h for h in headings(page)
                   if h != "P&amp;I &mdash; live values"]

        assert sidebar == ["Cryostat setpoint", "Xenon moved", "High voltage",
                           "Mains", "Alarms"]

    def test_alarms_are_last_because_that_card_changes_height(self, page):
        assert headings(page)[-1] == "Alarms", \
            "a card that grows must not push the stable ones down the page"

    def test_the_status_line_comes_before_every_card(self, page):
        assert page.index('id="s-status"') < page.index("Cryostat setpoint")


class TestTheDrawingIsNotRepeated:
    def test_no_sidebar_card_shows_a_pressure_or_a_temperature(self, page, config):
        """Strictly the complement of the drawing. A reading with two homes on
        one page is fine until the day they disagree."""
        sidebar = page[page.index('<aside class="mimic-side">'):page.index("</aside>")]

        on_drawing = [c.name for c in config.enabled_channels()
                      if c.kind in ("rtd", "temperature") and c.on_pid]
        repeated = [n for n in on_drawing if 's-' + n in sidebar]

        assert repeated == [], f"already on the drawing: {repeated}"


class TestLiveness:
    def test_the_page_can_say_it_has_stopped_updating(self, page):
        assert "markStale" in page
        assert "NOT UPDATING" in page

    def test_one_dropped_request_does_not_blank_the_page(self, page):
        """Making the whole mimic flicker because a packet went missing would
        be worse than the packet."""
        assert "STALE_AFTER_MS" in page
        assert re.search(r"Date\.now\(\)\s*-\s*lastOk\s*<\s*STALE_AFTER_MS", page), \
            "staleness is decided on the first failure rather than on elapsed time"

    def test_a_throttled_tab_is_still_caught(self, page):
        """A tab the browser has stopped scheduling never reaches the catch,
        so elapsed time is checked on its own timer too."""
        assert re.search(r"setInterval\(\(\)\s*=>\s*\{\s*\n?\s*if \(lastOk", page)

    def test_the_drawing_is_withdrawn_and_not_merely_annotated(self, page):
        """A banner over a drawing that still looks live is half a warning."""
        assert ".mimic-body.stale" in page
        assert 'body.classList.add("stale")' in page

    def test_going_stale_is_reversible(self, page):
        assert 'body.classList.remove("stale")' in page


class TestWhereControlLives:
    """The P&ID does not write to instruments; it says where to (§8.2).

    This is the page left open on a screen all day, and a setpoint box on it
    is the wrong thing to reach for by accident. The cards link to the
    Control page instead, which costs a deliberate navigation — the same
    reasoning that keeps the CAEN front-panel enable a hand operation.
    """

    def test_the_drawing_writes_to_no_instrument(self, page):
        """The `/operator` form in the header is not one: it sets a cookie
        naming who is at the keyboard, which is how the audit trail gets a
        name, and it touches no hardware."""
        for action in ('action="/hv/apply"', 'action="/hv/output"',
                       'action="/lakeshore/setpoint"',
                       'action="/lakeshore/range"', 'action="/flow/reset"'):
            assert action not in page, f"{action} appeared on the P&ID"

    def test_each_controllable_card_says_where_to_change_it(self, page):
        for anchor in ("/hv#cryostat", "/hv#flow", "/hv#hv"):
            assert anchor in page, f"no way through to {anchor}"

    def test_the_read_only_cards_offer_nothing(self, page):
        """Mains and Alarms are not controls and must not pretend to be."""
        mains = page[page.index("<h2>Mains"):page.index("<h2>Alarms")]

        assert "change &rarr;" not in mains
