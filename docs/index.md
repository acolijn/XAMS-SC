# XAMS Slow Control

Monitoring and convenience control of the XAMS slow-control hardware: one NI
cDAQ-9174 chassis, two CAEN DT1470ET high-voltage supplies, a Lake Shore 335
and a UPS. Storage, plotting, alarming and a small web UI.

**Current state: milestone 7 largely complete, milestone 8 in progress — the
Lake Shore and the HV write paths are built — milestone 3 partial.** The cDAQ,
both CAEN supplies, the Lake Shore and the UPS are read and logged on the lab PC — 45 channels — with alarms, SMS and email
notification, the flow integrator, a week of imported LabVIEW history, a web UI
and a live P&I mimic.

**The system reads everything and controls what needs controlling.** Two
instruments actuate:

- **Lake Shore 335** — setpoint and heater range, output 1.
- **Both CAEN DT1470ET supplies** — `VSET` per channel, and energising a
  channel on or off.

Every write is validated against `channels.yaml`, **read back from the
instrument** before it counts as successful, acknowledged on `xams/ack/#` and
appended to the audit log with the old value, the new value and who did it. A
channel with no range written down refuses everything: a range nobody recorded
is not permission to put volts on an electrode.

**Two gates stay in your hands, and no command can reach either.** A board must
be switched to `REMOTE` at its front panel, and a channel must be enabled at its
front panel, before software can do anything at all. Enabling a channel is a
hand operation on purpose — which is what makes the invariant of §10a matter:
**a channel that is not enabled holds `VSET` 0**, so flipping its switch always
brings it up at zero volts and raising voltage is always a deliberate, audited,
software step.

**Protection is never written.** `MAXV`, `RUP`, `RDW`, `TRIP` and `ISET` live on
the instrument; the software displays them and alarms when a board disagrees
with what is recorded, and no code exists that can change them. Nothing actuates
on startup or restart, and there is no automatic actuation anywhere — the
decided heater cut on a `pmain` hihi (§10) is specified but not built, and
`KILL VOLTAGE` remains open. Named procedures are milestone 9.

**This is not a protection system.** Interlocks belong in hardware, wired from a
real gauge trip (§10 rule 1).

LabVIEW is installed and recoverable but no longer running. See
[Project status](status.md).

## Where to start

| | |
|---|---|
| I need to run it, or it is misbehaving | [Operating](operating/index.md) |
| I am reinstalling from scratch | [Installation](install.md) |
| I want to understand how it works | [Architecture](software/architecture.md) |
| I am adding a sensor | [Configuration files](software/config.md) |
| I am writing a driver | [How a driver works](drivers/index.md) |
| I want to know why it is built this way | [Design specification](DESIGN.md) |

## What it is

Every device service reads its instrument and publishes to a local MQTT
broker. Nothing writes to a database directly. Storage, alarming and the UI
are all subscribers, so any of them can be added or replaced without touching
a driver.

```
  cdaq ─┐
  caen ─┤                    ┌─► jsonl_writer ──► data/raw/*.jsonl   the archive
  ls335 ┼─► MQTT (mosquitto) ┼─► pg_writer ─────► PostgreSQL ──► Grafana
  ups  ─┘                    ├─► alarm engine ──► SMS / email
                             └─► web UI (FastAPI, 127.0.0.1:8000)
```

**The files are the truth; the database is an index over them.** If PostgreSQL
is lost, replay the archive into a new one. Nothing in any driver changes.
