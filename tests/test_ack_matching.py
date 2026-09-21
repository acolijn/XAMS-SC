"""Which acknowledgement belongs to which command. See DESIGN.md §10.

Every waiter on `xams/ack/caen/vset` used to take whichever ack arrived
first. Two operators setting two different channels at the same moment were
shown each other's result — and since the ack carries `old` and `new`, one of
them read a confident `-4200 V` for a channel they had never touched while the
other got a spurious timeout.

The window is short, because the CAEN serial lock serialises the writes, and
it needs two people at once. It is not narrow enough to leave in a path that
puts volts on an electrode, and the fix costs nothing on the wire: the
discriminator (`channel`, or `output` for the Lake Shore) was already in both
payloads, so no service had to change.

`match=()` — any ack will do — is kept deliberately for commands with a single
target, such as the master notification switch, and is pinned here too.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from doubles import RecordingBus
from xams_sc.api.state import SystemState
from xams_sc.bus import (ACK_HV_VSET, ACK_NOTIFY, TOPIC_HV_VSET,
                        TOPIC_NOTIFY, AckInbox)


@pytest.fixture
def state():
    """A SystemState with only what `command` touches.

    Built without __init__ on purpose: the real one connects a bus, starts
    threads and reads the configuration, none of which this is about.
    """
    s = SystemState.__new__(SystemState)
    s.bus = RecordingBus()
    s._lock = threading.Lock()
    s._acks = AckInbox()
    return s


def vset_ack(channel, old, new, ok=True):
    return json.dumps({"ok": ok, "channel": channel, "old": old, "new": new})


def in_background(state, channel, value, by, timeout_s=3.0):
    """Start a command and return a handle to its result."""
    out = {}

    def run():
        out["answer"] = state.command(
            TOPIC_HV_VSET, ACK_HV_VSET,
            {"channel": channel, "value": value, "by": by},
            timeout_s=timeout_s, match=("channel",))

    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


class TestTwoOperatorsAtOnce:
    def test_each_is_shown_their_own_result(self, state):
        alice, alice_out = in_background(state, "hv_cathode_vset", -4200.0, "alice")
        bob, bob_out = in_background(state, "hv_anode_vset", 3000.0, "bob")
        time.sleep(0.3)         # both waiting

        # The CAEN service answers both, in the order it processed them.
        state._on_ack(ACK_HV_VSET, vset_ack("hv_cathode_vset", -4000.0, -4200.0))
        state._on_ack(ACK_HV_VSET, vset_ack("hv_anode_vset", 2800.0, 3000.0))
        alice.join(); bob.join()

        assert alice_out["answer"]["channel"] == "hv_cathode_vset"
        assert alice_out["answer"]["new"] == -4200.0
        assert bob_out["answer"]["channel"] == "hv_anode_vset"
        assert bob_out["answer"]["new"] == 3000.0

    def test_neither_times_out_because_of_the_other(self, state):
        """The second operator's ack must survive the first one's arrival.

        Clearing the whole queue on the way past is what used to take it.
        """
        alice, alice_out = in_background(state, "hv_cathode_vset", -4200.0, "alice")
        bob, bob_out = in_background(state, "hv_anode_vset", 3000.0, "bob")
        time.sleep(0.3)

        state._on_ack(ACK_HV_VSET, vset_ack("hv_anode_vset", 2800.0, 3000.0))
        state._on_ack(ACK_HV_VSET, vset_ack("hv_cathode_vset", -4000.0, -4200.0))
        alice.join(); bob.join()

        assert alice_out["answer"].get("ok") is True
        assert bob_out["answer"].get("ok") is True

    def test_an_ack_that_arrives_while_waiting_is_collected(self, state):
        """The ordinary case: publish, then the service answers."""
        alice, out = in_background(state, "hv_cathode_vset", -4200.0, "alice")
        time.sleep(0.2)
        state._on_ack(ACK_HV_VSET, vset_ack("hv_cathode_vset", -4000.0, -4200.0))
        alice.join()

        assert out["answer"]["channel"] == "hv_cathode_vset"
        assert out["answer"]["new"] == -4200.0


class TestStaleAcks:
    def test_our_own_stale_ack_is_never_taken_for_an_answer(self, state):
        """An ack that was already in the queue cannot be an answer to a
        command that had not been sent yet.

        It is left over from one of ours that already timed out, and reporting
        it would tell somebody a setpoint had moved when this attempt has not
        been answered at all. A timeout is the honest outcome, and §10 is
        explicit that a timeout reads as failure.
        """
        state._on_ack(ACK_HV_VSET, vset_ack("hv_cathode_vset", -1.0, -2.0))

        answer = state.command(
            TOPIC_HV_VSET, ACK_HV_VSET,
            {"channel": "hv_cathode_vset", "value": -4200.0, "by": "alice"},
            timeout_s=0.3, match=("channel",))

        assert answer["ok"] is False, "a stale ack was returned as the answer"

    def test_somebody_elses_pending_ack_is_left_alone(self, state):
        state._on_ack(ACK_HV_VSET, vset_ack("hv_anode_vset", 2800.0, 3000.0))

        state.command(TOPIC_HV_VSET, ACK_HV_VSET,
                      {"channel": "hv_cathode_vset", "value": -4200.0,
                       "by": "alice"},
                      timeout_s=0.3, match=("channel",))

        assert [a["channel"] for a in state._acks.pending(ACK_HV_VSET)] \
            == ["hv_anode_vset"]

    def test_uncollected_acks_do_not_accumulate_without_bound(self, state):
        """A command that timed out leaves an ack nobody will ever claim."""
        for i in range(200):
            state._on_ack(ACK_HV_VSET, vset_ack(f"ghost_{i}", 0.0, 0.0))

        assert len(state._acks.pending(ACK_HV_VSET)) <= 32


class TestWithoutADiscriminator:
    def test_any_ack_will_do(self, state):
        """The master notification switch has one target, so there is nothing
        to tell apart and `match=()` is correct."""
        out = {}

        def run():
            out["answer"] = state.command(TOPIC_NOTIFY, ACK_NOTIFY,
                                          {"enabled": False, "by": "alice"},
                                          timeout_s=3.0)

        waiter = threading.Thread(target=run)
        waiter.start()
        time.sleep(0.2)
        state._on_ack(ACK_NOTIFY, json.dumps({"ok": True, "enabled": False}))
        waiter.join()

        assert out["answer"]["enabled"] is False

    def test_the_queue_is_cleared_first_so_a_stale_ack_is_not_returned(self, state):
        state._on_ack(ACK_NOTIFY, json.dumps({"ok": True, "enabled": True}))
        state._on_ack(ACK_NOTIFY, json.dumps({"ok": True, "enabled": False}))

        answer = state.command(TOPIC_NOTIFY, ACK_NOTIFY,
                               {"enabled": True, "by": "alice"},
                               timeout_s=0.3)

        assert answer.get("ok") is False, "a stale ack was taken for an answer"
        assert "did not answer" in answer["reason"]


class TestTimeout:
    def test_silence_is_reported_as_failure_not_success(self, state):
        answer = state.command(
            TOPIC_HV_VSET, ACK_HV_VSET,
            {"channel": "hv_cathode_vset", "value": -4200.0, "by": "alice"},
            timeout_s=0.3, match=("channel",))

        assert answer["ok"] is False
        assert "nothing was changed" in answer["reason"]
        assert "caen" in answer["reason"]

    def test_an_ack_for_another_channel_does_not_end_the_wait(self, state):
        state._on_ack(ACK_HV_VSET, vset_ack("hv_anode_vset", 2800.0, 3000.0))

        answer = state.command(
            TOPIC_HV_VSET, ACK_HV_VSET,
            {"channel": "hv_cathode_vset", "value": -4200.0, "by": "alice"},
            timeout_s=0.3, match=("channel",))

        assert answer["ok"] is False

    def test_a_malformed_ack_is_ignored_rather_than_raising(self, state):
        state._on_ack(ACK_HV_VSET, "{not json")

        answer = state.command(
            TOPIC_HV_VSET, ACK_HV_VSET,
            {"channel": "hv_cathode_vset", "value": -4200.0, "by": "alice"},
            timeout_s=0.3, match=("channel",))

        assert answer["ok"] is False
