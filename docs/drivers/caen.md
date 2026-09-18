# CAEN DT1470ET

Two units, four HV channels each. Neither carries a USB serial number, so
identity rests entirely on the `BDSNUM` query.

The driver **writes exactly three things: `VSET`, `ON` and `OFF`** (§10a), each
range-checked against `channels.yaml`, read back from the board and audited.
How to drive it is in [The web interface](../operating/webui.md#high-voltage).

It **reads** the protection settings at startup and alarms on a mismatch, and
never writes them: ramp rate, trip current and over-voltage limit are configured
on the instrument and stay there. Nor can it enable a channel or take a board
out of `LOCAL` — those are hand operations, and they are the two gates between
this software and an electrode.

!!! warning "Not written yet"
    Wanted: the serial protocol, the status word decoding, what each fault bit means and what to do about it. Source: `src/xams_sc/devices/caen.py`, `src/xams_sc/hv_status.py`.
