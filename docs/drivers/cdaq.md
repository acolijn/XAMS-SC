# NI cDAQ-9174

One chassis, four modules: NI 9207 (voltage), two NI 9216 (PT100), NI 9226
(PT1000).

RTD excitation current is not the DAQmx default and each module accepts
exactly one value — 100 µA for the 9226, 1 mA for the 9216 — so both must be
set explicitly or the task refuses to configure.

**Read-only.** The chassis has no output module, so this service has no
control path and no code that could write to hardware.

It carries 20 of the roughly 30 connected channels — the pressures, the flow
meter and every cryostat RTD — which makes it the service whose silence is
most noticeable.

---

## The slot map

| Alias | Module | Slot | Reads | Channels |
|---|---|---|---|---|
| `9207` | NI 9207 | 1 | ±10 V | `ai0:7` — 4 pressures, detector pressure, flow meter, 2 unconnected |
| `9216_1` | NI 9216 | 2 | PT100 | `ai0:6` — water, xenon and heat-exchanger temperatures, ambient |
| `9216_2` | NI 9216 | 3 | PT100 | **none — entirely unconnected** |
| `9226` | NI 9226 | 4 | PT1000 | `ai0:6` — bucket and vessel temperatures |

**Modules are addressed by alias, never by `cDAQ1Mod1/ai0`.** Aliases are
configured in NI-MAX and follow the module, so re-slotting a module changes
nothing here. `phys` in [`channels.yaml`](../software/config.md) is
`<alias>/<line>` — `9207/ai0`.

A module with no enabled channels gets **no task at all**, which is how
`9216_2` costs nothing beyond a row in `devices.yaml`.

### The 9207's current inputs are not used

The module has eight voltage inputs (`ai0:7`) and eight current inputs
(`ai8:15`). Only the voltage half is wired and only it is read (§7.1).
Nothing is connected to the current inputs, so a task for them would read
noise and publish it as a measurement.

---

## Why on-demand reads

All four modules are low-rate delta-sigma with differing aggregate rates, and
**they cannot share one hardware-timed task**. Slow control reads at 1 Hz, so
this driver uses **software-timed (on-demand) reads — one task per module,
polled in sequence**. The CompactDAQ timing-engine constraint then does not
apply at all.

This is the single most important thing to know before changing the
acquisition here: reaching for a hardware-timed, multi-module task is the
obvious optimisation, and it does not work on this chassis.

---

## Excitation current

```python
EXCITATION_A = {"NI9226": 100e-6,   # PT1000 — 1 mA and 2.5 mA both refused
                "NI9216": 1e-3}     # PT100  — 100 µA and 2.5 mA both refused
```

Measured from the hardware on 17 September 2026. Each module accepts exactly
one value, and neither is the DAQmx default of 2.5 mA, so both are passed
explicitly.

The physics agrees: 1000 Ω at 1 mA would dissipate a milliwatt in the sensor
and self-heat it, which is why the PT1000 module runs at a tenth of the
current.

!!! warning "Do not let DAQmx choose"
    Dropping the argument is exactly what §7.1's example code does, and the
    task then refuses to configure. It looks like a wiring fault.

RTD type, `r0` and wiring come from each channel's `rtd:` block —
`PT3750`/1000 Ω on the 9226, `PT3851`/100 Ω on the 9216, three-wire
throughout.

---

## Identity

At startup the driver asks NI-DAQmx for the chassis and every module, and
compares **serial numbers** against `devices.yaml`:

```
chassis cDAQ1 serial 020C5E1C confirmed
module 9207 serial 020DFD57 confirmed
```

A mismatch is fatal and the service refuses to start. **A module swapped
between slots is harmless** — the alias follows the module — but a module
*replaced* is a different instrument with a different calibration, and that
needs a deliberate config change rather than silent acceptance.

If NI-DAQmx itself cannot be reached, or the chassis is not present, the
service says so and stops rather than reading nothing in a loop.

---

## What a module fault looks like from the outside

**An open circuit, a short or a missing RTD does not fail the read.** The
module returns a number — the rail — and it is a plausible-looking
temperature. So the driver range-checks:

> Platinum RTDs to IEC 60751 are defined from **−200 to +850 °C**. A reading
> outside that is not a cold or hot sensor; it is an open circuit, a short,
> or no sensor at all.

Such a channel is published with `quality=error` and `value=null` — never
`+1326 °C`. The log line names the channel, its `phys`, the reading and the
likely cause, and it is printed **once** per channel rather than every
second, so a disconnected sensor does not bury everything else in the log.

This is deliberately *not* a threshold in `alarms.yaml`: it is the difference
between a measurement and the absence of one, not between a good value and a
bad one.

| Symptom | Likely cause |
|---|---|
| one RTD channel `error`, reading far outside −200…850 | sensor, wiring or terminal block on that line |
| every channel of one module `error` | the module, or its task |
| the service will not start, naming a serial | a module was replaced — update `devices.yaml` deliberately |
| the service will not start, "cannot reach NI-DAQmx" | driver not installed, or the chassis is off |
| the service will not start, alias not in NI-MAX | aliases were lost — reconfigure them in NI-MAX |

Anything worse than a single channel ends up in the same place: every channel
of the service published as `error`, the state `degraded`, and a backoff
retry — see [How a driver works](index.md#the-loop).

---

## Scaling

RTD channels come back from DAQmx **already in degrees Celsius**, and their
offset and multiplier are the defaults, so scaling is a no-op for them. The
voltage channels are where `value = (raw - offset) * multiplier` actually
does something — `p101` is `(raw − 1.0) × 25.0` bar, and `raw` is archived
alongside so the history can be rescaled if a multiplier turns out to be
wrong.

---

## Handing it back

`close()` releases every task, so LabVIEW can have the chassis back. Like
every device here, it admits one process at a time — see [Starting and
stopping](../operating/running.md#handing-the-hardware-back-to-labview).
