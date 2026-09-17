# XAMS Slow Control

Monitoring and convenience control of the XAMS slow-control hardware: one NI
cDAQ-9174 chassis, two CAEN DT1470ET high-voltage supplies, a Lake Shore 335
and a UPS. Storage, plotting, alarming and a small web UI.

**This document is for someone reinstalling the system from scratch.**

| | |
|---|---|
| How to *run* it, and what to do when it breaks | [OPERATIONS.md](OPERATIONS.md) |
| Why it is built this way, and what to build next | [docs/DESIGN.md](docs/DESIGN.md) |
| What was decided and why | [docs/OPTIONS.md](docs/OPTIONS.md) |

**Current state: milestone 1 complete** — the full stack runs in simulation on
the lab PC. No hardware drivers yet; LabVIEW is untouched and still the system
of record. See [Milestones](#milestones).

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

Add `.[hardware]` for the device drivers (`nidaqmx`, `pyserial`, `lakeshore`)
from milestone 2 onward. Those need the NI runtime and are not required for
simulation.

Check that the configuration is valid — this starts nothing:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl check
```

### 2. Mosquitto, PostgreSQL and Grafana

One script does all three, in an **elevated** PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\tools\setup_services.ps1
```

It asks for two passwords (the `postgres` superuser, and one for the `xams`
role it creates), then downloads and installs the three services, binds all of
them to loopback, creates the database, applies `sql/schema.sql`, writes
`config/secrets.yaml`, and verifies that every port is listening on `127.0.0.1`
**and nowhere else**.

It is idempotent — if a step fails, fix the cause and run it again. Roughly
870 MB of downloads, staged in `tools/installers/` (gitignored) so a re-run
does not fetch them twice.

**Why the loopback binding matters.** Control commands travel over MQTT
(`xams/cmd/#`), so anything that can reach the broker can set a high voltage. A
broker listening on `0.0.0.0` — the default in most tutorials — is an
unauthenticated control interface on the building network. If remote viewing is
ever wanted, the answer is an SSH tunnel or a read-only mirror, never opening
the port (§8).

**What the script does not do:** install the XAMS services themselves as
Windows services. Auto-start stays off while LabVIEW is the fallback, because
every device admits only one process and a service starting at boot would lock
LabVIEW out (§12).

### 3. Grafana first login

<http://127.0.0.1:3000>, `admin` / `admin`, and change the password when asked.

The **XAMS** datasource and the **XAMS Overview** dashboard are provisioned
from this repository. Dashboards live in `grafana/dashboards/*.json`, in git,
and the file is authoritative: edit a dashboard in the UI to get it right, then
export the JSON and commit it. A dashboard that exists only in Grafana's own
database is lost when that database is.

---

## Run it

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl start
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl status
```

`start` brings up the sinks and the simulated device service. **`stop` releases
all hardware for LabVIEW.**

Then open <http://127.0.0.1:3000> → **XAMS Overview**.

To watch everything crossing the bus, with no UI and no database involved:

```powershell
& "$env:ProgramFiles\mosquitto\mosquitto_sub.exe" -t "xams/#" -v
```

Full day-to-day instructions, including what to do when something breaks, are
in [OPERATIONS.md](OPERATIONS.md).

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

Three conventions that must not be changed:

- **Scaling is `value = (raw - offset) * multiplier`**, in that order. It
  matches LabVIEW exactly. Changing it breaks every historical comparison.
- **HV values are stored signed.** The supplies report unsigned magnitudes with
  polarity separate; the sign is applied in software. Changing this later
  silently inverts history.
- **Channel names are permanent.** They are the identity of a measurement in
  the MQTT topic, the archive, the database and the UI.

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

53 tests, no hardware and no broker required: the scaling conventions,
configuration validation, bus fan-out, and the chain from a simulated read to a
record on disk.

Several exist because something already went wrong once — the order of
operations in the scaling formula, the sign on HV channels, the
minutes-versus-seconds trap in the flow integrator, and two consumers
subscribing to one MQTT topic.

---

## Milestones

| # | Milestone | State |
|---|---|---|
| 1 | Skeleton: config, bus, sinks, simulation, install | **done** |
| 2 | cDAQ read-only | next |
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

**Milestone 1 acceptance, met 17 September 2026:** the simulated service
publishes, both sinks store independently, Grafana plots from the provisioned
dashboard, and the install is scripted and reproducible.

---

## Hardware, as found on the lab PC

Verified 17 September 2026.

| Device | Identity |
|---|---|
| cDAQ-9174 | serial `20C5E1C`, modules 9207 / 9216 / 9216 / 9226 |
| CAEN DT1470ET | serial **19198**, firmware 1.08 — PMT bottom, PMT top, top screen, bottom screen |
| CAEN DT1470ET | serial **79**, firmware 1.04 — cathode, gate, anode, NaI |
| Lake Shore 335 | `335A12T` on COM6 |
| UPS | APC, `3S2005X18782`, USB HID |

Neither CAEN unit exposes a USB serial number, so they are told apart by asking
each board for its own `BDSNUM` — never by COM port number (§6.2).

Of the cDAQ's 40 available channels, **20 are connected**: 6 voltage inputs on
the 9207, 7 PT1000 on the 9226, and 7 PT100 on the first 9216. The second 9216
is entirely free — eight spare PT100 inputs already wired into the chassis.
