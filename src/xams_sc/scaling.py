"""Raw values to engineering units. See DESIGN.md §4.2.

The whole module is one formula, and that is the point: scaling lives here and
nowhere else, so there is exactly one place to look when a number is wrong.
"""

from __future__ import annotations


def apply(raw: float, offset: float = 0.0, multiplier: float = 1.0) -> float:
    """value = (raw - offset) * multiplier

    This matches the LabVIEW implementation exactly — its front panel labels
    the two columns "Offsets subtracted" and "Multipliers", in that order.

    DO NOT change the convention or the order of operations. Every historical
    CSV value was produced this way, and `tools/compare_to_labview.py` checks
    the new system against them.
    """
    return (raw - offset) * multiplier


def apply_sign(magnitude: float, sign: int) -> float:
    """Apply a polarity to an unsigned magnitude from a CAEN supply.

    The DT1470ET reports VMON and VSET as unsigned magnitudes with polarity as
    a separate POL parameter: a cathode at minus 2250 volts answers "2250.0".
    Values are stored SIGNED (§7.2), so the sign is applied once, here.

    This is almost certainly what the LabVIEW `DAISY_polarity_signs.vi` does.
    """
    if sign not in (-1, 1):
        raise ValueError(f"sign must be -1 or +1, got {sign!r}")
    return abs(magnitude) * sign


def strip_sign(value: float) -> float:
    """Inverse of apply_sign, for values going back out to a supply.

    A setpoint is held signed in this system and must be sent as a magnitude.
    Polarity is a hardware setting and is never changed by a write (§8.3).
    """
    return abs(value)


def integrate_step(flow_per_minute: float, dt_seconds: float) -> float:
    """Mass passed during one interval, in grams.

    `fm101` is a MASS FLOW IN GRAMS PER MINUTE — per *minute*. The interval
    arrives in seconds, so it is converted here, once.

    Two traps, both covered in tests/test_scaling.py:

      * Using dt in seconds without converting gives a factor-60 error that
        looks entirely plausible in a plot and can go unnoticed for months.
      * The LabVIEW front panel labels this channel "Flow (SLPM)". That label
        is WRONG. It is not a volumetric standard-litre flow, and nobody
        should "correct" this back by consulting the old front panel (§4.2).
    """
    return flow_per_minute * (dt_seconds / 60.0)
