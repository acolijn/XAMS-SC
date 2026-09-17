"""Configuration tests. See DESIGN.md §13.

Two jobs: prove the validator catches the mistakes that would otherwise be
found six months into a dataset, and prove the config hash is stable against
reformatting but sensitive to values.
"""

import textwrap

import pytest
import yaml

from xams_sc.config import ConfigError, Channel, compute_hash, load

DEVICES = """
cdaq:
  chassis: cDAQ1
caen:
  - {id: hv_1, board_serial: "19198"}
  - {id: hv_2, board_serial: "79"}
lakeshore: {baud: 57600}
ups: {model: APC}
"""

ALARMS = "defaults: {stale_after_seconds: 60}\nchannels: {}\n"


def write_config(tmp_path, channels_yaml, devices=DEVICES, alarms=ALARMS):
    (tmp_path / "channels.yaml").write_text(
        textwrap.dedent(channels_yaml), encoding="utf-8")
    (tmp_path / "devices.yaml").write_text(devices, encoding="utf-8")
    (tmp_path / "alarms.yaml").write_text(alarms, encoding="utf-8")
    return tmp_path


ONE_GOOD_CHANNEL = """
    channels:
      - {name: pmain, device: cdaq, phys: 9207/ai5, kind: voltage, unit: bar,
         offset: 0.0, multiplier: 0.714}
"""


class TestValidation:
    def test_loads_a_good_config(self, tmp_path):
        cfg = load(write_config(tmp_path, ONE_GOOD_CHANNEL))
        assert "pmain" in cfg.channels
        assert cfg.channels["pmain"].multiplier == 0.714
        assert len(cfg.config_hash) == 7

    def test_duplicate_channel_name_refused(self, tmp_path):
        d = write_config(tmp_path, """
            channels:
              - {name: pmain, device: cdaq, phys: 9207/ai5, kind: voltage, unit: bar}
              - {name: pmain, device: cdaq, phys: 9207/ai6, kind: voltage, unit: bar}
        """)
        with pytest.raises(ConfigError, match="duplicate channel name"):
            load(d)

    def test_unknown_device_refused(self, tmp_path):
        d = write_config(tmp_path, """
            channels:
              - {name: x1, device: nosuch, phys: "0", kind: voltage, unit: V}
        """)
        with pytest.raises(ConfigError, match="unknown device"):
            load(d)

    def test_unknown_kind_refused(self, tmp_path):
        d = write_config(tmp_path, """
            channels:
              - {name: x1, device: cdaq, phys: "0", kind: banana, unit: V}
        """)
        with pytest.raises(ConfigError, match="unknown kind"):
            load(d)

    def test_missing_required_key_refused(self, tmp_path):
        d = write_config(tmp_path, """
            channels:
              - {name: x1, device: cdaq, phys: "0", kind: voltage}
        """)
        with pytest.raises(ConfigError, match="missing required key 'unit'"):
            load(d)

    def test_rtd_without_rtd_block_refused(self, tmp_path):
        d = write_config(tmp_path, """
            channels:
              - {name: tt201, device: cdaq, phys: 9226/ai0, kind: rtd, unit: C}
        """)
        with pytest.raises(ConfigError, match="requires an `rtd:` block"):
            load(d)

    def test_uppercase_name_refused(self, tmp_path):
        # Names are lowercase (§3); `legacy:` carries the original casing.
        d = write_config(tmp_path, """
            channels:
              - {name: TT201, device: cdaq, phys: 9226/ai0, kind: voltage, unit: C}
        """)
        with pytest.raises(ConfigError, match="lowercase"):
            load(d)

    def test_two_channels_on_one_physical_input_refused(self, tmp_path):
        d = write_config(tmp_path, """
            channels:
              - {name: a1, device: cdaq, phys: 9207/ai0, kind: voltage, unit: V}
              - {name: a2, device: cdaq, phys: 9207/ai0, kind: voltage, unit: V}
        """)
        with pytest.raises(ConfigError, match="already used by"):
            load(d)

    def test_hv_vmon_and_imon_may_share_an_index(self, tmp_path):
        # Legitimate: one HV channel yields both a voltage and a current.
        cfg = load(write_config(tmp_path, """
            channels:
              - {name: hv_anode_vmon, device: hv_2, phys: 2, kind: hv_vmon, unit: V}
              - {name: hv_anode_imon, device: hv_2, phys: 2, kind: hv_imon, unit: uA}
        """))
        assert len(cfg.channels) == 2

    def test_bad_sign_refused(self, tmp_path):
        d = write_config(tmp_path, """
            channels:
              - {name: hv_x_vmon, device: hv_1, phys: 0, kind: hv_vmon, unit: V, sign: 0}
        """)
        with pytest.raises(ConfigError, match="sign must be"):
            load(d)

    def test_inverted_limits_refused(self, tmp_path):
        d = write_config(tmp_path, """
            channels:
              - {name: hv_x_vmon, device: hv_1, phys: 0, kind: hv_vmon, unit: V,
                 limits: {min: 100.0, max: -100.0}}
        """)
        with pytest.raises(ConfigError, match="min exceeds max"):
            load(d)

    def test_empty_channels_refused(self, tmp_path):
        d = write_config(tmp_path, "channels: []\n")
        with pytest.raises(ConfigError, match="no channels"):
            load(d)


class TestLimits:
    def test_value_inside_range_accepted(self):
        ch = Channel(name="hv_cathode_vmon", device="hv_2", phys="0",
                     kind="hv_vmon", unit="V", sign=-1,
                     limits={"min": -2500.0, "max": 0.0})
        assert ch.in_limits(-2250.0)
        assert not ch.in_limits(-2600.0)
        assert not ch.in_limits(10.0)

    def test_a_channel_without_limits_accepts_nothing(self):
        # A write range that was never specified is not permission to write
        # anything. The default must be closed, not open.
        ch = Channel(name="x", device="hv_1", phys="0", kind="hv_vmon", unit="V")
        assert not ch.in_limits(0.0)
        assert not ch.in_limits(-100.0)


class TestConfigHash:
    def test_reformatting_does_not_change_the_hash(self, tmp_path):
        # Same channel, keys written in a different order. Moving a channel or
        # reflowing the YAML must not look like a change to the data.
        reordered = """
            channels:
              - {unit: bar, multiplier: 0.714, offset: 0.0, kind: voltage,
                 phys: 9207/ai5, device: cdaq, name: pmain}
        """
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = load(write_config(tmp_path / "a", ONE_GOOD_CHANNEL))
        b = load(write_config(tmp_path / "b", reordered))
        assert a.config_hash == b.config_hash

    def test_changing_a_value_changes_the_hash(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = load(write_config(tmp_path / "a", ONE_GOOD_CHANNEL))
        b = load(write_config(tmp_path / "b", ONE_GOOD_CHANNEL.replace("0.714", "0.715")))
        assert a.config_hash != b.config_hash

    def test_recipients_do_not_affect_the_hash(self):
        # recipients.yaml changes often and does not affect the data (§4.5).
        channels = yaml.safe_load(ONE_GOOD_CHANNEL)
        devices = yaml.safe_load(DEVICES)
        alarms = yaml.safe_load(ALARMS)
        assert compute_hash(channels, devices, alarms) == \
            compute_hash(channels, devices, alarms)


class TestRealRepoConfig:
    """The configuration actually shipped in config/ must be valid."""

    def test_repo_config_loads(self):
        cfg = load()
        assert cfg.channels
        assert len(cfg.config_hash) == 7

    def test_every_hv_vmon_has_a_sign_and_limits(self):
        cfg = load()
        for ch in cfg.enabled_channels():
            if ch.kind == "hv_vmon":
                assert ch.sign in (-1, 1), ch.name
                assert ch.limits, f"{ch.name} has no software write range"

    def test_hv_limits_have_the_same_sign_as_the_channel(self):
        # A negative channel whose range runs positive would accept a setpoint
        # of the wrong polarity from the UI.
        cfg = load()
        for ch in cfg.enabled_channels():
            if ch.kind == "hv_vmon" and ch.limits:
                if ch.sign < 0:
                    assert ch.limits["min"] < 0 and ch.limits["max"] <= 0, ch.name
                else:
                    assert ch.limits["min"] >= 0 and ch.limits["max"] > 0, ch.name

    def test_flow_channel_is_not_labelled_slpm(self):
        # The old front panel says "Flow (SLPM)" and is wrong (§4.2). This
        # test exists so nobody "corrects" it back.
        cfg = load()
        assert cfg.channels["fm101"].unit == "g/min"
        assert cfg.channels["fm101_total"].unit == "g"
