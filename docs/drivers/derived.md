# Derived channels

Channels computed from other channels rather than read from an instrument —
the flow integrator above all, whose running total survives restarts and is
reset from the web UI with an audit record.

There are two quite different mechanisms here, and it is worth keeping them
apart: a **transform**, which is arithmetic applied inside a driver, and the
**integrator**, which is a service with memory.

---

## Transforms: arithmetic inside a driver

Where a channel is a function of one other channel and the relationship is
*not* linear, it is declared in `channels.yaml` and computed by whichever
driver reads the source:

```yaml
  - name: ls_heater_1_w
    device: lakeshore
    kind: power
    unit: W
    derive: {from: ls_heater_1, transform: heater_power,
             full_scale_v: 45.4, resistance_ohm: 31.2}
```

`transform` names an entry in `scaling.TRANSFORMS`. Today there is one,
`heater_power`, because heater wattage goes as the **square** of the
percentage and so cannot be expressed as an offset and a multiplier.

Two rules hold for every transform, and both are easy to get wrong:

- **Same timestamp as the source.** A derived value and the reading it came
  from must line up exactly, or a plot of the two shows a phantom lag.
- **Inherit the source's quality.** If the heater percentage could not be
  read, its wattage is not zero — it is unknown.

Anything expressible as `(raw - offset) * multiplier` should be an offset and
a multiplier instead. A transform is for the cases that genuinely are not.

---

## The flow integrator

A service of its own — `derived` — that subscribes to `fm101` and publishes
`fm101_total`: the mass that has passed through since the period started.

```
fm101        g/min     what the meter reads
fm101_total  g         what has gone through
```

It is a **bus consumer, not a driver**: it owns no instrument, reads no port,
and would work equally well on another machine. It runs as a service so that
it has a heartbeat and a state like everything else.

### This is the only stateful component in the system

Everything else is restartable without consequence. An integrator is not — a
running total cannot be rediscovered by asking the hardware. That raises
three requirements, and none of them may be skipped.

**1. It survives a restart.** After every publish the accumulator is written
to `data/fm101_total.json` with the timestamp of the last sample processed,
and read back at startup:

```json
{"total_g": 5437.80, "gaps_s": 0.0,
 "last_sample_t": "2026-09-18T19:47:09.220Z",
 "period_start": "2026-09-18T07:05:50.609Z"}
```

A Windows update that reset the total to zero would make the whole feature
useless.

**2. Gaps are recorded, never invented.** If the service was down for two
hours, what flowed during those two hours is **unknown**. Extrapolating the
last value is tempting and wrong, so such intervals are excluded from the
total and their duration accumulated in `gaps_s` instead. The total then
carries the evidence that it is an underestimate, rather than that having to
be reconstructed from service logs months later.

Samples whose quality is not `ok` are likewise not integrated.

**3. Reset closes a period; it does not erase.** See below.

### Units, and the factor-60 error

`fm101` is a mass flow in **grams per minute**. The integral is
`sum(flow × dt)` with `dt` in **minutes**, giving grams.

A `dt` in seconds produces a result sixty times too large, and — this is the
problem — an entirely plausible-looking one. The conversion therefore lives
in exactly one place, `scaling.integrate_step`, rather than being written out
wherever it is needed.

---

## Reset

From the [web interface](../operating/webui.md), or from the command line:

```powershell
xams-ctl flow-reset --by AP
```

**Nothing is zeroed.** The running period is closed and a new one opened, so
the history of how much passed through during each period survives:

| start | stop | total_g | gaps_s | reset_by |
|---|---|---|---|---|
| 2026-08-01 09:14 | 2026-09-16 11:02 | 4213.8 | 0.0 | apc |
| 2026-09-16 11:02 | — | 118.4 | 0.0 | — |

The closed period is published on `xams/flow/period` and stored by the [flow
sink](../software/storage.md) into `flow_periods`. `gaps_s` is stored **with**
the total, always, because a period that ran through an outage is an
underestimate and the number must carry that.

**A reset is a control action**, so it is acknowledged and audited like any
other — even though it writes nothing to hardware. What it changes is the
record, and a number somebody quietly discarded is exactly what §7.5 exists
to prevent.

The UI asks the derived service over the bus rather than reaching into the
integrator itself: the integrator runs in another process and is still
accumulating, so a web request that reached around it could not be audited
and would race the service. A reset that nothing acknowledged is reported as
a **failure**, not as success.

---

## Adding a derived channel

For a transform: add the channel to `channels.yaml` with a `derive:` block,
add the function to `scaling.TRANSFORMS` if it is a new shape, and make sure
the driver that reads the source computes it with the source's timestamp and
quality. No new service, no new subscription.

For anything with memory — a second integrator, a duty-cycle counter — read
the three requirements above first. Persistence, gap handling and reset
semantics are the whole of the work; the arithmetic is the easy part.
