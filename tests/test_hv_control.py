"""The CAEN setpoint write path. See DESIGN.md §10a.

**This is the code that puts kilovolts on electrodes.** The tests are
overwhelmingly about refusal, because the failure modes are not symmetric: a
command wrongly refused is an annoyance, and a command wrongly accepted is
4.2 kV on an anode nobody was standing next to.

The invariant the whole design rests on:

> A channel that is not enabled has `VSET` = 0.

The enable is a hand operation and the board ramps to `VSET` the instant it is
flipped. If the invariant holds, flipping the switch is always safe. If it does
not, flipping it is a step into an unannounced voltage.
"""

import json

import pytest

from xams_sc.bus import ACK_HV_VSET, TOPIC_AUDIT, TOPIC_HV_VSET
from xams_sc.config import load
from xams_sc.devices.caen import CaenService
from xams_sc.hv_status import BIT_DISABLED, BIT_ON

ENABLED = 1 << BIT_ON
DISABLED = 1 << BIT_DISABLED


class RecordingBus:
    def __init__(self):
        self.published = []

    def publish_raw(self, topic, payload, retain=False):
        self.published.append((topic, json.loads(payload)))

    def subscribe(self, topic, handler): pass
    def connect(self): pass
    def disconnect(self): pass
    def publish_state(self, s, st): pass
    def publish_heartbeat(self, s): pass
    def publish_measurement(self, m): pass

    def last(self, topic):
        for t, p in reversed(self.published):
            if t == topic:
                return p
        return None


class FakeReader:
    """One supply. `vset` is the unsigned magnitude, as on the wire."""

    def __init__(self, stat=ENABLED, vset=0.0, obey=True, accepts=True,
                 answers=True, refusal="the supply refused it"):
        import threading
        self.stat = stat
        self.vset = vset
        self.obey = obey            # False: acknowledges, keeps the old value
        self.accepts = accepts      # False: the board refuses the command
        self.answers = answers      # False: reads come back as None
        self.refusal = refusal      # why the board refused, as it reports it
        self.written = []
        self._lock = threading.RLock()

    def status(self, channel):
        return self.stat if self.answers else None

    def monitor(self, channel, par):
        if not self.answers:
            return None
        return self.vset if par == "VSET" else 0.0

    def set_voltage(self, channel, magnitude):
        # (ok, reason) like the real reader: the board says WHY it refused,
        # and the reasons are not interchangeable - LOCAL mode needs a person
        # at the front panel, a rejected value needs a different number.
        self.written.append(magnitude)
        if not self.accepts:
            return False, self.refusal
        if self.obey:
            self.vset = magnitude
        return True, ""

    def control_mode(self):
        return "REMOTE" if self.accepts else "LOCAL"


@pytest.fixture
def service(tmp_path):
    svc = CaenService(load(), RecordingBus(), simulate=False, lock_dir=tmp_path)
    svc._readers = {"hv_1": FakeReader(), "hv_2": FakeReader()}
    return svc


def send(service, channel="hv_cathode_vset", value=-1000.0, by="ap"):
    service._handle_vset(TOPIC_HV_VSET, json.dumps(
        {"channel": channel, "value": value, "by": by}))
    return service.bus.last(ACK_HV_VSET)


class TestTheInvariant:
    """Section 10a: a channel that is not enabled keeps VSET at zero."""

    def test_a_non_zero_setpoint_on_a_disabled_channel_is_refused(self, service):
        """The heart of it. Allowing this would let somebody stage 4.2 kV,
        walk to the supply, flip the switch, and get the unannounced ramp the
        whole design exists to prevent."""
        service._readers["hv_2"].stat = DISABLED

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "disabled at the supply" in ack["reason"]
        assert service._readers["hv_2"].written == [], (
            "a setpoint reached a disabled channel")

    def test_a_switched_on_but_UNENERGISED_channel_accepts_a_setpoint(self, service):
        """The rule is about the SWITCH (bit 10), not the output (bit 0).

        This is the state an operator is in between flipping the enable and
        pressing turn-on, and it is exactly when they want to load a setpoint:
        load defaults, read them, adjust, apply, and only then energise.

        The first version of the rule read bit 0 and refused here - which made
        the intended sequence impossible, while looking like a safety feature.
        """
        service._readers["hv_2"].stat = 0        # neither bit: switch on, off

        ack = send(service, value=-1000.0)

        assert ack["ok"] is True
        assert service._readers["hv_2"].written == [1000.0]

    def test_zero_IS_allowed_on_a_disabled_channel(self, service):
        """This is how the invariant gets established on a channel whose
        stored setpoint is currently wrong - which is true of seven of the
        eight channels today."""
        service._readers["hv_2"].stat = DISABLED
        service._readers["hv_2"].vset = 2250.0

        ack = send(service, value=0.0)

        assert ack["ok"] is True
        assert service._readers["hv_2"].written == [0.0]

    def test_an_enabled_channel_accepts_a_voltage(self, service):
        service._readers["hv_2"].stat = ENABLED

        ack = send(service, value=-1000.0)

        assert ack["ok"] is True
        assert ack["new"] == pytest.approx(-1000.0)

    def test_an_unreadable_status_word_refuses_rather_than_assumes(self, service):
        """Not knowing whether a channel is enabled is not permission to
        assume it is."""
        service._readers["hv_2"].answers = False

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "unknown" in ack["reason"]
        assert service._readers["hv_2"].written == []


class TestPolarity:
    """The supplies take an unsigned magnitude and apply their own POL."""

    def test_a_negative_channel_is_written_as_a_magnitude(self, service):
        send(service, channel="hv_cathode_vset", value=-2250.0)

        assert service._readers["hv_2"].written == [2250.0]

    def test_a_positive_value_on_a_negative_channel_is_refused(self, service):
        """`abs()` in the conversion would ask for +2250 and deliver -2250:
        the board applies its own POL, so the sign is discarded, the read-back
        agrees and the audit looks clean.

        Today the RANGE check refuses this first, because every HV channel's
        limits are one-sided (the cathode permits -2500..0). The reason
        reported is therefore the range, not the polarity - which is fine, and
        is asserted here as it actually behaves rather than as one might
        expect. The polarity check is the backstop for a channel whose limits
        are ever made symmetric; `test_scaling.py` covers it directly.
        """
        ack = send(service, channel="hv_cathode_vset", value=2250.0)

        assert ack["ok"] is False
        assert "outside the permitted range" in ack["reason"]
        assert service._readers["hv_2"].written == [], (
            "a positive setpoint reached a negative electrode")

    def test_a_negative_value_on_the_anode_is_refused(self, service):
        ack = send(service, channel="hv_anode_vset", value=-1000.0)

        assert ack["ok"] is False
        assert service._readers["hv_2"].written == []

    def test_the_polarity_backstop_refuses_independently_of_the_range(self):
        """With symmetric limits the range check would let a sign error
        through, and `magnitude_for` is what stops it."""
        from xams_sc.scaling import magnitude_for

        with pytest.raises(ValueError, match="polarity"):
            magnitude_for(2250.0, -1)
        with pytest.raises(ValueError, match="polarity"):
            magnitude_for(-4200.0, 1)


class TestTheSoftwareRange:
    def test_a_value_beyond_the_configured_limit_is_refused(self, service):
        """channels.yaml permits the cathode -2500..0."""
        ack = send(service, channel="hv_cathode_vset", value=-3000.0)

        assert ack["ok"] is False
        assert "outside the permitted range" in ack["reason"]
        assert service._readers["hv_2"].written == []


class TestTheReadBack:
    """`set_voltage` reports that the board acknowledged, not that it obeyed."""

    def test_a_write_that_did_not_take_is_reported_as_failure(self, service):
        service._readers["hv_2"] = FakeReader(stat=ENABLED, obey=False)

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "did not take" in ack["reason"]

    def test_a_board_refusal_is_reported(self, service):
        """A value above the board's own MAXV is refused by the instrument -
        the protection working as intended (§10 rule 2)."""
        service._readers["hv_2"] = FakeReader(
            stat=ENABLED, accepts=False,
            refusal="the supply rejected the value, most likely above MAXV")

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "MAXV" in ack["reason"]

    def test_a_board_in_local_mode_says_so(self, service):
        """The real finding, 18 September 2026: both boards were in LOCAL, so
        every SET was refused with LOC:ERR while every MON kept working.

        The message must name the cause and the remedy. "the supply did not
        accept the command" sent somebody to read a log file; "it is in LOCAL,
        switch the front panel to REMOTE" does not.
        """
        service._readers["hv_2"] = FakeReader(
            stat=ENABLED, accepts=False,
            refusal=("the supply is in LOCAL mode, so it refuses every remote "
                     "setpoint. Put the board in REMOTE at its front panel"))

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "LOCAL" in ack["reason"] and "REMOTE" in ack["reason"]


class TestRefusalsAreRecorded:
    def test_a_rejected_command_is_audited(self, service):
        service._readers["hv_2"].stat = DISABLED
        send(service, value=-1000.0)

        record = service.bus.last(TOPIC_AUDIT)
        assert record["result"] == "rejected"
        assert record["action"] == "caen_vset"
        assert record["actor"] == "ap"
        assert record["new"] == "-1000.0"

    def test_a_successful_write_records_both_values(self, service):
        service._readers["hv_2"].vset = 500.0
        send(service, value=-1000.0)

        record = service.bus.last(TOPIC_AUDIT)
        assert record["result"] == "ok"
        assert record["old"] == "-500.0"
        assert record["new"] == "-1000.0"

    def test_an_unnamed_command_is_recorded_as_unknown(self, service):
        service._handle_vset(TOPIC_HV_VSET, json.dumps(
            {"channel": "hv_cathode_vset", "value": 0.0}))

        assert service.bus.last(TOPIC_AUDIT)["actor"] == "unknown"


class TestBadInput:
    @pytest.mark.parametrize("payload", [
        "{not json",
        '{"value": -100}',                        # no channel
        '{"channel": "hv_cathode_vset"}',         # no value
        '{"channel": "tt401", "value": -100}',    # not an HV setpoint
        '{"channel": "hv_cathode_vmon", "value": -100}',   # a monitor, not a setpoint
    ])
    def test_it_is_refused_without_raising(self, service, payload):
        service._handle_vset(TOPIC_HV_VSET, payload)

        assert service.bus.last(ACK_HV_VSET)["ok"] is False
        assert service._readers["hv_2"].written == []

    def test_a_simulating_service_refuses_to_write(self, service):
        """Simulation must never look like a successful write to hardware."""
        service.simulate = True

        ack = send(service, value=-1000.0)

        assert ack["ok"] is False
        assert "simulating" in ack["reason"]


class TestWhatTheDriverCannotDo:
    """Section 10a: these are absences, and each is deliberate."""

    def test_no_protection_setting_is_ever_written(self):
        """§10 rule 2: ramp rate, trip current and the over-voltage limit stay
        configured on the instrument. That is what keeps this software out of
        the protection path, and it is the reason a bug here cannot raise a
        limit to make room for a mistake."""
        source = (__import__("pathlib").Path(
            __import__("xams_sc.devices.caen", fromlist=["x"]).__file__)
            .read_text(encoding="utf-8"))

        for line in source.splitlines():
            if "CMD:SET" not in line:
                continue
            for forbidden in ("MAXV", "RUP", "RDW", "TRIP", "ISET", "BDCTR"):
                assert forbidden not in line, (
                    "a protection setting is written: %s" % line.strip())
