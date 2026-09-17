"""CAEN protocol parsing, against recorded responses. See DESIGN.md §13.

Every string below was captured from the real supplies on 17 September 2026 —
serial 19198 (firmware 1.08) and serial 79 (firmware 1.04). The two run
different firmware, so both are represented: a divergence should fail here
rather than in the lab.

The malformed cases matter more than the well-formed ones. §7.2 requires that
"a malformed or absent reply is a read error, never a silently substituted
value", and a parser that returns 0.0 for a truncated response would put a
plausible zero on an electrode monitor.
"""

import pytest

from xams_sc.devices.caen import STAT_BITS, _decode, describe_status
from xams_sc.scaling import apply_sign


class TestDecode:
    """Recorded replies from both units."""

    @pytest.mark.parametrize("response,expected", [
        ("#BD:00,CMD:OK,VAL:DT1470ET", "DT1470ET"),
        ("#BD:00,CMD:OK,VAL:19198", "19198"),       # firmware 1.08 unit
        ("#BD:00,CMD:OK,VAL:79", "79"),             # firmware 1.04 unit
        ("#BD:00,CMD:OK,VAL:1.08", "1.08"),
        ("#BD:00,CMD:OK,VAL:4", "4"),
        ("#BD:00,CMD:OK,VAL:0700.0", "0700.0"),
        ("#BD:00,CMD:OK,VAL:0000.0", "0000.0"),
        ("#BD:00,CMD:OK,VAL:000.000", "000.000"),
        ("#BD:00,CMD:OK,VAL:01024", "01024"),
        ("#BD:00,CMD:OK,VAL:-", "-"),               # POL, negative
        ("#BD:00,CMD:OK,VAL:+", "+"),               # POL, positive
    ])
    def test_well_formed(self, response, expected):
        assert _decode(response) == expected

    @pytest.mark.parametrize("response", [
        "",                                  # nothing came back
        "   ",
        "#BD:00,CMD:ERR",                    # the board rejected the command
        "#BD:00,CMD:OK",                     # OK but no value
        "#BD:00,CMD:OK,VAL:",                # empty value
        "BD:00,CMD:OK,VAL:19198",            # missing leading #
        "#BD:00,CMD:OK,VAL",                 # truncated mid-key
        "#BD:00,CM",                         # truncated early
        "garbage",
        "\x00\xff\x13",                      # line noise
    ])
    def test_malformed_returns_none(self, response):
        """None, never a substituted value. This is the whole point."""
        assert _decode(response) is None

    def test_trailing_whitespace_and_newlines(self):
        assert _decode("#BD:00,CMD:OK,VAL:19198\r\n") == "19198"
        assert _decode("  #BD:00,CMD:OK,VAL:79  ") == "79"

    def test_a_truncated_number_is_not_silently_zero(self):
        # The dangerous failure: something that looks parseable but is not.
        assert _decode("#BD:00,CMD:ERR,VAL:0000.0") is None


class TestStatusWord:
    def test_all_channels_off_reads_1024(self):
        """Every channel on both units read 01024 with outputs off."""
        assert describe_status(1024) == "DISABLED"

    def test_zero_is_off(self):
        assert describe_status(0) == "OFF"

    def test_on_and_ramping(self):
        assert describe_status(0b011) == "ON,RAMP_UP"

    def test_trip_is_reported(self):
        assert "TRIP" in describe_status(1 << 7)

    def test_interlock_is_reported(self):
        assert "INTERLOCK" in describe_status(1 << 12)

    def test_every_bit_has_a_name(self):
        for bit in range(14):
            assert bit in STAT_BITS, f"status bit {bit} unnamed"

    def test_combined_flags(self):
        word = (1 << 0) | (1 << 7) | (1 << 10)
        described = describe_status(word)
        assert "ON" in described and "TRIP" in described and "DISABLED" in described


class TestSignConvention:
    """The supplies report unsigned magnitudes with POL separate (§7.2).

    Recorded VSET values, 17 September 2026:
      hv_1: 700.0, 1000.0, 500.0, 600.0     all POL '-'
      hv_2: 2250.0, 1750.0 POL '-'; 4200.0, 600.0 POL '+'
    """

    @pytest.mark.parametrize("magnitude,sign,expected", [
        (700.0, -1, -700.0),      # PMT bottom
        (1000.0, -1, -1000.0),    # PMT top
        (2250.0, -1, -2250.0),    # cathode
        (1750.0, -1, -1750.0),    # gate
        (4200.0, 1, 4200.0),      # anode
        (600.0, 1, 600.0),        # NaI
        (0.0, -1, 0.0),           # off
    ])
    def test_recorded_setpoints(self, magnitude, sign, expected):
        assert apply_sign(magnitude, sign) == pytest.approx(expected)

    def test_cathode_and_anode_end_up_opposite(self):
        """The reason the convention exists: these must not plot together
        as if both were positive."""
        cathode = apply_sign(2250.0, -1)
        anode = apply_sign(4200.0, 1)
        assert cathode < 0 < anode


class TestMonitorCommandsAreReadOnly:
    """Milestone 5 is read-only. There must be no way to write a setpoint."""

    def test_no_set_command_in_the_module(self):
        import inspect

        from xams_sc.devices import caen
        source = inspect.getsource(caen)
        # A SET would appear as CMD:SET in a formatted command string.
        assert "CMD:SET" not in source, (
            "caen.py contains a SET command. The control path is milestone 8 "
            "and needs §10's open decision resolved first.")

    def test_command_builder_only_emits_mon(self):
        from xams_sc.devices.caen import CaenChannelReader
        import inspect
        source = inspect.getsource(CaenChannelReader._command)
        assert "CMD:MON" in source
        assert "CMD:SET" not in source
