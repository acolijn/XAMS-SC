# XAMS Slow Control

Monitoring and convenience control of the XAMS slow-control hardware: one NI
cDAQ-9174 chassis, two CAEN DT1470ET high-voltage supplies, a Lake Shore 335
and a UPS. Storage, plotting, alarming and a small web UI.

**Current state: milestone 7 largely complete, milestone 8 started, milestone 3
partial.** The cDAQ, both CAEN supplies, the Lake Shore and the UPS are read and
logged on the lab PC — 45 channels — with alarms, SMS notification, the flow
integrator, a week of imported LabVIEW history, a web UI and a live P&I mimic.

**The system is read-only with one exception: the Lake Shore heater.** Its
setpoint and heater range can be written, on output 1 only — from the web UI or
by publishing on the bus. That is the first write path (§10), added once §10's
open decision was resolved on 18 September 2026. Every write is validated
against the range in `channels.yaml`, **read back from the instrument** before
it counts as successful, acknowledged on `xams/ack/#` and appended to the audit
log. A value with no range written down is refused, not permitted.

**Nothing else actuates.** The cDAQ chassis has no output module, so there is no
control path to have; the CAEN high-voltage supplies and the UPS are read only.
There is no automatic actuation anywhere yet: the decided heater cut on a
`pmain` hihi is specified but **not built**, and `KILL VOLTAGE` remains an open
question. Procedures (named sequences) are still milestone 9.

**This is not a protection system.** Interlocks belong in hardware and limits
belong in the instrument, which the software verifies and alarms on but does not
write (§10 rules 1–2).

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
