# CAEN DT1470ET

Two units, four HV channels each. Neither carries a USB serial number, so
identity rests entirely on the `BDSNUM` query.

The software **reads** the channel settings at startup and alarms on a
mismatch. It never writes them: ramp rate, trip current and over-voltage limit
are configured on the instrument and stay there.

!!! warning "Not written yet"
    Wanted: the serial protocol, the status word decoding, what each fault bit means and what to do about it. Source: `src/xams_sc/devices/caen.py`, `src/xams_sc/hv_status.py`.
