"""cDAQ driver tests. No hardware required.

These cover the two things about this driver that are easy to get wrong and
expensive to get wrong: the excitation current, which DAQmx will not choose
correctly on its own, and the difference between a cold sensor and no sensor.
"""

import pytest

from xams_sc.config import load
from xams_sc.devices.cdaq import EXCITATION_A, RTD_VALID_C, CdaqService
from xams_sc.model import Quality


class RecordingBus:
    def __init__(self):
        self.measurements = []
        self.handlers = []

    def publish_measurement(self, m):
        self.measurements.append(m)

    def publish_state(self, s, st):
        pass

    def publish_heartbeat(self, s):
        pass

    def subscribe(self, t, h):
        self.handlers.append((t, h))

    def connect(self):
        pass

    def disconnect(self):
        pass


@pytest.fixture
def config():
    return load()


@pytest.fixture
def service(config, tmp_path):
    return CdaqService(config, RecordingBus(), simulate=True, lock_dir=tmp_path)


class TestExcitation:
    """Measured from the hardware, 17 September 2026.

    Each module accepts exactly one value, and neither is the DAQmx default of
    2.5 mA. Getting this wrong does not produce a wrong reading — the task
    refuses to configure — but the values must not drift from what the modules
    actually accept.
    """

    def test_pt1000_module_runs_at_100_microamp(self):
        assert EXCITATION_A["NI9226"] == pytest.approx(100e-6)

    def test_pt100_module_runs_at_1_milliamp(self):
        assert EXCITATION_A["NI9216"] == pytest.approx(1e-3)

    def test_neither_is_the_daqmx_default(self):
        # DAQmx defaults to 2.5 mA, which BOTH modules refuse. This is why the
        # argument cannot be omitted, contrary to §7.1's example code.
        for value in EXCITATION_A.values():
            assert value != pytest.approx(2.5e-3)

    def test_pt1000_draws_less_current_than_pt100(self):
        # 1000 ohm at 1 mA would put a milliwatt into the sensor and self-heat
        # it. The tenfold lower current on the PT1000 module is the reason.
        assert EXCITATION_A["NI9226"] < EXCITATION_A["NI9216"]

    def test_every_rtd_module_in_the_config_has_an_excitation(self, config):
        models = {m["model"] for m in config.devices["cdaq"]["modules"]}
        rtd_models = {m for m in models if m in ("NI9216", "NI9226")}
        assert rtd_models, "no RTD modules in devices.yaml?"
        for model in rtd_models:
            assert model in EXCITATION_A, f"{model} has no excitation current"


class TestTaskBuilding:
    def test_unconnected_module_gets_no_task(self, service):
        """9216_2 is entirely unconnected — it must cost nothing."""
        aliases = {m.alias for m in service._modules}
        assert "9216_2" not in aliases

    def test_connected_modules_all_present(self, service):
        aliases = {m.alias for m in service._modules}
        assert aliases == {"9207", "9216_1", "9226"}

    def test_channels_are_ordered_by_physical_index(self, service):
        """Read order must match the order channels were added to the task,
        or every value lands on the wrong channel name."""
        for mod in service._modules:
            idx = [int(c.phys.split("/ai")[1]) for c in mod.channels]
            assert idx == sorted(idx), f"{mod.alias} channels out of order"

    def test_channel_count_matches_what_is_actually_wired(self, service):
        """19 live channels: 6 voltage + 13 RTD.

        Milestone 2 read 20 (6 + 14). tt202 was then found to be a failed
        sensor — it reads open-circuit — and disabled on 17 September 2026,
        so the 9226 now contributes 6 RTDs rather than 7.

        This asserts an exact count on purpose. A channel silently appearing
        or disappearing is worth a failing test: it means either the wiring
        or channels.yaml changed, and both deserve a look.
        """
        total = sum(len(m.channels) for m in service._modules)
        assert total == 19
        kinds = [c.kind for m in service._modules for c in m.channels]
        assert kinds.count("voltage") == 6
        assert kinds.count("rtd") == 13

    def test_failed_sensor_is_not_read(self, config, service):
        """tt202 is disabled, so no task should reference 9226/ai1."""
        assert config.channels["tt202"].enabled is False
        phys = [c.phys for m in service._modules for c in m.channels]
        assert "9226/ai1" not in phys

    def test_current_channels_are_not_read(self, service):
        """9207/ai8:15 are unused and must not appear in any task (§7.1)."""
        phys = [c.phys for m in service._modules for c in m.channels]
        for n in range(8, 16):
            assert f"9207/ai{n}" not in phys


class TestOpenCircuitDetection:
    """A disconnected RTD input does not read a temperature. It reads the rail.

    On this hardware an unconnected 9226 input swings between roughly -245 and
    +1327 C. Publishing that as quality=ok is the frozen-plausible-value
    failure principle 4 forbids, and it is how tt202 was found.
    """

    def test_valid_range_is_the_iec_60751_span(self):
        assert RTD_VALID_C == (-200.0, 850.0)

    @pytest.mark.parametrize("reading", [1326.57, -245.0, 9999.0, -1000.0])
    def test_out_of_range_is_rejected(self, reading):
        lo, hi = RTD_VALID_C
        assert not (lo <= reading <= hi)

    @pytest.mark.parametrize("reading", [-90.3, -56.1, 24.2, 48.7, 0.0, -199.9, 849.9])
    def test_real_readings_are_accepted(self, reading):
        """Including the genuinely cold cryostat sensors, which must NOT be
        mistaken for a fault: -90 C is a real measurement."""
        lo, hi = RTD_VALID_C
        assert lo <= reading <= hi

    def test_a_cold_sensor_is_not_a_fault(self):
        # The cryostat runs near -90 C. If the valid range were ever narrowed
        # to something "reasonable" like -100..100, real data would be thrown
        # away. The bound is the sensor standard, not an expectation.
        assert RTD_VALID_C[0] < -90.0


class TestSimulation:
    def test_simulate_produces_every_enabled_channel(self, service):
        batch = service.read()
        expected = {c.name for m in service._modules for c in m.channels}
        assert {m.channel for m in batch} == expected

    def test_simulate_needs_no_hardware(self, service):
        assert service.verify_identity() is True

    def test_simulated_values_are_quality_ok(self, service):
        assert all(m.quality is Quality.OK for m in service.read())

    def test_simulated_rtds_are_inside_the_valid_range(self, service):
        by_name = {m.channel: m.raw for m in service.read()}
        for mod in service._modules:
            for ch in mod.channels:
                if ch.kind == "rtd":
                    lo, hi = RTD_VALID_C
                    assert lo <= by_name[ch.name] <= hi, ch.name


class TestScalingThroughTheDriver:
    def test_voltage_channels_carry_their_configured_scaling(self, config, service):
        """p101 is (raw - 1.0) * 25.0 — the LabVIEW convention (§4.2)."""
        ch = config.channels["p101"]
        assert ch.offset == 1.0 and ch.multiplier == 25.0

    def test_rtd_scaling_is_a_no_op(self, config):
        """DAQmx returns degrees C directly; do not hand-roll the conversion."""
        for name in ("tt201", "tt301", "ttamb"):
            ch = config.channels[name]
            assert ch.offset == 0.0 and ch.multiplier == 1.0
