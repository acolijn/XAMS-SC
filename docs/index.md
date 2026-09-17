# XAMS Slow Control

Monitoring and convenience control of the XAMS slow-control hardware: one NI
cDAQ-9174 chassis, two CAEN DT1470ET high-voltage supplies, a Lake Shore 335
and a UPS. Storage, plotting, alarming and a small web UI.

**Current state: milestone 7 largely complete, milestone 3 partial.** The
cDAQ, both CAEN supplies, the Lake Shore and the UPS are read and logged on the
lab PC — 45 channels — with alarms, SMS notification, the flow integrator, a
week of imported LabVIEW history, a web UI and a live P&I mimic.

**Everything is read-only.** No setpoint can be written to any instrument; that
is milestone 8, and it needs §10's open decision resolved first. LabVIEW is
installed and recoverable but no longer running. See [Project status](status.md).

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
