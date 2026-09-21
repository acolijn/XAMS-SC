"""The Lake Shore control path. See DESIGN.md §10.

**The first writes in this system.** Everything before this reported what the
hardware was doing; these two commands make it do something. The tests are
therefore mostly about refusal, because the failure modes are asymmetric: a
command wrongly refused is an annoyance, and a command wrongly accepted puts a
number into a cryostat.

Four steps, in order, none optional:

    1. VALIDATE    connected, writable output, value inside channels.yaml
    2. WRITE       one SCPI command
    3. READ BACK   ask the instrument what it now holds
    4. ACK + AUDIT say what happened, and record it

Step 3 carries most of the weight here. `send` reports that bytes left the
port, which is not the instrument having obeyed — and a write that silently
did not take, reported as success, is the worst outcome available.
"""

import json
import threading

import pytest

from doubles import RecordingBus

from xams_sc.bus import (ACK_LS_RANGE, ACK_LS_SETPOINT, TOPIC_AUDIT,
                         TOPIC_LS_RANGE, TOPIC_LS_SETPOINT)
from xams_sc.config import load
from xams_sc.devices.lakeshore import LakeShoreService


class FakeDevice:
    """A Lake Shore that can be told to misbehave.

    `obey=False` accepts the write and keeps its old value, which is the case
    a read-back exists to catch.
    """

    def __init__(self, setpoint=-90.0, heat_range=0, obey=True,
                 send_ok=True, answers=True):
        self.state = {"SETP": setpoint, "RANGE": float(heat_range)}
        self.obey = obey
        self.send_ok = send_ok
        self.answers = answers
        self.sent = []
        # The real driver serialises every conversation with the instrument,
        # and the handlers hold this across write-then-read-back. A double
        # without it passes tests the real object would fail, which is how the
        # race below reached the hardware in the first place.
        self._lock = threading.RLock()

    def send(self, command):
        self.sent.append(command)
        if not self.send_ok:
            return False
        if self.obey:
            verb, args = command.split(" ", 1)
            _, value = args.split(",")
            self.state[verb] = float(value)
        return True

    def number(self, command):
        if not self.answers:
            return None
        return self.state[command.split("?")[0]]


@pytest.fixture
def service(tmp_path):
    svc = LakeShoreService(load(), RecordingBus(), simulate=False,
                           lock_dir=tmp_path)
    svc._device = FakeDevice()
    return svc


def setpoint(service, **kw):
    payload = {"output": 1, "by": "ap"}
    payload.update(kw)
    service._handle_setpoint(TOPIC_LS_SETPOINT, json.dumps(payload))
    return service.bus.last_json(ACK_LS_SETPOINT)


def heater(service, **kw):
    payload = {"output": 1, "by": "ap"}
    payload.update(kw)
    service._handle_range(TOPIC_LS_RANGE, json.dumps(payload))
    return service.bus.last_json(ACK_LS_RANGE)


class TestTheSetpointCanBeChanged:
    def test_a_value_in_range_is_written(self, service):
        ack = setpoint(service, value=-95.0)

        assert ack["ok"] is True
        assert ack["new"] == pytest.approx(-95.0)
        assert "SETP 1,-95.000" in service._device.sent

    def test_the_previous_value_is_reported_and_audited(self, service):
        """Rule 5 wants the old value as well as the new one. "Somebody set
        it to -95" is a different fact from "somebody moved it from -90"."""
        setpoint(service, value=-95.0)

        record = service.bus.last_json(TOPIC_AUDIT)
        assert record["old"] == "-90.0"
        assert record["new"] == "-95.0"
        assert record["actor"] == "ap"
        assert record["result"] == "ok"


class TestTheHeaterRangeCanBeChanged:
    def test_high_turns_it_on(self, service):
        ack = heater(service, range="high")

        assert ack["ok"] is True
        assert ack["new"] == "high"
        assert "RANGE 1,3" in service._device.sent

    def test_off_turns_it_off(self, service):
        service._device.state["RANGE"] = 3.0
        ack = heater(service, range="off")

        assert ack["ok"] is True
        assert ack["old"] == "high"
        assert "RANGE 1,0" in service._device.sent

    def test_an_unknown_range_is_refused(self, service):
        ack = heater(service, range="maximum")

        assert ack["ok"] is False
        assert service._device.sent == []


class TestRefusals:
    """A refused command is acknowledged with a reason, never dropped."""

    def test_a_setpoint_above_the_limit_is_refused(self, service):
        """channels.yaml permits -196..0 C. The sign is the realistic typo:
        25 instead of -25 would warm the cryostat."""
        ack = setpoint(service, value=25.0)

        assert ack["ok"] is False
        assert "outside the permitted range" in ack["reason"]
        assert service._device.sent == [], "nothing must reach the instrument"

    def test_a_setpoint_below_the_limit_is_refused(self, service):
        ack = setpoint(service, value=-300.0)

        assert ack["ok"] is False
        assert service._device.sent == []

    def test_output_2_is_refused(self, service):
        """Output 2 is not used on this cryostat, so naming it is a mistake
        rather than an instruction."""
        ack = setpoint(service, output=2, value=-95.0)

        assert ack["ok"] is False
        assert "not writable" in ack["reason"]
        assert service._device.sent == []

    def test_a_disconnected_instrument_is_refused(self, service):
        service._device = None
        ack = setpoint(service, value=-95.0)

        assert ack["ok"] is False
        assert "not connected" in ack["reason"]

    def test_a_simulating_service_refuses_to_write(self, service):
        """Simulation must never look like a successful write. It holds no
        instrument, and saying "ok" would be a lie with a plausible shape."""
        service.simulate = True
        ack = setpoint(service, value=-95.0)

        assert ack["ok"] is False
        assert "simulating" in ack["reason"]

    def test_a_missing_value_is_refused(self, service):
        service._handle_setpoint(TOPIC_LS_SETPOINT, json.dumps({"by": "ap"}))

        assert service.bus.last_json(ACK_LS_SETPOINT)["ok"] is False

    def test_unparseable_json_is_refused_without_raising(self, service):
        service._handle_setpoint(TOPIC_LS_SETPOINT, "{not json")

        assert service.bus.last_json(ACK_LS_SETPOINT)["ok"] is False

    def test_a_refusal_is_audited_too(self, service):
        """What somebody tried to do is as interesting afterwards as what
        they managed to do."""
        setpoint(service, value=25.0)

        record = service.bus.last_json(TOPIC_AUDIT)
        assert record["result"] == "rejected"
        assert record["actor"] == "ap"
        assert "outside the permitted range" in record["detail"]


class TestTheReadBack:
    """The step that is easy to skip and must not be."""

    def test_a_write_that_did_not_take_is_reported_as_failure(self, service):
        """The instrument accepted the bytes and kept its old value. Saying
        "ok" here would leave somebody believing a setpoint had moved."""
        service._device = FakeDevice(obey=False)
        ack = setpoint(service, value=-95.0)

        assert ack["ok"] is False
        assert "did not take" in ack["reason"]

    def test_a_range_that_did_not_take_is_reported_as_failure(self, service):
        service._device = FakeDevice(obey=False)
        ack = heater(service, range="high")

        assert ack["ok"] is False
        assert "did not take" in ack["reason"]

    def test_an_instrument_that_stops_answering_is_a_failure(self, service):
        service._device = FakeDevice(answers=False)
        ack = setpoint(service, value=-95.0)

        assert ack["ok"] is False

    def test_a_send_that_fails_is_reported(self, service):
        service._device = FakeDevice(send_ok=False)
        ack = setpoint(service, value=-95.0)

        assert ack["ok"] is False
        assert "could not be sent" in ack["reason"]

    def test_a_failed_write_is_audited_as_failed(self, service):
        service._device = FakeDevice(obey=False)
        setpoint(service, value=-95.0)

        assert service.bus.last_json(TOPIC_AUDIT)["result"] == "rejected"


class TestAttribution:
    def test_an_unnamed_command_is_recorded_as_unknown(self, service):
        """Not blank. A blank actor reads as though the field did not exist."""
        service._handle_setpoint(TOPIC_LS_SETPOINT,
                                 json.dumps({"output": 1, "value": -95.0}))

        assert service.bus.last_json(TOPIC_AUDIT)["actor"] == "unknown"

    def test_every_command_produces_exactly_one_audit_record(self, service):
        setpoint(service, value=-95.0)
        setpoint(service, value=25.0)        # refused
        heater(service, range="high")

        assert len(service.bus.json_on(TOPIC_AUDIT)) == 3
