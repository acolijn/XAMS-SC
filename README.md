# XAMS Slow Control

Monitoring and convenience control of the XAMS slow-control hardware: one NI
cDAQ-9174 chassis, two CAEN DT1470ET high-voltage supplies, a Lake Shore 335
and a UPS. Storage, plotting, alarming and a small web UI.

**This document is for someone reinstalling the system from scratch.** The
design and the reasoning behind it are in [docs/DESIGN.md](docs/DESIGN.md);
day-to-day operation will be in `OPERATIONS.md` as it is written.

Current state: **milestone 1 — skeleton.** Simulation only, no hardware
drivers yet. See [Milestones](#milestones).

---

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

---

## Install

### 1. Python

Python 3.11 or newer. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Add `.[hardware]` for the device drivers (`nidaqmx`, `pyserial`, `lakeshore`).
Those need the NI runtime and are not required for simulation.

Check that the configuration is valid:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl check
```

### 2. Mosquitto (the broker)

Download from <https://mosquitto.org/download/> and install. Then **bind it to
loopback** — this matters more than it looks:

`C:\Program Files\mosquitto\mosquitto.conf`

```
listener 1883 127.0.0.1
allow_anonymous true
```

Commands travel over MQTT (`xams/cmd/#`), so anything that can reach the
broker can set a high voltage. A broker listening on all interfaces is an
unauthenticated control interface on the building network. `127.0.0.1` is the
only accepted bind address without a recorded decision to the contrary (§8).

```powershell
net start mosquitto
```

### 3. PostgreSQL

Install from <https://www.postgresql.org/download/windows/>, then:

```powershell
psql -U postgres -c "CREATE DATABASE xams;"
psql -U postgres -c "CREATE USER xams WITH PASSWORD 'choose-one';"
psql -U postgres -d xams -f sql/schema.sql
psql -U postgres -d xams -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO xams;"
```

In `postgresql.conf` set `listen_addresses = 'localhost'`.

Copy the credentials into place — this file is in `.gitignore` and must never
be committed:

```powershell
copy config\secrets.example.yaml config\secrets.yaml
notepad config\secrets.yaml
```

### 4. Grafana

Install from <https://grafana.com/grafana/download?platform=windows>. In
`conf\custom.ini`:

```ini
[server]
http_addr = 127.0.0.1
http_port = 3000
```

Point Grafana at the provisioning in this repository by copying or symlinking
`grafana/provisioning/` into Grafana's `conf/provisioning/`, and set the
datasource password in the environment before starting the service:

```powershell
setx XAMS_PG_PASSWORD "the-password-you-chose"
```

Dashboards live in `grafana/dashboards/*.json`, in git. Edit one in the
Grafana UI, export it to JSON, commit it — and it appears on any other machine
at the next `git pull`. Without this, dashboards live only in Grafana's
internal database and are lost when it is.

---

## Run it

Start the broker first, then:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl start
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl status
```

`start` brings up the sinks and the simulated device service; `stop` shuts
them down and **releases all hardware for LabVIEW**, which matters while
LabVIEW is still the fallback — every device admits only one process.

To run one service in the foreground and watch it:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.devices sim
.\.venv\Scripts\python.exe -m xams_sc.sinks
```

To see everything crossing the bus, with no UI and no database involved:

```powershell
mosquitto_sub -t "xams/#" -v
```

Archived data lands in `data/raw/<UTC date>.jsonl`, one JSON object per line,
with a header line recording the config hash that produced it.

---

## Configuration

Everything the system knows about the hardware is in `config/`, in git.
**Adding a sensor touches no Python.**

| File | What it defines | How it is changed |
|---|---|---|
| `channels.yaml` | every channel: address, scaling, unit, write range | edit, commit, `xams-ctl reload` |
| `devices.yaml` | how each instrument is found and identified | edit, commit, `xams-ctl reload` |
| `alarms.yaml` | thresholds and severity routing | edit, commit, `xams-ctl reload` |
| `recipients.yaml` | who gets notified | the web UI, or by hand — no restart |
| `secrets.yaml` | credentials | by hand; **never committed** |

Two conventions that must not be changed:

- **Scaling is `value = (raw - offset) * multiplier`**, in that order. It
  matches LabVIEW exactly. Changing it breaks every historical comparison.
- **HV values are stored signed.** The supplies report unsigned magnitudes
  with polarity separate; the sign is applied in software. Changing this
  later silently inverts history.

`xams-ctl check` validates the configuration and prints what it defines,
without starting anything.

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

No hardware and no broker required. The suite covers the scaling conventions,
configuration validation, and the chain from a simulated read to a record on
disk.

Three of the tests exist because the system being replaced got these wrong:
the order of operations in the scaling formula, the sign on HV channels, and
the minutes-versus-seconds trap in the flow integrator.

---

## Milestones

| # | Milestone | State |
|---|---|---|
| 1 | Skeleton: config, bus, sinks, simulation, install | **in progress** |
| 2 | cDAQ read-only | |
| 3 | Channel verification against physical sensors | |
| 4 | Scaling and history import | |
| 5 | Lake Shore + CAEN monitoring | |
| 6 | UPS, alarms, flow integrator | |
| 7 | Web UI and P&ID mimic | |
| 8 | Control path | |
| 9 | Procedures | |
| 10 | Production | |

Each has an acceptance criterion in [docs/DESIGN.md §15](docs/DESIGN.md). Do
not start the next before the current one passes.

---

## Hardware, as found on the lab PC

Verified 17 September 2026.

| Device | Identity |
|---|---|
| cDAQ-9174 | serial `20C5E1C`, modules 9207 / 9216 / 9216 / 9226 |
| CAEN DT1470ET | serial **19198**, firmware 1.08 — PMT bottom, PMT top, top screen, bottom screen |
| CAEN DT1470ET | serial **79**, firmware 1.04 — cathode, gate, anode, NaI |
| Lake Shore 335 | `335A12T` |
| UPS | APC, `3S2005X18782` |

Neither CAEN unit exposes a USB serial number, so they are told apart by
asking each board for its own `BDSNUM` — never by COM port number (§6.2).
