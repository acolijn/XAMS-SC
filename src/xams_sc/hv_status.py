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
BIT_TRIP = 7
BIT_DISABLED = 10

# How far a setpoint must sit from zero before a DISABLED channel counts as
# ARMED - far enough that flipping the enable by hand would put real volts on
# an electrode, and comfortably above the board's own rounding on a value that
# was written as zero.
#
# Shared by the page that warns about it and the driver that zeroes it, so the
# two cannot drift apart. A page calling a channel armed that the driver will
# not act on - or the reverse - is worse than either behaviour alone.
ARMED_ABOVE_V = 1.0

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


def is_energised(word: int) -> bool:
    """Is the OUTPUT energised? Bit 0.

    **This is not "is the channel enabled".** It was called `is_enabled` until
    18 September 2026, and that name caused four separate bugs in one
    afternoon, because in this system's own vocabulary "enabled" means the
    front-panel switch - which is bit 10, and a different question:

      * `is_disabled(word)`  - the switch is OFF (bit 10)
      * `is_energised(word)` - the output is ON (bit 0)
      * neither              - switch on, output off. Permitted and inert.

    Every one of the four bugs was the same shape: code that meant "has the
    operator flipped the switch" and asked "is it putting out volts". It
    displayed a live channel as off, refused a setpoint to a channel that was
    ready for one, and warned that an enabled channel was dangerous to enable.

    The name is the fix. Anything that wants the switch asks `is_disabled`.
    """
    return bool(word & (1 << BIT_ON))


def is_disabled(word: int) -> bool:
    """Is it explicitly DISABLED? Bit 10.

    Not simply `not is_energised(...)`: a word could have neither bit set, and
    the two questions are reported separately by the board.
    """
    return bool(word & (1 << BIT_DISABLED))


def is_tripped(word: int) -> bool:
    """Did the board trip this channel? Bit 7.

    **It latches.** The channel is switched OFF and the bit stays set until
    the board alarm is cleared (`BDCLR`); neither ON nor the enable switch
    clears it. That is what made a trip need a power cycle before
    23 September 2026 (§10a *Recovering from a trip*).
    """
    return bool(word & (1 << BIT_TRIP))
