"""Decoding the CAEN channel STATUS word. See DESIGN.md §7.2.

Separate from `devices/caen.py` because it is pure logic with no instrument
behind it, and because the web UI needs it. `caen.py` imports `serial`, which
belongs to the optional `hardware` extra — importing it into the UI would make
a machine that only serves pages need pyserial to start.

**Bit 0 is the answer to "is this channel on", not VMON.** The supplies have a
physical enable per channel, and a channel can be enabled while sitting at
zero volts:

    17 Sep 2026, hv_1 ch1 — STAT=1 (ON), VSET=0.0, VMON=0.0

Inferring on/off from the voltage, as the web UI first did, calls that "off".
It is not off: it is on, at zero, and one turn of a setpoint away from putting
volts on a photomultiplier. A display that understates what is live is the
kind of wrong that gets somebody hurt.
"""

from __future__ import annotations

# DT1470ET status word. Confirmed against both units on 17 September 2026:
# bit 10 on every channel that was disabled, bit 0 on the one that was not.
STAT_BITS = {
    0: "ON", 1: "RAMP_UP", 2: "RAMP_DOWN", 3: "OVER_CURRENT",
    4: "OVER_VOLTAGE", 5: "UNDER_VOLTAGE", 6: "MAX_V", 7: "TRIP",
    8: "OVER_POWER", 9: "OVER_TEMP", 10: "DISABLED", 11: "KILL",
    12: "INTERLOCK", 13: "UNCALIBRATED",
}

BIT_ON = 0
BIT_DISABLED = 10

# Bits meaning something is WRONG, as against describing what the channel is
# doing. ON, RAMP_UP, RAMP_DOWN and DISABLED are ordinary states and must not
# be dressed up as faults, or the display cries wolf at a supply behaving
# exactly as intended.
FAULT_BITS = {
    3: "OVER_CURRENT", 4: "OVER_VOLTAGE", 5: "UNDER_VOLTAGE", 6: "MAX_V",
    7: "TRIP", 8: "OVER_POWER", 9: "OVER_TEMP", 11: "KILL",
    12: "INTERLOCK", 13: "UNCALIBRATED",
}


def describe_status(word: int) -> str:
    flags = [name for bit, name in STAT_BITS.items() if word & (1 << bit)]
    return ",".join(flags) if flags else "OFF"


def status_faults(word: int) -> list[str]:
    """The fault flags set in a status word, if any."""
    return [name for bit, name in sorted(FAULT_BITS.items())
            if word & (1 << bit)]


def is_enabled(word: int) -> bool:
    """Is this channel's output switched ON? Bit 0, never the voltage."""
    return bool(word & (1 << BIT_ON))


def is_disabled(word: int) -> bool:
    """Is it explicitly DISABLED? Bit 10.

    Not simply `not is_enabled(...)`: a word could have neither bit set, and
    the two questions are reported separately by the board.
    """
    return bool(word & (1 << BIT_DISABLED))
