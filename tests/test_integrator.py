"""Flow integrator. See DESIGN.md §7.5.

The only stateful component in the system, so the only one where a restart can
lose something. These cover the three requirements §7.5 says must not be
skipped: it survives a restart, gaps are recorded rather than invented, and a
reset closes a period instead of erasing it.
"""

import json
from datetime import timedelta

import pytest

from xams_sc.config import load
from xams_sc.devices.derived import MAX_INTERVAL_S, FlowIntegrator
from xams_sc.model import Measurement, Quality, utcnow


class FakeBus:
    def __init__(self):
        self.published = []

    def publish_raw(self, topic, payload, retain=False):
        self.published.append((topic, json.loads(payload)))

    def subscribe(self, topic, handler):
        pass


@pytest.fixture
def config():
    return load()


def flow(t, value, quality=Quality.OK):
    return Measurement(t=t, channel="fm101", value=value, unit="g/min",
                       quality=quality)


def totals(bus):
    return [p["v"] for t, p in bus.published if t.endswith("fm101_total")]


class TestAccumulation:
    def test_six_grams_per_minute_for_one_minute_is_six_grams(self, config, tmp_path):
        bus = FakeBus()
        it = FlowIntegrator(bus, config, state_path=tmp_path / "s.json")
        t0 = utcnow()
        it.on_measurement(flow(t0, 6.0))
        it.on_measurement(flow(t0 + timedelta(seconds=60), 6.0))
        assert it.state.total_g == pytest.approx(6.0)

    def test_ten_second_steps(self, config, tmp_path):
        bus = FakeBus()
        it = FlowIntegrator(bus, config, state_path=tmp_path / "s.json")
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        for _ in range(6):
            t += timedelta(seconds=10)
            it.on_measurement(flow(t, 6.0))
        assert it.state.total_g == pytest.approx(6.0)

    def test_the_total_is_published(self, config, tmp_path):
        bus = FakeBus()
        it = FlowIntegrator(bus, config, state_path=tmp_path / "s.json")
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        it.on_measurement(flow(t + timedelta(seconds=60), 6.0))
        assert totals(bus)[-1] == pytest.approx(6.0)

    def test_unit_is_grams(self, config, tmp_path):
        bus = FakeBus()
        it = FlowIntegrator(bus, config, state_path=tmp_path / "s.json")
        it.on_measurement(flow(utcnow(), 6.0))
        assert bus.published[-1][1]["u"] == "g"


class TestSurvivesRestart:
    """§7.5: a Windows update that reset the total to zero would make the
    feature useless."""

    def test_total_is_restored(self, config, tmp_path):
        path = tmp_path / "s.json"
        bus = FakeBus()
        it = FlowIntegrator(bus, config, state_path=path)
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        it.on_measurement(flow(t + timedelta(seconds=60), 6.0))
        before = it.state.total_g
        assert before > 0

        # A completely new instance, as after a service restart.
        again = FlowIntegrator(FakeBus(), config, state_path=path)
        assert again.state.total_g == pytest.approx(before)

    def test_accumulation_continues_after_a_restart(self, config, tmp_path):
        """Picks up where it left off.

        The tolerance is deliberate and bounded, not a fudge. While running,
        dt is computed against the last sample time held in memory at full
        precision. Across a restart that is gone, so the persisted value is
        used — and the wire format is millisecond precision (§5), so dt can be
        wrong by up to 1 ms. At 6 g/min that is 0.1 mg, once per restart.

        Anything larger than this bound means the truncation has leaked back
        into the running path, where it would accumulate on every sample.
        """
        path = tmp_path / "s.json"
        t = utcnow()
        it = FlowIntegrator(FakeBus(), config, state_path=path)
        it.on_measurement(flow(t, 6.0))
        it.on_measurement(flow(t + timedelta(seconds=60), 6.0))

        again = FlowIntegrator(FakeBus(), config, state_path=path)
        again.on_measurement(flow(t + timedelta(seconds=120), 6.0))

        one_ms_of_flow = 6.0 * (0.001 / 60.0)
        assert again.state.total_g == pytest.approx(12.0, abs=one_ms_of_flow)

    def test_running_accumulation_has_no_truncation_drift(self, config, tmp_path):
        """The bias this guards against: truncating the previous timestamp
        makes every dt slightly too long, always in the same direction, so an
        integrator drifts upward forever. Exact equality here, no tolerance."""
        it = FlowIntegrator(FakeBus(), config, state_path=tmp_path / "s.json")
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        for i in range(1, 101):
            it.on_measurement(flow(t + timedelta(seconds=i * 10), 6.0))
        assert it.state.total_g == pytest.approx(100.0, abs=1e-9)

    def test_a_corrupt_state_file_refuses_rather_than_zeroing(self, config, tmp_path):
        """Silently restarting from zero is the data loss this class exists
        to prevent, so a corrupt file is fatal instead."""
        path = tmp_path / "s.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(Exception):
            FlowIntegrator(FakeBus(), config, state_path=path)


class TestGaps:
    """§7.5: extrapolating the last value across an outage is tempting and
    wrong."""

    def test_a_long_outage_is_not_integrated(self, config, tmp_path):
        bus = FakeBus()
        it = FlowIntegrator(bus, config, state_path=tmp_path / "s.json")
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        it.on_measurement(flow(t + timedelta(seconds=2 * 3600), 6.0))
        assert it.state.total_g == pytest.approx(0.0)
        assert it.state.gaps_s == pytest.approx(7200, abs=1)

    def test_the_gap_is_published_with_the_total(self, config, tmp_path):
        """The total carries the evidence that it is an underestimate."""
        bus = FakeBus()
        it = FlowIntegrator(bus, config, state_path=tmp_path / "s.json")
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        it.on_measurement(flow(t + timedelta(seconds=2 * 3600), 6.0))
        assert bus.published[-1][1]["gaps_s"] == pytest.approx(7200, abs=1)

    def test_a_bad_sample_is_not_integrated(self, config, tmp_path):
        it = FlowIntegrator(FakeBus(), config, state_path=tmp_path / "s.json")
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        it.on_measurement(flow(t + timedelta(seconds=30), None, Quality.ERROR))
        assert it.state.total_g == pytest.approx(0.0)
        assert it.state.gaps_s == pytest.approx(30, abs=1)

    def test_a_normal_interval_is_integrated(self, config, tmp_path):
        it = FlowIntegrator(FakeBus(), config, state_path=tmp_path / "s.json")
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        it.on_measurement(flow(t + timedelta(seconds=MAX_INTERVAL_S - 1), 6.0))
        assert it.state.total_g > 0
        assert it.state.gaps_s == pytest.approx(0.0)


class TestReset:
    """§7.5: reset closes a period; it does not erase."""

    def test_reset_returns_the_closed_period(self, config, tmp_path):
        it = FlowIntegrator(FakeBus(), config, state_path=tmp_path / "s.json")
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        it.on_measurement(flow(t + timedelta(seconds=60), 6.0))

        closed = it.reset("apc")
        assert closed["total_g"] == pytest.approx(6.0)
        assert closed["reset_by"] == "apc"
        assert closed["start"] and closed["stop"]

    def test_reset_starts_the_new_period_at_zero(self, config, tmp_path):
        it = FlowIntegrator(FakeBus(), config, state_path=tmp_path / "s.json")
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        it.on_measurement(flow(t + timedelta(seconds=60), 6.0))
        it.reset("apc")
        assert it.state.total_g == 0.0
        assert it.state.gaps_s == 0.0

    def test_reset_survives_a_restart(self, config, tmp_path):
        path = tmp_path / "s.json"
        it = FlowIntegrator(FakeBus(), config, state_path=path)
        t = utcnow()
        it.on_measurement(flow(t, 6.0))
        it.on_measurement(flow(t + timedelta(seconds=60), 6.0))
        it.reset("apc")
        again = FlowIntegrator(FakeBus(), config, state_path=path)
        assert again.state.total_g == 0.0
