"""The CAEN channel STATUS word. See DESIGN.md §7.2.

The supplies have a physical enable per channel, and **a channel can be
enabled while sitting at zero volts**. Read from the hardware on
17 September 2026:

    hv_1 ch1 — STAT=1 (ON), VSET=0.0, VMON=0.0
    every other channel — STAT=1024 (DISABLED)

The web UI originally inferred on/off from `VMON > 1`, which called that
channel "off". It is not off: it is on, at zero, and one turn of a setpoint
away from putting volts on a photomultiplier. Understating what is live is the
kind of wrong that gets somebody hurt, so these tests exist to keep the
inference from creeping back.
"""

import pytest

from xams_sc.hv_status import (FAULT_BITS, STAT_BITS, describe_status,
                               is_disabled, is_energised, status_faults)

# Recorded from the two units, 17 September 2026.
ENABLED_AT_ZERO = 1       # hv_1 ch1
DISABLED = 1024           # every other channel


class TestTheRecordedWords:
    def test_enabled_at_zero_volts_reads_as_on(self):
        """The whole point. VMON was 0.0 when this word was read."""
        assert is_energised(ENABLED_AT_ZERO) is True
        assert describe_status(ENABLED_AT_ZERO) == "ON"

    def test_disabled_reads_as_not_on(self):
        assert is_energised(DISABLED) is False
        assert is_disabled(DISABLED) is True
        assert describe_status(DISABLED) == "DISABLED"

    def test_neither_word_carries_a_fault(self):
        """A supply sitting idle must not light the page up red."""
        assert status_faults(ENABLED_AT_ZERO) == []
        assert status_faults(DISABLED) == []


class TestOnIsBitZeroNotAVoltage:
    def test_on_is_bit_zero(self):
        assert STAT_BITS[0] == "ON"

    def test_enabled_and_disabled_are_separate_questions(self):
        """Not simply each other's negation: the board reports them as two
        bits, and a word can carry neither."""
        assert is_energised(0) is False
        assert is_disabled(0) is False

    @pytest.mark.parametrize("word", [1, 3, 5, 129, 0b1000000000001])
    def test_on_survives_other_bits_being_set(self, word):
        """Ramping, tripped, interlocked — still ON, and still dangerous."""
        assert is_energised(word) is True


class TestFaults:
    def test_a_trip_is_a_fault(self):
        assert "TRIP" in status_faults(1 << 7)

    def test_an_interlock_is_a_fault(self):
        assert "INTERLOCK" in status_faults(1 << 12)

    @pytest.mark.parametrize("bit,name", [(0, "ON"), (1, "RAMP_UP"),
                                          (2, "RAMP_DOWN"), (10, "DISABLED")])
    def test_ordinary_states_are_not_faults(self, bit, name):
        """A supply ramping to its setpoint is working, not failing. Dressing
        these up as faults is how a display trains people to ignore it."""
        assert status_faults(1 << bit) == []
        assert bit not in FAULT_BITS

    def test_an_enabled_channel_that_trips_reports_both(self):
        word = (1 << 0) | (1 << 7)
        assert is_energised(word) is True
        assert status_faults(word) == ["TRIP"]
        assert describe_status(word) == "ON,TRIP"

    def test_faults_come_back_in_bit_order(self):
        """Stable ordering, so the page does not reshuffle between refreshes
        for no reason."""
        word = (1 << 12) | (1 << 3) | (1 << 7)
        assert status_faults(word) == ["OVER_CURRENT", "TRIP", "INTERLOCK"]


class TestEveryBitIsAccountedFor:
    def test_every_fault_bit_has_a_name_in_the_status_table(self):
        for bit, name in FAULT_BITS.items():
            assert STAT_BITS[bit] == name

    def test_a_word_of_zero_is_off_not_blank(self):
        assert describe_status(0) == "OFF"
