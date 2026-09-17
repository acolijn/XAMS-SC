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

from xams_sc.api import app as app_module
from xams_sc.bus import TOPIC_FLOW_RESET
from xams_sc.model import ServiceState


class FakeBus:
    """Stands in for the broker, and for the derived service behind it.

    When `acknowledge` is set it answers a reset the way DerivedService does,
    which is what lets the round trip be tested without a broker.
    """

    def __init__(self, acknowledge=True):
        self.acknowledge = acknowledge
        self.published = []
        self.handlers = []

    def subscribe(self, topic, handler):
        self.handlers.append((topic, handler))

    def connect(self):
        pass

    def disconnect(self):
        pass

    def publish_state(self, service, state):
        pass

    def publish_heartbeat(self, service):
        pass

    def publish_measurement(self, m):
        pass

    def publish_raw(self, topic, payload, retain=False):
        self.published.append((topic, payload))
        if topic == TOPIC_FLOW_RESET and self.acknowledge:
            reply = json.dumps({"ok": True, "start": "2026-09-17T10:00:00Z",
                                "stop": "2026-09-17T17:00:00Z",
                                "total_g": 1987.654, "gaps_s": 0.0,
                                "by": json.loads(payload)["by"]})
            for topic_pattern, handler in self.handlers:
                if topic_pattern == "xams/ack/derived/flow_reset":
                    handler(topic_pattern, reply)


@pytest.fixture
def bus():
    return FakeBus()


class StubDrift:
    """The dashboard-drift check, stubbed out.

    Without this the web UI tests reach across to Grafana over HTTP, which
    makes them fail on any machine where Grafana is merely stopped — and the
    thing they are testing has nothing to do with dashboards.
    """

    def get(self):
        return {"state": "ok", "dashboards": [], "detail": "stubbed"}


@pytest.fixture
def client(bus, monkeypatch):
    monkeypatch.setattr(app_module, "Bus", lambda **kw: bus)
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    return TestClient(app_module.create_app())


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
        silent = FakeBus(acknowledge=False)
        monkeypatch.setattr(app_module, "Bus", lambda **kw: silent)
        monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
        app = app_module.create_app()
        # The real timeout is 8 s; the point here is the answer, not the wait.
        monkeypatch.setattr(app.state.system, "reset_flow",
                            lambda who, timeout_s=8.0: None)

        r = TestClient(app).post("/flow/reset", data={"by": "ap"},
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

    def test_loading_the_overview_resets_nothing(self, client, bus):
        client.get("/")

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
        r = client.get("/")

        assert r.status_code == 200
        assert "Integrated flow" in r.text
