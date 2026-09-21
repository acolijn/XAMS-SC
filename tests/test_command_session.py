"""The shared half of §10: AckInbox and CommandSession.

Both halves of the command path — the web UI, which keeps an inbox for the
life of the process, and `xams-ctl`, which keeps one for a single invocation
— now match acknowledgements the same way, because they do it with the same
object. Before, the UI had one implementation and the CLI had four copies of
another, with timeouts of 8 s and 15 s against the UI's 10 s.

`AckInbox` is covered end-to-end through the UI in test_ack_matching.py and
through the CLI in test_cli_commands.py. What is here is the object on its
own, including the cases neither caller reaches yet.
"""

from __future__ import annotations

import json
import logging

import pytest

from doubles import RecordingBus
from xams_sc import bus as bus_module
from xams_sc.bus import ACK_HV_VSET, TOPIC_HV_VSET, AckInbox, BrokerUnreachable


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def inbox():
    return AckInbox()


def ack(channel="hv_cathode_vset", **kw):
    return json.dumps({"ok": True, "channel": channel, **kw})


class TestAckInbox:
    def test_an_ack_is_claimed_by_the_command_it_answers(self, inbox):
        inbox.deliver(ACK_HV_VSET, ack("hv_cathode_vset", new=-4200.0))
        inbox.deliver(ACK_HV_VSET, ack("hv_anode_vset", new=3000.0))

        mine = inbox.claim(ACK_HV_VSET, {"channel": "hv_anode_vset"})

        assert mine["new"] == 3000.0
        assert [a["channel"] for a in inbox.pending(ACK_HV_VSET)] \
            == ["hv_cathode_vset"], "somebody else's ack was consumed"

    def test_claiming_twice_does_not_return_the_same_ack(self, inbox):
        inbox.deliver(ACK_HV_VSET, ack())

        assert inbox.claim(ACK_HV_VSET, {"channel": "hv_cathode_vset"})
        assert inbox.claim(ACK_HV_VSET, {"channel": "hv_cathode_vset"}) is None

    def test_an_empty_topic_claims_nothing(self, inbox):
        assert inbox.claim(ACK_HV_VSET, {"channel": "hv_cathode_vset"}) is None

    def test_open_discards_only_what_is_stale_for_this_command(self, inbox):
        inbox.deliver(ACK_HV_VSET, ack("hv_cathode_vset"))
        inbox.deliver(ACK_HV_VSET, ack("hv_anode_vset"))

        inbox.open(ACK_HV_VSET, {"channel": "hv_cathode_vset"})

        assert [a["channel"] for a in inbox.pending(ACK_HV_VSET)] \
            == ["hv_anode_vset"]

    def test_open_with_no_discriminator_clears_the_topic(self, inbox):
        """Right for a command with a single target: anything already queued
        is stale, because there is no second command it could belong to."""
        inbox.deliver(ACK_HV_VSET, ack())

        inbox.open(ACK_HV_VSET, {})

        assert inbox.pending(ACK_HV_VSET) == []

    def test_a_malformed_payload_is_warned_about_and_ignored(self, inbox, caplog):
        with caplog.at_level(logging.WARNING):
            inbox.deliver(ACK_HV_VSET, "{not json")

        assert inbox.pending(ACK_HV_VSET) == []
        assert "unparseable acknowledgement" in caplog.text

    def test_uncollected_acks_are_bounded(self, inbox):
        """An ack nobody is waiting for — a command that already timed out —
        must not accumulate."""
        for i in range(200):
            inbox.deliver(ACK_HV_VSET, ack(f"ghost_{i}"))

        assert len(inbox.pending(ACK_HV_VSET)) <= 32

    def test_a_missing_field_does_not_match(self, inbox):
        """An ack that does not carry the discriminator cannot be shown to
        answer anything, so it is not claimed."""
        inbox.deliver(ACK_HV_VSET, json.dumps({"ok": False, "reason": "no"}))

        assert inbox.claim(ACK_HV_VSET, {"channel": "hv_cathode_vset"}) is None


@pytest.fixture
def session(monkeypatch):
    bus = RecordingBus()
    monkeypatch.setattr(bus_module, "Bus", lambda **kw: bus)
    clock = Clock()
    s = bus_module.CommandSession("test", ACK_HV_VSET, sleep=clock.sleep,
                                  monotonic=clock.monotonic)
    return s, bus


class TestCommandSession:
    def test_it_refuses_rather_than_pretending_when_the_broker_is_down(
            self, session):
        s, bus = session
        bus.connected = False

        with pytest.raises(BrokerUnreachable):
            s.__enter__()

        assert bus.published == [], "a command went out with no broker"
        assert bus.disconnected, "the connection was left open"

    def test_it_subscribes_its_ack_topics_before_connecting(self, session):
        s, bus = session

        assert [topic for topic, _ in bus.handlers] == [ACK_HV_VSET]

    def test_a_command_is_answered(self, session):
        s, bus = session
        bus.on_publish = lambda t, p: bus.deliver(
            ACK_HV_VSET, ack(json.loads(p)["channel"], new=-4200.0))

        with s:
            answer = s.send(TOPIC_HV_VSET, ACK_HV_VSET,
                            {"channel": "hv_cathode_vset", "value": -4200.0},
                            match=("channel",))

        assert answer["new"] == -4200.0

    def test_silence_returns_none_rather_than_raising(self, session):
        """None is a FAILURE and callers must read it as one: if the service
        is down the write did not happen."""
        s, bus = session

        with s:
            assert s.send(TOPIC_HV_VSET, ACK_HV_VSET,
                          {"channel": "hv_cathode_vset", "value": -4200.0},
                          match=("channel",), timeout_s=15.0) is None

    def test_an_ack_for_another_channel_does_not_end_the_wait(self, session):
        s, bus = session
        bus.on_publish = lambda t, p: bus.deliver(
            ACK_HV_VSET, ack("hv_anode_vset", new=3000.0))

        with s:
            answer = s.send(TOPIC_HV_VSET, ACK_HV_VSET,
                            {"channel": "hv_cathode_vset", "value": -4200.0},
                            match=("channel",))

        assert answer is None

    def test_the_connection_is_closed_even_when_a_command_raises(self, session):
        s, bus = session

        with pytest.raises(ZeroDivisionError):
            with s:
                1 / 0

        assert bus.disconnected

    def test_every_command_shares_one_timeout(self):
        """8 s in flow-reset and 15 s in the HV path, for no reason anybody
        had decided."""
        assert bus_module.CommandSession.DEFAULT_TIMEOUT_S == 15.0
