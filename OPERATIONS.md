# XAMS Slow Control — Operations

**How to run the system, and what to do when it misbehaves.** Not architecture
— that is [docs/DESIGN.md](docs/DESIGN.md). Not installation from scratch —
that is [README.md](README.md).

This document **grows with each milestone** (§14). It is deliberately written
during construction rather than afterwards: written at the end it would record
what the author remembers rather than what a reader does not understand.

> **State: milestone 1.** Simulation only. No hardware drivers, no alarms, no
> control. Sections marked *(not yet)* are placeholders so the shape of the
> document is visible and it is obvious what is missing.

**The acceptance test for this document** (§14): a colleague, using this file
alone, can stop the system, add a channel and start it again — without asking
anyone. If that fails, the document is unfinished however thorough it looks.

---

## Every day

Open a terminal in `C:\Users\localadmin\Documents\XAMS-SC` and:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl status
```

This shows each service, whether it is running, and how long ago it last sent
a heartbeat. Heartbeat ages come from the **retained MQTT topics, not the
database** — the status view works even when PostgreSQL does not, which is
precisely when it is needed.

Grafana: <http://127.0.0.1:3000>, the **XAMS Overview** dashboard.

What "healthy" looks like: every service running, every heartbeat a few
seconds old, and the *"Channels not reading OK"* panel **empty**. That panel
being empty is the point — a frozen plausible value is worse than a gap.

---

## Starting and stopping

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl start
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl stop
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl restart
```

Services start in a fixed order and stop in reverse, so the sinks outlive the
producers and the last measurements are archived rather than dropped.

### Handing the hardware back to LabVIEW

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl stop
```

**This matters while LabVIEW is still the fallback.** Every device admits only
one process: the cDAQ, each CAEN supply and the Lake Shore can each be held by
exactly one program. If our services are running, LabVIEW cannot start, and
vice versa.

This is also why **auto-start stays off** until LabVIEW is retired (§12). A
service that starts after an overnight Windows update would claim the hardware
and lock LabVIEW out with nobody present to notice.

### Running one service in the foreground

To watch a service directly — this is how you debug one:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.devices sim
.\.venv\Scripts\python.exe -m xams_sc.sinks
```

Ctrl-C stops it cleanly. Logs go to `logs\<service>.log` either way.

---

## Looking at the data

**Live, with no UI and no database involved** — the first thing to try when
something looks wrong:

```powershell
& "$env:ProgramFiles\mosquitto\mosquitto_sub.exe" -t "xams/#" -v
```

Every measurement, every heartbeat, every state change, as it happens.

**The archive** is `data\raw\<UTC date>.jsonl`, one JSON object per line. The
first line of each file records the config hash that produced it, so a file is
self-describing:

```powershell
Get-Content data\raw\2026-09-17.jsonl -TotalCount 1
Get-Content data\raw\2026-09-17.jsonl -Tail 5
```

**These files are the truth. The database is an index over them.** If
PostgreSQL is lost, replay the archive into a new one; nothing in any driver
changes.

---

## Adding a channel

Adding a sensor touches **no Python**.

1. Add an entry to `config\channels.yaml`. Copy the nearest existing channel
   and change what differs. Required: `name`, `device`, `phys`, `kind`, `unit`.
2. Validate it, without starting anything:

   ```powershell
   .\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl check
   ```

   This refuses duplicate names, unknown devices, unknown kinds, an `rtd` kind
   with no `rtd:` block, and two channels on one physical input.
3. Commit it. Configuration is data, and the config hash in every archive file
   records which version produced which data.
4. Apply it without a restart and without a gap in the data:

   ```powershell
   .\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl reload
   ```

**The name is permanent.** It is the identity of the measurement in the MQTT
topic, the archive, the database and the UI. Renaming it later breaks the
history. Use the P&ID tag, lowercased — `TT201` becomes `tt201` — and put the
original casing in `legacy:`.

**Do not guess a unit.** Five channels currently carry `unit: TBD` for exactly
this reason (`p101`–`p104`, `pmain`). A wrong label propagates into every plot,
every threshold, and eventually into a paper. `TBD` is the honest state.

### Changing a calibration

Same procedure — edit `offset` / `multiplier`, check, commit, reload.

Scaling is `value = (raw - offset) * multiplier`, in that order, matching
LabVIEW exactly. **Do not change the convention**: historical comparisons break
silently if you do.

---

## Troubleshooting

*Every time something breaks and is fixed, it goes in here. After a year this
is the most valuable section in the repository.*

### "running scripts is disabled on this system"

Running `tools\setup_services.ps1` fails with a `PSSecurityException`.

The execution policy defaults to `Restricted`. In the **elevated** window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\tools\setup_services.ps1
```

`-Scope Process` dies with that window. Do not set `LocalMachine` — it is a
permanent change to the machine's security posture to run one script once.

### A service refuses to start: "another sim service is already running"

A stale lock file in `logs\`, usually after a hard kill.

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl status
```

If nothing is actually running, delete the lock and start again:

```powershell
Remove-Item logs\*.lock
```

The lock exists because two copies of a driver is not untidiness — it is two
processes fighting over one instrument.

### "broker unreachable: still reading hardware and buffering to memory"

Mosquitto is down. Acquisition continues and measurements are buffered, but
**nothing is being stored**. The message repeats every 30 seconds.

```powershell
Get-Service mosquitto
Restart-Service mosquitto
```

A short outage costs nothing — the buffer republishes in order when the broker
returns. A long one costs a visible gap in the data, which is logged as a loss
count rather than passing silently.

### A service will not start: configuration is invalid

`xams-ctl check` prints the reason and the offending channel. Validation is
strict and deliberately fails at startup rather than at the first bad read: a
typo is cheap to find now and expensive to find in six months of history.

### Nothing in Grafana, but the services look healthy

Check in this order — it separates the three things that can be wrong:

1. Is data crossing the bus at all? `mosquitto_sub -t "xams/#" -v`
2. Is it reaching the archive? `Get-Content data\raw\<today>.jsonl -Tail 5`
3. Is it reaching the database? `SELECT count(*) FROM meas;`

If (1) and (2) work and (3) does not, it is the PostgreSQL writer — look in
`logs\sinks.log`. A database failure never blocks the bus or the services, so
everything else will look perfectly healthy while nothing is being indexed.

### Grafana: every panel says "No data" with a red error marker

Hover the red marker. If it reads **"You do not currently have a default
database configured"**, the datasource has the database name in the legacy
place only.

The PostgreSQL datasource reads it from `jsonData.database`; the older
top-level `database:` field is still honoured by the **backend**. With only the
top-level field set the two halves of Grafana disagree, and every server-side
check passes while nothing renders:

| | Reads `database` from | Says |
|---|---|---|
| Backend | top-level `database:` | health "Database Connection OK", queries return data |
| Query editor | `jsonData.database` | "no default database configured", refuses to query |

`grafana/provisioning/datasources/xams.yaml` sets **both**. Do not remove
either. After changing it, restart Grafana — provisioned datasources are only
re-read at startup:

```powershell
Restart-Service Grafana
```

**The diagnostic worth remembering.** When a panel shows nothing, first
establish whether the query *returns* nothing or is *never issued*:

```powershell
Get-Content "$env:ProgramFiles\GrafanaLabs\grafana\data\log\grafana.log" -Tail 300 |
    Select-String queryData
```

An entry with `referer=http://...` is a real panel query. No entry at all means
the frontend never asked, which rules out the database, the SQL, the time range
and the credentials in one step — and points at the datasource or the dashboard
document instead.

### Grafana: a dashboard edit does not take effect

Provisioned dashboards are re-read from `grafana/dashboards/*.json`. If a change
to the file does not appear, restart Grafana.

`allowUiUpdates` is **false** so the file always wins. To change a dashboard:
edit it in the UI until it looks right, **export the JSON**, commit it to
`grafana/dashboards/`, and let provisioning apply it. A dashboard that exists
only in Grafana's own database is lost when that database is.

### psql reports "NOTICE: relation already exists, skipping"

Not an error. `sql/schema.sql` uses `CREATE TABLE IF NOT EXISTS` so that
re-running it is safe, and psql writes NOTICE to stderr.

This matters when scripting: in PowerShell 5.1, `2>&1` on a native executable
wraps each stderr line in an ErrorRecord, and under
`$ErrorActionPreference = "Stop"` that becomes a terminating error even though
the program succeeded. `tools/setup_services.ps1` therefore sends stderr to a
file and judges psql by its exit code alone.

### A sink stops storing while everything looks healthy

Check the archive and the database **separately** — they are independent
consumers, and one can fail silently while the other works:

```powershell
Get-Content dataaw\<today>.jsonl -Tail 3
```
```sql
SELECT count(*), max(t) FROM meas;
```

If the database is filling and the JSONL archive is not, the archive is the one
that matters: the files are the truth and the database is only an index over
them (§9.3).

### The whole PC was rebooted

The three infrastructure services (mosquitto, PostgreSQL, Grafana) start
automatically. **The XAMS services do not** — that is deliberate while
LabVIEW is the fallback. Start them by hand with `xams-ctl start`.

---

## What each alarm means *(not yet — milestone 6)*

This section is the reason `OPERATIONS.md` exists, and it is currently empty.

The thresholds of the present LabVIEW system exist in the VIs, but **the
response to each exists only in people's heads**. A threshold without a
prescribed action is half the information. Reproducing that omission would
waste the migration.

When `config\alarms.yaml` is filled from the LabVIEW *Error and Alarm* tab,
every threshold gets an entry here: what it means, and what to do about it.

---

## Control and procedures *(not yet — milestones 8 and 9)*

No setpoint can be written by this system yet. High voltage, the Lake Shore
setpoint and the named procedures arrive at milestone 8, and only after the
open decision in §10 is made — whether the LabVIEW heater shut-off and
`KILL VOLTAGE` behaviours move into hardware or are reproduced in software.

Until then: **the instruments are operated from their own front panels.**

---

## Rolling back to LabVIEW

The fallback, at any point before milestone 10:

1. `xams-ctl stop` — releases the cDAQ, both CAEN supplies and the Lake Shore.
2. Confirm nothing is holding them: `xams-ctl status` shows all services
   stopped.
3. Start LabVIEW as before.

LabVIEW stays installed until milestone 10 and is not removed when it is
retired. Nothing in this system modifies the LabVIEW installation, its VIs or
its CSV output.

---

## Reference

| Thing | Where |
|---|---|
| Configuration | `config\*.yaml` — in git, except `secrets.yaml` |
| Credentials | `config\secrets.yaml` — **never committed** |
| Archive | `data\raw\*.jsonl` |
| Logs | `logs\<service>.log`, rotating |
| Broker | `127.0.0.1:1883` |
| PostgreSQL | `127.0.0.1:5432`, database `xams` |
| Grafana | <http://127.0.0.1:3000> |
| Web UI | `127.0.0.1:8000` *(not yet — milestone 7)* |

**Everything binds to loopback, deliberately.** Control commands travel over
MQTT, so anything that can reach the broker can set a high voltage. If remote
viewing is wanted, the answer is an SSH tunnel or a read-only mirror — never
opening the control port (§8).
