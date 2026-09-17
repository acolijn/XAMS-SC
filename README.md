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

**Current state: milestone 6 complete, milestone 3 partial** — the cDAQ, both
CAEN supplies, the Lake Shore and the UPS are read and logged on the lab PC,
45 channels, with alarms, notifications and the flow integrator running and a
week of LabVIEW history imported alongside. Everything is still read-only; no
setpoint can be written to any instrument. The CAEN supplies, the Lake Shore and the
UPS are still to come, and LabVIEW remains installed as the fallback. See
[Milestones](#milestones).

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

`start` brings up the sinks and the cDAQ service. **`stop` releases all
hardware for LabVIEW** — do that before starting LabVIEW, and stop LabVIEW
before starting these, because every device admits only one process.

For development with no hardware attached, the simulated service publishes
every enabled channel instead:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.devices sim
```

Do not run `sim` and `cdaq` together: they publish the same channel names and
would overwrite each other.

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

83 tests, no hardware and no broker required: the scaling conventions,
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
| 2 | cDAQ read-only | **done** |
| 3 | Channel verification against physical sensors | **partial** — see below |
| 4 | Scaling and history import | **done** |
| 5 | Lake Shore + CAEN monitoring | **done** |
| 6 | UPS, alarms, flow integrator | **done** |
| 7 | Web UI and P&ID mimic | next |
| 8 | Control path | |
| 9 | Procedures | |
| 10 | Production | |

Each has an acceptance criterion in [docs/DESIGN.md §15](docs/DESIGN.md). Do
not start the next before the current one passes.

**Milestone 1 acceptance, met 17 September 2026:** the simulated service
publishes, both sinks store independently, Grafana plots from the provisioned
dashboard, and the install is scripted and reproducible.

**Milestone 2 acceptance, met 17 September 2026:** all 20 connected channels
(6 voltage + 14 RTD) read and logged with LabVIEW stopped, values plausible
(`ttamb` 24.2 °C, cryostat −90 °C, `pmain` 1.495 against LabVIEW's 1.53), and
readings continuous across a service restart to better than 0.7%.

**Milestone 3, partially met 17 September 2026.** Every channel's physical
location is recorded in `channels.yaml`, and the 1xx / 2xx / 3xx series are now
known to mean xenon circulation, detector vessel and bucket, and cooling and
heat exchange respectively (DESIGN.md §3). Readings were checked against the
LabVIEW project and agree.

**The empirical per-sensor check is still outstanding.** Agreement with LabVIEW
confirms we read the same hardware the same way — but it cannot catch a tag
that was already on the wrong channel *in LabVIEW*, because we would inherit
the error and the comparison would agree perfectly. That is the specific
failure §15 says this milestone exists to catch, so it is not yet closed.

**Milestone 4 acceptance, met 17 September 2026.** The column-count census of
§9.6 was run over all 733 log files, one week of history was imported
(6.3 million readings, tagged `src="labview"`), and every channel present in
both systems agrees across the changeover:

```
19 of 19 channels agree, within 0.4%
  pmain    LabVIEW 1.495   XAMS 1.495   -0.03%
  fm101    LabVIEW 7.136   XAMS 7.127   -0.13%
  tt206    LabVIEW -90.317 XAMS -90.460  0.16%
```

Reproduce with `python tools/compare_to_labview.py`.

The largest differences are the cryostat sensors, all drifting the same way by
about 0.2 °C across the 4.8-minute changeover gap — consistent with slow
cooling, not with a scaling error.

**Milestone 5 acceptance, met 17 September 2026.** Both CAEN supplies and the
Lake Shore are read, identity-verified and logged:

```
  2 candidate ports for 21E1:0003: COM4, COM5
  COM4 reports DT1470ET / 19198    -> hv_1
  COM5 reports DT1470ET / 79       -> hv_2
  COM6 reports MODEL335 / 335A12T  -> lakeshore
```

**Unplug test passes.** With the link cut under a running service: readings are
published `quality=error`, the service goes `degraded` and stays alive, and
after two failures it re-enumerates the ports and re-asks every board for its
serial before resuming. A reconnect re-verifies identity rather than merely
reopening the port — a swapped cable during an outage would otherwise resume
under the wrong name (§6.2 rule 6).

Two things the instruments confirmed that the design had recorded from the old
system: the Lake Shore PID is **100, 20, 0**, exactly as §7.3 says, and all
eight HV channels are `DISABLED` with the protection settings in `devices.yaml`
matching the boards.

**Milestone 6 acceptance, met 17 September 2026.**

- **Thresholds** from `alarms.yaml` on `pmain`, `tt104`, `tt302` and three UPS
  channels. Editing one and running `xams-ctl reload` applies it live.
- **Staleness alarms fire.** Proven twice on unplanned faults, not just tests:
  a service that died from a bad config deploy was reported 60 s later, and a
  channel rename produced a staleness alarm for the old name.
- **Flight-recorder dumps** are written on every alarm, holding the ten
  minutes of full-rate data leading up to it.
- **SMS delivered** — one real message through the existing MessageBird
  gateway. Email is not yet configured (no `smtp_host`).
- **The integrator survives a restart** and records gaps rather than
  inventing flow across them. A reset closes a period into `flow_periods`
  with its total, its gaps and who did it, and is written to the audit log.

The UPS is read from its HID **alongside PowerChute**, which keeps doing its
safe-shutdown job. Two other routes were tried and rejected: WMI reports no
battery at all, and `GetSystemPowerStatus` describes the wall socket rather
than the UPS — it would have read "on line power" while running on battery.

**The pressure units remain unknown.** `p101`–`p104` and `pmain` still carry
`unit: TBD`. The comparison validates the *numbers*, not the *labels*: our
`pmain` reproduces LabVIEW's `pmain` to 0.03%, and both would be equally right
if the unit were bar, and equally wrong if it were not. Guessing it is exactly
the failure the `fm101` "SLPM" story records (§4.2).

The verification found one fault: **the `tt202` sensor has failed** (bottom of
the detector vessel). It is `enabled: false` pending replacement, so the cDAQ
carries **19 live channels, not 20**.

---

## Hardware, as found on the lab PC

Verified 17 September 2026.

| Device | Identity |
|---|---|
| cDAQ-9174 | serial `20C5E1C`, modules 9207 / 9216 / 9216 / 9226 |
| | 19 live channels: 6 voltage, 13 RTD (`tt202` failed, awaiting replacement) |
| CAEN DT1470ET | serial **19198**, firmware 1.08 — PMT bottom, PMT top, top screen, bottom screen |
| CAEN DT1470ET | serial **79**, firmware 1.04 — cathode, gate, anode, NaI |
| Lake Shore 335 | `335A12T` on COM6 |
| UPS | APC, `3S2005X18782`, USB HID |

Neither CAEN unit exposes a USB serial number, so they are told apart by asking
each board for its own `BDSNUM` — never by COM port number (§6.2).

Of the cDAQ's 40 available channels, **20 are connected**: 6 voltage inputs on
the 9207, 7 PT1000 on the 9226, and 7 PT100 on the first 9216. The second 9216
is entirely free — eight spare PT100 inputs already wired into the chassis.
