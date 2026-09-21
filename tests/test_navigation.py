"""Which page answers which question. See DESIGN.md §8.1, §8.2.

The site used to open on an Overview that answered two questions at once —
"is everything all right?" and "change something" — because the Lake Shore
and the flow reset had nowhere else to be. They are separate questions, asked
at different moments, and they are now separate pages:

    /         the plant, drawn, live, READ-ONLY
    /hv       Control: everything that writes to an instrument
    /system   System health: is the software all right
    /status   the channel list
    /alarms   the alarm chain
    /logs     the service logs

What is pinned here is that every one of them answers, that the old
bookmarks still work, and that no page grew a second job.
"""

from __future__ import annotations

import re

import pytest

#: Every form in this system that reaches an instrument or closes a record.
#: `/operator` is deliberately absent: it sets a cookie saying who is at the
#: keyboard, which is how the audit trail gets a name, and it touches nothing.
INSTRUMENT_WRITES = ('action="/hv/apply"', 'action="/hv/output"',
                     'action="/lakeshore/setpoint"', 'action="/lakeshore/range"',
                     'action="/flow/reset"')

PAGES = ("/", "/status", "/hv", "/hv/defaults", "/system", "/alarms", "/logs")


class TestEveryPageAnswers:
    @pytest.mark.parametrize("path", PAGES)
    def test_it_renders(self, client, path):
        assert client.get(path).status_code == 200


class TestTheOldAddressesStillWork:
    def test_the_mimic_url_redirects_to_the_landing_page(self, client):
        """It is in the manual, in bookmarks and on a wall-display kiosk."""
        r = client.get("/mimic", follow_redirects=False)

        assert r.status_code == 301
        assert r.headers["location"] == "/"

    def test_following_it_arrives_at_the_drawing(self, client):
        r = client.get("/mimic")

        assert r.status_code == 200
        assert "P&amp;I" in r.text


class TestNoPageHasTwoJobs:
    def test_the_landing_page_writes_to_no_instrument(self, client):
        body = client.get("/").text

        for action in INSTRUMENT_WRITES:
            assert action not in body, f"{action} appeared on the P&ID"

    def test_system_health_writes_nothing(self, client):
        """It answers "is the software all right", and nothing else. The two
        control cards it used to carry are the reason this page felt like two
        pages stapled together."""
        body = client.get("/system").text

        assert 'action="/lakeshore/setpoint"' not in body
        assert 'action="/flow/reset"' not in body

    def test_control_carries_every_write_in_the_system(self, client):
        body = client.get("/hv").text

        for action in INSTRUMENT_WRITES:
            assert action in body, f"{action} is not on the Control page"

    def test_control_still_explains_what_it_will_not_do(self, client):
        """The §10a limits — no enable, no MAXV, no TRIP. That is the text
        that stays, scoped to the high-voltage section."""
        body = client.get("/hv").text

        assert "what this page cannot do" in body
        assert "front-panel enable switch" in body


class TestTheNavBar:
    def test_it_offers_the_pages_it_should(self, client):
        nav = client.get("/").text
        nav = nav[nav.index("<nav>"):nav.index("</nav>")]

        labels = re.findall(r">([^<>]+)</a>", nav)
        assert [l.strip() for l in labels][:4] == [
            "P&amp;I", "Channels", "Control", "System health"]

    def test_every_internal_link_resolves(self, client):
        """A nav that points at a 404 is worse than one that is missing it."""
        html = client.get("/").text
        nav = html[html.index("<nav>"):html.index("</nav>")]

        for href in re.findall(r'href="(/[^"#]*)', nav):
            assert client.get(href).status_code == 200, href
