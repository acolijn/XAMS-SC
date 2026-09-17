"""End-to-end pipeline test, without a broker or any hardware.

This is the milestone 1 acceptance criterion reduced to something that runs in
CI: a service produces measurements, they cross a bus, a writer stores them,
and what lands on disk can be read back.

The bus is replaced by a recording stand-in. That is deliberate — this test is
about the chain from acquisition to archive, and it must not fail because a
broker happens to be down. The real broker path is exercised by running the
stack (see README, "Run it").
"""

import json
from pathlib import Path

import pytest

from xams_sc.config import load
from xams_sc.devices.sim import SimService
from xams_sc.model import Measurement, Quality, utcnow
from xams_sc.sinks.jsonl_writer import JsonlWriter


class RecordingBus:
    """Stands in for Bus: records instead of publishing."""

    def __init__(self):
        self.measurements: list[Measurement] = []
        self.states: list = []
        self.heartbeats: list[str] = []
        # A list, mirroring the real Bus. This stand-in originally used a dict
        # keyed by topic — the same flaw the real Bus had — so the suite was
        # structurally incapable of catching the fanout bug. A double that
        # reproduces the defect under test proves nothing.
        self.handlers: list = []

    def publish_measurement(self, m):
        self.measurements.append(m)

    def publish_state(self, service, state):
        self.states.append((service, state))

    def publish_heartbeat(self, service):
        self.heartbeats.append(service)

    def subscribe(self, topic, handler):
        self.handlers.append((topic, handler))

    def connect(self):
        pass

    def disconnect(self):
        pass


@pytest.fixture
def config():
    return load()


class TestSimService:
    def test_produces_a_value_for_every_enabled_hardware_channel(self, config, tmp_path):
        bus = RecordingBus()
        svc = SimService(config, bus, lock_dir=tmp_path)
        batch = svc.read()

        expected = {c.name for c in config.enabled_channels() if c.device != "derived"}
        assert {m.channel for m in batch} == expected
        assert all(m.quality == Quality.OK for m in batch)
        assert all(m.value is not None for m in batch)

    def test_timestamps_are_timezone_aware_utc(self, config, tmp_path):
        svc = SimService(config, RecordingBus(), lock_dir=tmp_path)
        for m in svc.read():
            assert m.t.tzinfo is not None
            assert m.t.utcoffset().total_seconds() == 0

    def test_hv_channels_carry_the_configured_sign(self, config, tmp_path):
        """A negative electrode must simulate negative, or the sign convention
        is untested until the first time real HV is read."""
        svc = SimService(config, RecordingBus(), lock_dir=tmp_path)
        by_name = {m.channel: m.value for m in svc.read()}

        assert by_name["hv_cathode_vmon"] < 0
        assert by_name["hv_gate_vmon"] < 0
        assert by_name["hv_pmt_top_vmon"] < 0
        assert by_name["hv_anode_vmon"] > 0
        assert by_name["hv_nai_vmon"] > 0

    def test_simulated_hv_stays_within_the_software_limits(self, config, tmp_path):
        svc = SimService(config, RecordingBus(), lock_dir=tmp_path)
        by_name = {m.channel: m.value for m in svc.read()}
        for ch in config.enabled_channels():
            if ch.kind == "hv_vmon" and ch.limits:
                assert ch.in_limits(by_name[ch.name]), ch.name


class TestAveraging:
    def test_window_publishes_the_mean_not_the_last_sample(self, config, tmp_path):
        """Read at 1 Hz, log the mean of those samples (§9.1). The averaging is
        not merely data reduction: the stored value is less noisy than any
        single reading, so it is better than what is discarded."""
        bus = RecordingBus()
        svc = SimService(config, bus, lock_dir=tmp_path)

        now = utcnow()
        svc._accumulate([Measurement(t=now, channel="pmain", value=1.0, unit="bar")])
        svc._accumulate([Measurement(t=now, channel="pmain", value=2.0, unit="bar")])
        svc._accumulate([Measurement(t=now, channel="pmain", value=3.0, unit="bar")])
        svc._emit_window()

        published = [m for m in bus.measurements if m.channel == "pmain"]
        assert len(published) == 1
        assert published[0].value == pytest.approx(2.0)

    def test_a_bad_sample_downgrades_the_aggregate(self, config, tmp_path):
        """Quality is not averaged. A mean that quietly includes an error
        reading is the frozen-plausible-value failure this design refuses."""
        bus = RecordingBus()
        svc = SimService(config, bus, lock_dir=tmp_path)

        now = utcnow()
        svc._accumulate([Measurement(t=now, channel="pmain", value=1.0, unit="bar")])
        svc._accumulate([Measurement(t=now, channel="pmain", value=None, unit="bar",
                                     quality=Quality.ERROR)])
        svc._emit_window()

        published = [m for m in bus.measurements if m.channel == "pmain"][0]
        assert published.quality is not Quality.OK

    def test_all_samples_bad_publishes_no_value(self, config, tmp_path):
        bus = RecordingBus()
        svc = SimService(config, bus, lock_dir=tmp_path)
        now = utcnow()
        svc._accumulate([Measurement(t=now, channel="pmain", value=None, unit="bar",
                                     quality=Quality.ERROR)])
        svc._emit_window()

        published = [m for m in bus.measurements if m.channel == "pmain"][0]
        assert published.value is None
        assert published.quality is Quality.ERROR


class TestJsonlArchive:
    def test_writes_a_header_then_records(self, config, tmp_path):
        writer = JsonlWriter(RecordingBus(), config, directory=tmp_path)
        writer.write(Measurement(t=utcnow(), channel="pmain", value=1.53,
                                 unit="bar", raw=2.143))
        writer.close()

        files = list(Path(tmp_path).glob("*.jsonl"))
        assert len(files) == 1

        lines = files[0].read_text(encoding="utf-8").strip().split("\n")
        header = json.loads(lines[0])
        assert header["meta"]["config"] == config.config_hash
        assert "host" in header["meta"]

        record = json.loads(lines[1])
        assert record["ch"] == "pmain"
        assert record["v"] == 1.53
        assert record["raw"] == 2.143      # raw is kept for rescaling (§9.4)
        assert record["q"] == "ok"

    def test_round_trips_through_the_wire_format(self, config, tmp_path):
        original = Measurement(t=utcnow(), channel="hv_cathode_vmon",
                               value=-2250.0, unit="V", raw=2250.0)
        writer = JsonlWriter(RecordingBus(), config, directory=tmp_path)
        writer.write(original)
        writer.close()

        line = list(Path(tmp_path).glob("*.jsonl"))[0].read_text(
            encoding="utf-8").strip().split("\n")[1]
        restored = Measurement.from_payload(json.loads(line))

        assert restored.channel == original.channel
        assert restored.value == original.value    # sign survives, -2250.0
        assert restored.unit == original.unit
        assert restored.raw == original.raw

        # The wire format is millisecond precision (§5), so a round trip
        # truncates below that. Stated here so the loss is a known property
        # of the archive rather than a surprise during an incident analysis.
        assert abs((restored.t - original.t).total_seconds()) < 0.001
        assert restored.t.microsecond % 1000 == 0

    def test_full_chain_sim_to_disk(self, config, tmp_path):
        """The milestone 1 criterion: a service publishes, a writer stores."""
        bus = RecordingBus()
        svc = SimService(config, bus, lock_dir=tmp_path)
        writer = JsonlWriter(bus, config, directory=tmp_path)

        for _ in range(3):
            svc._accumulate(svc.read())
        svc._emit_window()

        for m in bus.measurements:
            writer.write(m)
        writer.close()

        lines = list(Path(tmp_path).glob("*.jsonl"))[0].read_text(
            encoding="utf-8").strip().split("\n")
        records = [json.loads(line) for line in lines[1:]]

        expected = {c.name for c in config.enabled_channels() if c.device != "derived"}
        assert {r["ch"] for r in records} == expected
        assert all(r["q"] == "ok" for r in records)


class TestBusFanout:
    """Several consumers subscribe to the same pattern. All must receive.

    This is the whole premise of §2.1 — a consumer can be added without
    touching anything else. An earlier version keyed handlers by topic in a
    dict, so PgWriter.start() silently replaced JsonlWriter's handler and the
    archive received nothing while the database filled normally. The durable
    store failed silently while the disposable one worked.
    """

    def _make_bus(self):
        from xams_sc.bus import Bus
        return Bus(client_id="test-fanout")

    def test_two_handlers_on_one_pattern_both_fire(self):
        bus = self._make_bus()
        seen_a, seen_b = [], []
        bus.subscribe("xams/meas/#", lambda t, p: seen_a.append(t))
        bus.subscribe("xams/meas/#", lambda t, p: seen_b.append(t))
        assert bus.subscriber_count == 2

        class Msg:
            topic = "xams/meas/pmain"
            payload = b'{"t":"2026-09-17T00:00:00.000Z","ch":"pmain","v":1.0,"u":"bar","q":"ok"}'

        bus._on_message(None, None, Msg())
        assert seen_a == ["xams/meas/pmain"]
        assert seen_b == ["xams/meas/pmain"]

    def test_a_raising_handler_does_not_starve_the_others(self):
        bus = self._make_bus()
        seen = []

        def explodes(topic, payload):
            raise RuntimeError("this subscriber is broken")

        bus.subscribe("xams/meas/#", explodes)
        bus.subscribe("xams/meas/#", lambda t, p: seen.append(t))

        class Msg:
            topic = "xams/meas/pmain"
            payload = b"{}"

        bus._on_message(None, None, Msg())
        assert seen == ["xams/meas/pmain"]

    def test_non_matching_pattern_is_not_called(self):
        bus = self._make_bus()
        meas, status = [], []
        bus.subscribe("xams/meas/#", lambda t, p: meas.append(t))
        bus.subscribe("xams/status/#", lambda t, p: status.append(t))

        class Msg:
            topic = "xams/meas/pmain"
            payload = b"{}"

        bus._on_message(None, None, Msg())
        assert len(meas) == 1
        assert status == []

    def test_real_sinks_both_register(self, config, tmp_path):
        """The actual failure, reproduced with the actual classes."""
        from xams_sc.sinks.jsonl_writer import JsonlWriter
        from xams_sc.sinks.pg_writer import PgWriter

        bus = self._make_bus()
        JsonlWriter(bus, config, directory=tmp_path).start()
        PgWriter(bus, dsn="host=127.0.0.1 dbname=nowhere", label="test").start()
        assert bus.subscriber_count == 2
