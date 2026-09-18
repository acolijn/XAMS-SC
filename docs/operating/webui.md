# The web interface

<http://127.0.0.1:8000> — the page to bookmark, and where the day-to-day work
happens. It reads the **retained MQTT topics, never the database**, so it keeps
working when PostgreSQL does not, which is exactly when it is needed (§8.1).
Every page refreshes itself; nothing has to be reloaded by hand.

It binds to loopback only. There is no login, and there is deliberately no
remote access — see [Who is acting](#who-is-acting-and-why-it-is-asked) for
what that means for attribution, and §8 for why a tunnel is the answer if
remote viewing is ever wanted.

| Tab | The question it answers |
|---|---|
| **Overview** | Is everything all right — and the controls that change something |
| **Channels** | What is every channel reading, right now |
| **P&I** | Where in the plant is that number |
| **High voltage** | What are both CAEN supplies actually doing |
| **Logs** | What did a service say when it went wrong |

Two links lead off the site: **Grafana ↗** for history and plots, **Manual ↗**
for this documentation, served by the same process so it is present on the lab
PC whether or not the building network is.

---

## The header, on every page

- **config hash and uptime** — the hash identifies which version of
  `config/*.yaml` is running, and is the same hash written into the first line
  of every archive file.
- **acting as** — who you are. See below.
- **the badge on the right** — the whole system in one word. It is the first
  thing to look at and the only thing worth looking at from across the room.

### Who is acting, and why it is asked

Every command is recorded with a name (§10 rule 5). Set it once in the header;
it is kept in a cookie in this browser and attached to everything you send, so
it never has to be retyped before an action.

**It is taken on trust.** There is no login on this interface, so the name is
whatever was typed. Left empty, a command records `webui (unnamed)` rather than
a blank. Weak attribution recorded honestly beats an anonymous change, and it
matches what `xams-ctl ... --by <you>` does from a terminal. Real attribution
needs authentication, which this interface does not have.

---

## Overview

The question it answers is *is everything all right*, and it is built so that a
healthy system is a boring page: no alarm card, no unhealthy channels, green
dots.

**Active alarms** appear at the top, and only when there are any. Each row
gives the channel, the state, the threshold it crossed and the value that
crossed it. *Acknowledged* means the repeating notification has been stopped —
**it does not mean the condition is gone**, and the two must never be read as
the same thing.

**Services** — one row per service, with its state and how long ago it last sent
a heartbeat. A few seconds is normal. `sinks` and `alarms` publish no heartbeat,
so a dash on those two is not a fault. Note what this card cannot do: the alarm
engine cannot report that the alarm engine has stopped. That needs the outside
watchdog of §12.

**UPS & dashboards** — line power or `ON BATTERY`, battery charge and runtime,
plus two housekeeping facts that ride along rather than taking a card of their
own: whether the **nightly backup** ran, and whether the **Grafana dashboards**
are saved to git. Both say `unknown` rather than `ok` when they cannot see;
a check that reports green because it could not reach anything is worse than
no check.

**Lake Shore 335** — both sensor inputs, and per output the setpoint, the
heater percentage and the power in watts. This card also carries the controls
([below](#the-controls)).

**Integrated flow** — the total since the period started, the current rate
underneath it, and the **Reset** button. The rate is shown as well as the total
because the total answers *how much has gone through* and the rate answers *is
it flowing now*; reading the second off a rising number is guesswork.

**Channels not reading OK** — appears only when something is stale or erroring.
This panel being empty is the point of the page.

**Known faults** — channels disabled in `channels.yaml`, listed from the
configuration. `tt202` is here. A disabled channel produces no data at all, so
it cannot show up anywhere driven by measurements; listing it here keeps a known
fault visible without leaving a permanently active alarm that people learn to
ignore.

---

## The controls

Everything that can be changed from this interface is on the Overview page.
There are three things, and nothing else in the system actuates.

### Lake Shore setpoint

Type the temperature in °C, press **Set**, confirm. Output 1 only.

The page validates nothing beyond *is it a number*. The permitted range, the
instrument being connected, the read-back and the audit record all live in the
service that owns the serial port, so a command sent from here gets exactly the
same treatment as one sent any other way. What that treatment is:

1. the value is checked against the range in `channels.yaml` — a channel with
   no range refuses everything, because a range nobody wrote down is not
   permission to write anything;
2. the setpoint is **read back from the instrument** and must match to 0.01 °C
   before the write counts as successful;
3. the result is published on `xams/ack/lakeshore/setpoint` and appended to the
   audit log with the old value, the new value and your name.

A refusal is shown in red as **"Refused — nothing was changed"**, with the
reason. A rejected command is never silently dropped.

### Heater range

**high** or **off**, on output 1, confirmed the same way and audited the same
way. The 335 has four ranges; the two the procedure actually uses are offered.

### Reset the flow integrator

Closes the running period and opens a new one. **Nothing is erased** — the
closed period keeps its total, its gaps and your name in the `flow_periods`
table. That is the whole difference between this and zeroing a counter, and it
is why this button could exist before any instrument was writable.

If the **derived** service is not running, the reset fails and says so; nothing
is closed in that case.

> **`N s of gaps`** on the card means the integrator was not running for part of
> the period, so the total is an underestimate by whatever flowed during them.
> That is recorded rather than papered over: extrapolating the last known rate
> would be inventing data.

Equivalently, from a terminal:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl flow-reset --by <you>
```

### What cannot be changed here

The CAEN high-voltage supplies are **read-only** — there is no code in the
driver that can write a setpoint, and the High voltage page has no controls on
it. The cDAQ has no output module. The UPS is read through its HID and is not
commanded. Named procedures (§10) do not exist yet.

---

## Channels

Every channel, grouped by the service that reads it: value, unit, age, quality,
alarm state and description. This is the page to open when a number somewhere
else looks wrong, because it shows *when* the number was read as well as what it
was.

**A stale channel shows a dash, never its last number.** A frozen value
displayed as though it were live invites a decision based on a reading that
stopped being true an hour ago — that is the failure this kind of page exists to
avoid.

Quality is the driver's own verdict on the reading:

| quality | means |
|---|---|
| `ok` | a real reading from a verified instrument |
| `error` | the instrument answered, but not with a number — open circuit, out of range, link down. **No value is published** |
| `unverified` | read from a device that has not proved its identity; never written to |
| `stale` | nothing new has arrived within the staleness window |

---

## P&I

The plant drawing with a live value in each instrument bubble — the page that
answers *where is that sensor*, which a table of tag names cannot.

Built from the original drawing by `tools/build_mimic.py`: rotated to
landscape, frame and title block removed, recoloured for the dark interface.

**A stale channel greys out and shows a dash**, exactly as on the Channels page
and for the same reason.

The tags in the SVG are checked against `channels.yaml` when the service starts,
and drift is reported **in both directions** — a bubble with no channel behind
it, and a channel that appears nowhere on the drawing.

---

## High voltage

Both CAEN supplies, eight channels each: `VMON`, `IMON`, the state, and the
board's own protection settings. Read-only.

The **Status** column comes from the board's own `STAT` word, **not** from the
voltage:

| shown | meaning |
|---|---|
| `ON` | output enabled and putting volts out |
| `enabled` | output enabled, sitting at **zero volts** — still live |
| `off` | output disabled (STAT bit 10) |
| `TRIP`, `INTERLOCK`, … | a fault flag, shown in red in place of the state |
| `no reading` | the status word could not be read |

**`enabled` is not `off`.** The supplies have a physical enable per channel, and
a channel can be switched on with its setpoint at zero — one turn of a knob from
putting volts on an electrode. This page said "off" for exactly that case until
17 September 2026, because it inferred the state from `VMON > 1` instead of
asking the board. Treat `enabled` as live.

The full bitmask is stored as `hv_*_stat`, so the history carries `TRIP`,
`INTERLOCK`, `OVER_CURRENT` and `OVER_TEMP` too, whether or not anything alarms
on them yet.

---

## Logs

The last lines of any service log, without going to the filesystem. The same
files are in `logs\<service>.log` and rotate there.

This is the second place to look when a service is unhappy; the first is
`xams-ctl status`. What the messages mean is in
[Troubleshooting](troubleshooting.md).

---

## When the interface itself is the problem

If the page does not load at all, the web service is not running:
`xams-ctl status`, then `xams-ctl start`.

If the page loads but everything is dashed, the **broker** is the likely
culprit, not the web service — it renders from retained MQTT topics. See
[Troubleshooting](troubleshooting.md#services-and-the-bus).
