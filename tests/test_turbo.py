"""HiPace 80 turbo, listened to on RS-485. No hardware required.

Telegrams recorded on the lab PC on 8 October 2026, pump stopped, DCU display
on 340 with the service line on 309.
"""

import time

import pytest

from xams_sc.devices import turbo
from xams_sc.devices.turbo import (PARAMS, PfeifferTap, TurboService,
                                   _ReceiveOnlyPort, decode, parse_telegram)
from xams_sc.model import Quality
from xams_sc.service import BaseService

def with_checksum(head: str) -> str:
    return head + f"{sum(head.encode()) % 256:03d}"


# Recorded on the bus: the DCU's query, then the pump's reply. The 309 pair
# was seen too but not printed verbatim, so it is rebuilt here; the checksum
# function is the protocol's, and the other six are byte-for-byte recordings.
RECORDED = [
    with_checksum("0010030902=?"), with_checksum("0011030906000000"),
    "0010030702=?105", "0011030706000000018",
    "0010030302=?101", "0011030306000000014",
    "0010001002=?096", "0011001006000000009",
]


class FakePort:
    """Receive side only, like _ReceiveOnlyPort. Records any write attempt."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.writes = []
        self.closed = False

    def read_available(self):
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):           # pragma: no cover - must never be called
        self.writes.append(data)

    def close(self):
        self.closed = True


class TestTelegrams:
    @pytest.mark.parametrize("text", RECORDED)
    def test_recorded_telegrams_parse_with_valid_checksum(self, text):
        assert parse_telegram(text) is not None

    def test_query_and_reply_are_told_apart(self):
        q, r = parse_telegram(RECORDED[0]), parse_telegram(RECORDED[1])
        assert (q.address, q.action, q.param, q.data) == (1, "00", 309, "=?")
        assert (r.address, r.action, r.param, r.data) == (1, "10", 309, "000000")

    @pytest.mark.parametrize("text", [
        "0011030706000000019",        # checksum off by one
        "00110307060000000",          # truncated
        "110307060000000018",         # joined mid-telegram
        "0011030707000000018",        # length byte disagrees with the data
        "", "garbage!!!!!!!",
    ])
    def test_a_fragment_is_never_a_telegram(self, text):
        """A sniffer sees fragments; a fragment decoded is a plausible wrong value."""
        assert parse_telegram(text) is None


class TestDecoding:
    def test_booleans(self):
        assert decode(306, "000000") == 0.0
        assert decode(306, "111111") == 1.0

    def test_a_malformed_boolean_is_refused(self):
        with pytest.raises(ValueError):
            decode(306, "000001")

    def test_speed_in_hz(self):
        assert decode(309, "001500") == 1500.0

    def test_current_has_two_implied_decimals(self):
        assert decode(310, "000123") == pytest.approx(1.23)

    @pytest.mark.parametrize("data,value", [
        ("000000", 0.0), ("no Err", 0.0), ("Err006", 6.0), ("Wrn110", 110.0),
    ])
    def test_error_codes(self, data, value):
        assert decode(303, data) == value

    def test_every_configured_channel_is_decodable(self, config):
        """A channel naming an unknown parameter would be decoded by guess."""
        chans = config.channels_for("turbo")
        assert chans, "no turbo channels configured"
        for ch in chans:
            assert int(ch.phys) in PARAMS, ch.name


class TestTheTap:
    def test_replies_are_kept_queries_are_not(self):
        stream = ("\r".join(RECORDED) + "\r").encode()
        tap = PfeifferTap(FakePort([stream]), address=1)
        assert tap.poll(now=10.0) == 4
        assert tap.latest[309] == ("000000", 10.0)
        assert tap.polled == {309, 307, 303, 10}

    def test_a_telegram_split_across_reads_is_reassembled(self):
        a, b = RECORDED[1][:7].encode(), (RECORDED[1][7:] + "\r").encode()
        tap = PfeifferTap(FakePort([a, b]), address=1)
        assert tap.poll(now=1.0) == 0
        assert tap.poll(now=2.0) == 1
        assert tap.latest[309][0] == "000000"

    def test_another_address_is_ignored(self):
        other = with_checksum("0021030906001500")
        tap = PfeifferTap(FakePort([(other + "\r").encode()]), address=1)
        tap.poll(now=1.0)
        assert 309 not in tap.latest

    def test_noise_without_cr_is_eventually_dropped(self):
        tap = PfeifferTap(FakePort([b"x" * 1000]), address=1)
        tap.poll(now=1.0)
        assert tap.latest == {} and tap.rejected == 1


class TestNeverTransmits:
    def test_the_port_wrapper_has_no_write(self):
        """Absent, not merely unused: a later edit cannot send by accident."""
        assert not hasattr(_ReceiveOnlyPort, "write")
        assert not hasattr(_ReceiveOnlyPort, "send")

    def test_nothing_in_the_module_writes(self):
        import inspect
        source = inspect.getsource(turbo)
        code = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
        assert ".write(" not in code

    def test_a_full_read_cycle_writes_nothing(self, config, bus):
        port = FakePort([("\r".join(RECORDED) + "\r").encode()])
        svc = service_with(config, bus, port)
        svc.read()
        assert port.writes == []


def service_with(config, bus, port):
    svc = TurboService(config, bus)
    svc._tap = PfeifferTap(port, address=1)
    return svc


class TestService:
    def test_polled_parameters_are_published(self, config, bus):
        svc = service_with(config, bus, FakePort([("\r".join(RECORDED) + "\r").encode()]))
        by = {m.channel: m for m in svc.read()}
        assert by["turbo_speed"].value == 0.0
        assert by["turbo_speed"].quality is Quality.OK
        assert by["turbo_on"].value == 0.0
        assert by["turbo_error"].value == 0.0

    def test_a_parameter_the_dcu_does_not_poll_is_an_error_not_a_zero(self, config, bus):
        """306 is not in the recording: no value, quality=error."""
        svc = service_with(config, bus, FakePort([("\r".join(RECORDED) + "\r").encode()]))
        by = {m.channel: m for m in svc.read()}
        assert by["turbo_at_speed"].value is None
        assert by["turbo_at_speed"].quality is Quality.ERROR

    def test_a_value_older_than_max_age_is_an_error(self, config, bus):
        svc = service_with(config, bus, FakePort())
        now = time.monotonic()
        svc._tap.latest = {309: ("001500", now - 60), 10: ("111111", now)}
        svc._tap.last_reply = now
        by = {m.channel: m for m in svc.read()}
        assert by["turbo_speed"].quality is Quality.ERROR
        assert by["turbo_on"].value == 1.0

    def test_silence_on_the_bus_raises(self, config, bus):
        """The loop needs the exception to mark every channel bad and back off."""
        svc = service_with(config, bus, FakePort())
        svc._tap.last_reply = time.monotonic() - 60
        with pytest.raises(RuntimeError):
            svc.read()

    def test_simulation_is_tagged(self, config, bus):
        svc = TurboService(config, bus, simulate=True)
        out = svc.read()
        assert out and all(m.src == "sim" for m in out)

    def test_an_unknown_parameter_refuses_to_start(self, config, bus):
        from dataclasses import replace
        svc = TurboService(config, bus, simulate=True)
        svc._channels = [replace(svc._channels[0], phys="999")]
        assert svc.verify_identity() is False

    def test_identity_needs_the_usb_serial(self, config, bus, monkeypatch):
        """An FTDI VID/PID alone identifies nothing."""
        from xams_sc.devices.serial_id import Candidate
        other = Candidate(port="COM9", vid="0403", pid="6001",
                          usb_serial="SOMEONEELSE", description="")
        monkeypatch.setattr(turbo, "candidates", lambda vid, pid: [other])
        assert TurboService(config, bus)._find_port() is None


class TestCodesAreNotAveraged:
    def test_an_error_code_window_publishes_the_last_value(self, config, bus):
        from xams_sc.model import Measurement, utcnow
        svc = BaseService(config, bus)
        t = utcnow()
        svc._accumulate([Measurement(t=t, channel="turbo_error", value=0.0, unit="code")])
        svc._accumulate([Measurement(t=t, channel="turbo_error", value=6.0, unit="code")])
        svc._emit_window()
        assert bus.measurements[-1].value == 6.0


class TestStartup:
    def test_startup_waits_for_every_configured_parameter(self, config, bus, monkeypatch):
        """The first read must not find half the channels missing.

        On the first deployment the service started on the first reply, the
        next read lacked 305 and 306, and the alarm engine raised both as
        major alarms for one log interval. Every restart would do the same.
        """
        wanted = sorted({int(c.phys) for c in config.channels_for("turbo")})
        replies = [with_checksum(f"00110{p:03d}06000000") for p in wanted]
        first, rest = replies[:2], replies[2:]
        port = FakePort([("\r".join(first) + "\r").encode(), b"",
                         ("\r".join(rest) + "\r").encode()])
        monkeypatch.setattr(turbo, "_ReceiveOnlyPort", lambda *a: port)
        monkeypatch.setattr(TurboService, "_find_port", lambda self: "COM_TEST")
        monkeypatch.setattr(turbo.time, "sleep", lambda s: None)
        svc = TurboService(config, bus)
        assert svc.verify_identity() is True
        assert set(wanted) <= set(svc._tap.latest)
        assert all(m.quality is Quality.OK for m in svc.read())
