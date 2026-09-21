"""The flow-integrator reset in the web UI. See DESIGN.md §7.5, §8.1, §10.

This is the ONLY thing the web UI can change, and it is allowed to exist
before milestone 8 because it changes a record rather than an instrument:
the closed period keeps its total and its gaps in `flow_periods`. Nothing is
erased, which is the whole difference between this and zeroing a counter.

What these tests hold down is that it stays that way — that the UI asks the
derived service over the bus instead of reaching into the integrator itself,
that a reset nobody acknowledged is reported as a failure rather than as
success, and that it cannot happen on a GET. The page reloads itself every
ten seconds, so a mutating GET would fire on its own, repeatedly, with
nobody touching the machine.
"""

import json

import pytest
from fastapi.testclient import TestClient

from doubles import RecordingBus, StubDrift, ui_client
from xams_sc.api import app as app_module
from xams_sc.bus import TOPIC_FLOW_RESET


def flow_reset_bus(acknowledge=True):
    """A RecordingBus that also plays the derived service behind the broker.

    Answering the reset is what lets the round trip be tested without a
    broker. `acknowledge=False` is the service being down, which must read as
    failure and not as a silent success.
    """
    bus = RecordingBus()
    if acknowledge:
        def reply(topic, payload):
            if topic != TOPIC_FLOW_RESET:
                return
            bus.ack("xams/ack/derived/flow_reset",
                    {"ok": True, "start": "2026-09-17T10:00:00Z",
                     "stop": "2026-09-17T17:00:00Z", "total_g": 1987.654,
                     "gaps_s": 0.0, "by": json.loads(payload)["by"]})
        bus.on_publish = reply
    return bus


@pytest.fixture
def bus():
    return flow_reset_bus()


@pytest.fixture
def client(bus, monkeypatch):
    monkeypatch.setattr(app_module, "Bus", lambda **kw: bus)
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    return ui_client(app_module.create_app())


class TestTheResetReachesTheDerivedService:
    def test_it_is_published_not_performed_here(self, client, bus):
        """The UI must not own the integrator's state.

        The integrator runs in another process and keeps the period. A web
        request that reached around it could not be audited and would race
        the service that is still accumulating into it (§2.1).
        """
        client.post("/flow/reset", data={"by": "ap"})

        assert [t for t, _ in bus.published if t == TOPIC_FLOW_RESET], (
            "the reset was not published to the derived service")

    def test_the_name_is_carried_for_the_audit_trail(self, client, bus):
        client.post("/flow/reset", data={"by": "ap"})

        payload = next(json.loads(p) for t, p in bus.published
                       if t == TOPIC_FLOW_RESET)
        assert payload["by"] == "ap"

    def test_an_unnamed_reset_is_recorded_as_unnamed(self, client, bus):
        """Not rejected, but not blank either.

        There is no login on this UI, so a name is taken on trust and can be
        left empty. What must not happen is an empty string going into the
        record, where it reads as though the field did not exist.
        """
        client.post("/flow/reset", data={"by": "   "})

        payload = next(json.loads(p) for t, p in bus.published
                       if t == TOPIC_FLOW_RESET)
        assert payload["by"].strip(), "an empty name reached the audit record"


class TestTheAnswerIsReportedHonestly:
    def test_a_successful_reset_says_so(self, client):
        r = client.post("/flow/reset", data={"by": "ap"},
                        follow_redirects=False)

        assert r.status_code == 303
        assert "reset=ok" in r.headers["location"]
        assert "1987.654" in r.headers["location"]

    def test_an_unacknowledged_reset_is_a_failure_not_a_success(self, monkeypatch):
        """The case that matters. `xams-ctl reload` once reported success
        while doing nothing at all, and it took somebody noticing in the lab.

        If the derived service is down, the period was NOT closed. Saying it
        was would leave somebody believing they had a fresh integration.
        """
        silent = flow_reset_bus(acknowledge=False)
        monkeypatch.setattr(app_module, "Bus", lambda **kw: silent)
        monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
        app = app_module.create_app()
        # The real timeout is 8 s; the point here is the answer, not the wait.
        monkeypatch.setattr(app.state.system, "reset_flow",
                            lambda who, timeout_s=8.0: None)

        r = ui_client(app).post("/flow/reset", data={"by": "ap"},
                                follow_redirects=False)

        assert r.status_code == 303
        assert "reset=failed" in r.headers["location"]


class TestItCannotHappenByAccident:
    def test_a_get_does_not_reset(self, client, bus):
        """The overview reloads itself every 10 s. A mutating GET would fire
        on its own, forever, and close a period every time."""
        r = client.get("/flow/reset")

        assert r.status_code == 405
        assert not [t for t, _ in bus.published if t == TOPIC_FLOW_RESET]

    def test_loading_the_control_page_resets_nothing(self, client, bus):
        client.get("/hv")

        assert not [t for t, _ in bus.published if t == TOPIC_FLOW_RESET]


class TestTheCardShowsTheRate:
    def test_the_rate_is_offered_to_the_template(self, client):
        """The total answers "how much has gone through"; the rate answers
        "is it flowing now". Reading the second off a rising number is
        guesswork."""
        state = client.app.state.system
        flow = state.flow_total()

        assert "rate" in flow and "view" in flow

    def test_the_page_renders_with_no_data_at_all(self, client):
        """Nothing has been published to this fake bus, so every channel is
        absent. The card must still render — a monitoring page that breaks
        when the data stops is broken exactly when it is needed."""
        r = client.get("/hv")

        assert r.status_code == 200
        assert "Integrated flow" in r.text


class TestTheOperatorIsRemembered:
    """§10 rule 5 wants a name on every write. Typing one before each command
    is the friction people work around by leaving it blank, which costs the
    audit trail the one thing it exists for — so it is set once and kept."""

    def test_a_remembered_name_is_used_without_being_typed(self, client, bus):
        client.post("/operator", data={"operator": "ap"})
        client.post("/flow/reset", data={})          # no name on the command

        payload = next(json.loads(p) for t, p in bus.published
                       if t == TOPIC_FLOW_RESET)
        assert payload["by"] == "ap"

    def test_a_name_on_the_request_still_wins(self, client, bus):
        """The CLI and the API send their own, and must be unaffected."""
        client.post("/operator", data={"operator": "ap"})
        client.post("/flow/reset", data={"by": "someone else"})

        payload = next(json.loads(p) for t, p in bus.published
                       if t == TOPIC_FLOW_RESET)
        assert payload["by"] == "someone else"

    def test_with_no_operator_set_it_is_recorded_as_unnamed(self, client, bus):
        client.post("/flow/reset", data={})

        payload = next(json.loads(p) for t, p in bus.published
                       if t == TOPIC_FLOW_RESET)
        assert payload["by"] == "webui (unnamed)"

    def test_clearing_the_name_forgets_it(self, client, bus):
        client.post("/operator", data={"operator": "ap"})
        client.post("/operator", data={"operator": "  "})
        client.post("/flow/reset", data={})

        payload = next(json.loads(p) for t, p in bus.published
                       if t == TOPIC_FLOW_RESET)
        assert payload["by"] == "webui (unnamed)"

    def test_the_lakeshore_card_shows_only_the_used_output(self, client):
        """Output 2 is not used on this cryostat, and a row that always reads
        0 % is noise on a page meant to be scanned in one glance.

        Scoped to the card deliberately: `ls_heater_2` is still read, so its
        description legitimately appears elsewhere on the page when it is not
        reporting. Dropping it from this card is a display choice, not a
        decision to stop monitoring it.
        """
        card = client.get("/hv").text.split("Lake Shore 335")[1].split("</div>")[0]

        assert "output 1" in card
        assert "output 2" not in card


class TestTheOperatorIsSiteWide:
    """It identifies the session, not one instrument. The HV control surface
    will want the same name without asking for it again."""

    @pytest.mark.parametrize("path", ["/", "/status", "/hv", "/logs"])
    def test_every_page_carries_the_operator_control(self, client, path):
        r = client.get(path)

        assert r.status_code == 200
        assert 'action="/operator"' in r.text, (
            "%s cannot set the operator; a control surface added to it later "
            "would have no name to record" % path)

    def test_it_returns_to_the_page_it_was_set_from(self, client):
        r = client.post("/operator", data={"operator": "ap", "next": "/hv"},
                        follow_redirects=False)

        assert r.status_code == 303
        assert r.headers["location"] == "/hv"

    def test_a_name_set_on_one_page_is_used_on_another(self, client, bus):
        client.post("/operator", data={"operator": "ap", "next": "/hv"})
        client.post("/flow/reset", data={})

        payload = next(json.loads(p) for t, p in bus.published
                       if t == TOPIC_FLOW_RESET)
        assert payload["by"] == "ap"


class TestTheReturnPathCannotLeaveTheSite:
    """`next` comes from a form, so it is attacker-controlled input."""

    @pytest.mark.parametrize("target", [
        "//evil.example",            # protocol-relative: NOT a local path
        "https://evil.example",
        "http://evil.example/x",
        "evil.example",
    ])
    def test_an_offsite_target_is_discarded(self, client, target):
        r = client.post("/operator",
                        data={"operator": "ap", "next": target},
                        follow_redirects=False)

        assert r.headers["location"] == "/", (
            "%r was used as a redirect target" % target)

    def test_an_ordinary_path_is_kept(self, client):
        r = client.post("/operator", data={"operator": "ap", "next": "/status"},
                        follow_redirects=False)

        assert r.headers["location"] == "/status"


class TestEveryCommandCanHearItsAnswer:
    """A command whose ack topic is not subscribed times out after ten
    seconds and reports "the service did not answer" - about a write that in
    fact SUCCEEDED. The instrument moved; the page said it had not.

    This happened on 18 September 2026: the HV setpoint route was added, the
    service handled it correctly, and `SystemState` had never been told to
    listen for `xams/ack/caen/vset`. Nothing failed until somebody pressed the
    button, and then it lied in the worst direction.
    """

    def test_every_ack_topic_in_the_bus_is_subscribed(self, client, bus):
        import xams_sc.bus as bus_module

        acks = {name: getattr(bus_module, name) for name in dir(bus_module)
                if name.startswith("ACK_")}
        assert acks, "no ACK_ topics found; has the naming changed?"

        # PROVE THE TEST BITES: drop one and it must fail.
        subscribed = {topic for topic, _ in bus.handlers}
        missing = sorted(n for n, t in acks.items() if t not in subscribed)

        assert not missing, (
            "SystemState does not subscribe to %s, so any command using it "
            "will time out and report failure for a write that worked"
            % ", ".join(missing))
