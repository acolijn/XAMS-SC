"""CAEN link loss and recovery. See DESIGN.md §6.1, §6.2.

Written after a real failure on 17 September 2026: the USB was pulled from ONE
of the two supplies and replugged, and that supply never came back. It sat
publishing errors until the service was restarted by hand.

The cause was that liveness was judged over the whole service:

    if out and all(m.quality is Quality.ERROR for m in out):
        raise RuntimeError("no CAEN channel answered; link lost")

Both supplies share one service, so the healthy one kept the `all()` false,
no exception was raised, and the reconnect path is only reached through an
exception. Nothing appeared in the log either, because a failed command is
logged at debug.

**The link is per device, so liveness has to be too.** That is the invariant
these tests hold down. They use fake readers rather than hardware, so the
half-dead case can be produced on demand — it is precisely the case that is
awkward to arrange in the lab and easy to regress.
"""

import pytest

from xams_sc.config import load
from xams_sc.devices.caen import CaenService
from xams_sc.model import Quality


class RecordingBus:
    def __init__(self):
        self.measurements = []

    def publish_measurement(self, m):
        self.measurements.append(m)

    def publish_state(self, s, st):
        pass

    def publish_heartbeat(self, s):
        pass

    def subscribe(self, t, h):
        pass

    def connect(self):
        pass

    def disconnect(self):
        pass


class FakeReader:
    """Stands in for one supply. `alive` is the USB cable."""

    def __init__(self, device_id, alive=True):
        self.device_id = device_id
        self.alive = alive
        self.port = "COM?"
        self.board_name = "DT1470ET"
        self.board_serial = "fake"
        self.closed = False

    def monitor(self, channel, par):
        # A dead link does not return a wrong number, it returns nothing.
        return None if not self.alive else (1000.0 if par == "VMON" else 0.5)

    def status(self, channel):
        # 1 = ON (§7.2). A dead link returns nothing here too — a status word
        # is a read like any other and must fail the same way.
        return None if not self.alive else 1

    def close(self):
        self.closed = True


@pytest.fixture
def service(tmp_path):
    svc = CaenService(load(), RecordingBus(), simulate=False, lock_dir=tmp_path)
    svc._readers = {"hv_1": FakeReader("hv_1"), "hv_2": FakeReader("hv_2")}
    return svc


def quality_by_device(service, measurements):
    """Group the qualities that came back, by which supply they came from."""
    device = {c.name: c.device for c in service._channels}
    out = {}
    for m in measurements:
        out.setdefault(device[m.channel], set()).add(m.quality)
    return out


class TestOneSupplyDies:
    """The failure that was actually seen: one cable out, one still in."""

    def test_the_dead_supply_is_noticed(self, service):
        service._relink = lambda force=False: True
        service._readers["hv_1"].alive = False

        service.read()
        service.read()

        assert service._link_down["hv_1"] >= 2, (
            "the supply that stopped answering was not counted as down")

    def test_the_healthy_supply_is_not_dragged_down(self, service):
        service._relink = lambda force=False: True
        service._readers["hv_1"].alive = False

        by_device = quality_by_device(service, service.read())

        assert by_device["hv_2"] == {Quality.OK}, (
            "one supply losing its USB must not stop the other being read")
        assert by_device["hv_1"] == {Quality.ERROR}

    def test_a_relink_is_attempted(self, service):
        """The regression itself. This is what did not happen in the lab."""
        attempts = []
        service._relink = lambda force=False: attempts.append(force) or True
        service._readers["hv_1"].alive = False

        service.read()
        assert attempts == [], "one bad cycle is a glitch, not a dead link"
        service.read()
        assert attempts, (
            "a supply that answered nothing twice running must trigger a "
            "relink; without this it publishes errors until someone "
            "restarts the service by hand")

    def test_no_exception_while_one_supply_still_answers(self, service):
        """Raising would be wrong here, and quietly worse than it looks.

        BaseService discards everything read in a cycle that raises, so the
        healthy supply would stop being published for as long as its
        neighbour stayed unplugged — which could be days.
        """
        service._relink = lambda force=False: True
        service._readers["hv_1"].alive = False
        service.read()
        service.read()  # no raise


class TestBothSuppliesDie:
    def test_raises_so_the_service_backs_off(self, service):
        for reader in service._readers.values():
            reader.alive = False
        with pytest.raises(RuntimeError):
            service.read()


class TestRecovery:
    def test_replugging_clears_the_fault(self, service):
        service._relink = lambda force=False: True
        service._readers["hv_1"].alive = False
        service.read()
        service.read()
        assert service._link_down["hv_1"] >= 2

        service._readers["hv_1"].alive = True          # cable back in
        by_device = quality_by_device(service, service.read())

        assert service._link_down["hv_1"] == 0
        assert by_device["hv_1"] == {Quality.OK}

    def test_a_missing_reader_reports_error_rather_than_nothing(self, service):
        """A relink that has not found the supply yet must not go silent.

        Publishing nothing at all would let those channels age into
        staleness, which reads as a monitoring failure rather than as the
        instrument being unplugged. The absence is published (§7.2).
        """
        service._relink = lambda force=False: True
        del service._readers["hv_1"]

        by_device = quality_by_device(service, service.read())

        assert by_device["hv_1"] == {Quality.ERROR}
        assert by_device["hv_2"] == {Quality.OK}


class TestRelinkIsForgiving:
    def test_a_missing_supply_does_not_close_the_healthy_one(self, service, monkeypatch):
        """Startup is strict; relink must not be.

        verify_identity() raises IdentityError when a board is absent, and it
        clears every reader first. Reusing it here would drop the supply that
        is working, which is the opposite of recovery.
        """
        from xams_sc.devices import caen

        def only_hv_2(vid, pid, ask, expected):
            if "hv_1" in expected:
                raise caen.IdentityError("hv_1: no port reported it")
            return {"hv_2": "COM5"}

        monkeypatch.setattr(caen, "resolve", only_hv_2)
        monkeypatch.setattr(caen.CaenChannelReader, "open", lambda self: None)
        monkeypatch.setattr(CaenService, "_check_expectations",
                            lambda self, d, r: None)

        assert service._relink(force=True) is True
        assert "hv_2" in service._readers, (
            "the supply that is present must be relinked even though the "
            "other one is still unplugged")
        assert "hv_1" not in service._readers
