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

Web UI: <http://127.0.0.1:8000>, the page to bookmark.
Grafana: <http://127.0.0.1:3000>, the **XAMS Overview** dashboard.

What "healthy" looks like: every service running, every heartbeat a few
seconds old, and the *"Channels not reading OK"* panel **empty**. That panel
being empty is the point — a frozen plausible value is worse than a gap.

---

## Resetting the flow integrator

The **Integrated flow** card on <http://127.0.0.1:8000> shows the total since
the period started, the current rate underneath it, and a **Reset** button.
Type your name in the box and click it. Equivalently, from a terminal:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl flow-reset --by <you>
```

**Nothing is erased.** The running period is closed into the `flow_periods`
table with its total, its gaps and your name, and a new one is opened. The
history of how much passed through during each period survives — unlike a
counter somebody zeroed, which is simply gone.

The name is taken on trust: there is no login on this UI, and leaving the box
empty records `webui (unnamed)` rather than a blank. Weak attribution recorded
honestly beats an anonymous change.

If the **derived** service is not running, the reset fails and says so.
Nothing is closed in that case — check `xams-ctl status` and the button again.

> **`N s of gaps`** on that card means the integrator was not running for part
> of the period, so the total is an underestimate by whatever flowed during
> them. That is recorded rather than papered over; extrapolating the last
> known rate would be inventing data.

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

### Editing a dashboard, the right way round

**Grafana owns the dashboard. `grafana/dashboards-archive/` is the copy git
tracks.** Edit in one place — the Grafana UI — and copy it to the other. The
loop:

```powershell
# 1. edit in the Grafana UI, and save there as normal
# 2. pull it into git
.\.venv\Scripts\python.exe tools\save_dashboard.py --save --password <pw>
# 3. commit
git add grafana/dashboards-archive
git commit -m "grafana: <what changed>"
```

Step 2 is the one that matters: **a dashboard that exists only in Grafana's
own database is lost when that database is.** Nothing runs it for you.

| command | direction | when |
|---|---|---|
| `--save` *(the default)* | Grafana → git | after **every** editing session |
| `--check` | compare only | before `--load`, before a reinstall, before a `git pull` |
| `--load` | git → Grafana | **only** to restore: fresh install, or Grafana's database lost |

**Never edit a file in `dashboards-archive/` by hand.** That is how both
copies end up changed at once, and `--load` then silently reverts whatever was
done in the UI. This happened on 17 September 2026: a panel setting was edited
into the archive file while the panel layout had been rearranged in the UI, and
`--load` would have thrown the layout away. If a change is easier to express as
JSON than by clicking, run `--save` first, apply it to the file, and `--load`
it straight back — so the two are only ever out of step for a moment.

Dashboards are deliberately **not** provisioned from a file, which is why the
UI can save them at all. Provisioning refuses every UI save with *"cannot be
saved from the Grafana UI because it has been provisioned from another
source"*, and the setting that permits it is only read when the service starts.

### Noticing drift before it costs you

Forgetting step 2 is silent, so the system says so instead. Both
`xams-ctl status` and the web UI overview show it:

```
  grafana   dashboards saved to git (1)
```

and when they have diverged:

```
  grafana   NOT SAVED TO GIT — XAMS Overview (differs)
            a dashboard only in Grafana is lost with Grafana:
            tools\save_dashboard.py --save --password <pw>
```

This reads Grafana through a **read-only service account** (`xams-drift-check`,
role Viewer), whose token lives in `config/secrets.yaml` — which is gitignored.
It can look at dashboards and nothing else: it cannot edit a panel, delete
anything or touch a datasource. Saving and loading still take the admin
password, deliberately, because those write.

If Grafana is stopped, uninstalled or unreachable the check reports
**unknown**, never "fine" and never "drift". A check that cries wolf about its
own plumbing trains people to ignore it, and one that reports green because it
could not reach anything is worse still.

To re-issue the token — after rotating the admin password, say:

```powershell
# In Grafana: Administration > Users and access > Service accounts
#   xams-drift-check > Add service account token
# then put it in config/secrets.yaml under:
#   grafana:
#     url: http://127.0.0.1:3000
#     token: <the new token>
```

Deleting the `grafana:` section turns the check off; it then reports nothing
rather than complaining, on the grounds that a check nobody configured is not
a fault.

### How often a point appears in a plot

Three different rates are easy to confuse, and only the last one is usually
the problem:

| | rate | set where |
|---|---|---|
| sensors are **read** | 1 s | `interval_s` in `service.py` |
| values are **stored** | 10 s | `log_interval_s` in `service.py` |
| points are **plotted** | *depends on the panel* | Grafana, per panel |

Each row in `meas` is the **mean of the ten 1 Hz samples** in its window, with
`vmin` and `vmax` alongside it — so a spike between two stored points is still
recorded, it is just not the thing plotted by default.

**Storage is already 10 s.** If plots look coarser than that, nothing is wrong
with PostgreSQL or with the write frequency — it is Grafana bucketing, which
it does with `$__timeGroupAlias(t, $__interval)`:

```
$__interval = max(timeRange / maxDataPoints, minInterval)
```

With both unset, `maxDataPoints` defaults to the panel's width in pixels
(~700), so a 12-hour range gives `43200 / 700` ≈ **one point per minute**,
regardless of how much detail is in the database.

Every time-series panel therefore sets, under **Query options**:

- **Min interval** `10s` — the floor. It matches how often a value is actually
  stored, so a finer bucket could only invent empty ones.
- **Max data points** `3000` — the ceiling, and so what decides how far out
  10 s survives: `3000 × 10 s` = **8.3 hours**. Past that Grafana widens the
  bucket on its own rather than pulling tens of thousands of points per series
  into the browser.

To hold 10 s over a longer range, raise **Max data points** — a 24-hour range
at 10 s needs 8640. It is worth doing deliberately: that is 8640 points *per
series*, and the gas-temperature panel draws nine of them.

The measurements land almost exactly on 10 s boundaries, so these buckets come
out dense — a check over a 30-minute window found all 180 buckets present and
all nine channels in every one of them. `spanNulls: 45000` stays in place for
the occasional missed window.

### Grafana: a panel mixing channels from two services shows nothing

Every channel read by one service shares a timestamp; a different service is on
its own loop phase and never lands on the same instant. Grafana pivots a long
result into a wide frame **by exact timestamp**, so a panel mixing two services
produces rows that are ~50% NULL, and with `spanNulls` off there are no two
adjacent points to draw a line between — the series vanishes entirely.

Bucket the query so every channel lands on the same grid:

```sql
SELECT $__timeGroupAlias(t, $__interval), channel AS metric, avg(value) AS value
FROM meas WHERE <selection> GROUP BY 1, 2 ORDER BY 1
```

All the provisioned panels do this. Use it for any new one.

`spanNulls` is set to **45000 ms**, not `true`: enough to bridge sampling
jitter between services, far too short to hide a real outage. A service being
down still breaks the line, which is how a gap is supposed to look (§9.1).

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
Get-Content data
aw\<today>.jsonl -Tail 3
```
```sql
SELECT count(*), max(t) FROM meas;
```

If the database is filling and the JSONL archive is not, the archive is the one
that matters: the files are the truth and the database is only an index over
them (§9.3).

### A retired service still shows a heartbeat

MQTT retained messages outlive the process that published them. A service that
has been stopped for good — `sim`, once the real drivers exist — keeps its last
`xams/status/<service>/heartbeat` on the broker forever, and the staleness
monitor will eventually alarm on something that was retired deliberately.

Clear the retained topics once:

```powershell
& "$env:ProgramFiles\mosquitto\mosquitto_pub.exe" -h 127.0.0.1 -t "xams/status/<service>/heartbeat" -r -n
& "$env:ProgramFiles\mosquitto\mosquitto_pub.exe" -h 127.0.0.1 -t "xams/status/<service>/state" -r -n
```

`-r -n` publishes an empty retained payload, which is how MQTT deletes one.

The same applies to a channel removed from `channels.yaml`: its last value
stays retained under `xams/meas/<channel>`. Clear it the same way, or the
status page and the mimic will keep showing a measurement that no longer
exists.

### The whole PC was rebooted

The three infrastructure services (mosquitto, PostgreSQL, Grafana) start
automatically. **The XAMS services do not** — that is deliberate while
LabVIEW is the fallback. Start them by hand with `xams-ctl start`.

---

## The cDAQ

`xams-ctl start` brings up the `cdaq` service alongside the sinks. It is
**read-only** — the chassis has no output module, so there is no control path.

20 channels are connected of 40 available: 6 voltage on the 9207, 7 PT1000 on
the 9226, 7 PT100 on the first 9216. The second 9216 is entirely free and gets
no DAQmx task at all.

### "cDAQ1 in use by another process (LabVIEW running?)"

The service refuses to start. **Stop the LabVIEW slow-control VI.** Every
device admits exactly one process, and DAQmx reports this as error `-200022`,
"Resource requested by this task has already been reserved by a different
task". This is a clean refusal, not a crash — nothing was changed on the
hardware.

The reverse applies too: run `xams-ctl stop` before starting LabVIEW.

### A temperature channel reports quality=error

The driver publishes `quality=error` with **no value** when an RTD reads
outside −200…+850 °C, the IEC 60751 range for platinum RTDs. Outside that the
module is reporting an open circuit, a short or a missing sensor — not a
temperature.

The log names the channel once, not every second:

```
tt202 (9226/ai1) reads -245.0 C, outside the RTD range -200.0..850.0 —
open circuit, short, or no sensor. Publishing quality=error, not a temperature.
```

**`tt202` is how this was first found.** Its sensor has failed — it reads
open-circuit, identically to the unconnected `9226/ai7`. It is now
`enabled: false` pending replacement, so it no longer reports at all; a
permanently erroring channel would become a permanently active alarm, and an
alarm that is always on is one nobody reads.

**When the sensor is replaced:** set `enabled: true` for `tt202` in
`channels.yaml`, then `xams-ctl reload`. Nothing else changes.

**A genuinely cold sensor is not a fault.** The cryostat RTDs read around
−90 °C and are perfectly valid. The range is the sensor standard, not an
expectation about the experiment — do not narrow it.

### Checking a channel against the hardware

To read the chassis directly, without the service running:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.devices cdaq --interval 1 --log-interval 5
```

Ctrl-C releases the chassis. Add `--simulate` to exercise the driver with no
hardware at all.

---

## Importing LabVIEW history

```powershell
.\.venv\Scripts\python.exe tools\import_labview_csv.py --days 7 --dry-run
.\.venv\Scripts\python.exe tools\import_labview_csv.py --days 7 --to-postgres
```

Safe to re-run: it removes previously imported rows for the same date range
before writing, and only ever touches rows tagged `src='labview'`.

A week is about 6.3 million readings and takes roughly two minutes.

**The importer ignores the LabVIEW header files, deliberately.** They are not
trustworthy: 304 of them contain 90 different layouts, and the current one
describes 62 columns for data that has 47. Layouts are recognised by column
count against a table confirmed against live readings, and **any file whose
layout is not recognised is refused, not guessed at**. If you see

```
  3-4-2025      REFUSED - 46 columns - not a confirmed layout
```

that is the tool working. Confirm what those columns are against known values
before adding the layout to `LAYOUTS` in the script.

To check our readings against the imported record:

```powershell
.\.venv\Scripts\python.exe tools\compare_to_labview.py
```

### Stale retained topics

```powershell
.\.venv\Scripts\python.exe tools\clear_retained.py           # report
.\.venv\Scripts\python.exe tools\clear_retained.py --apply   # clear
```

Run this after retiring a service or removing a channel. MQTT retained
messages outlive the process that published them and are re-delivered to every
new subscriber, so a channel nothing produces any more keeps reappearing in
the database after you delete it. Clearing the broker is the only fix.

---

## The CAEN supplies and the Lake Shore

`xams-ctl start` brings both up alongside the cDAQ. **Everything is read-only.**
The CAEN driver issues `CMD:MON` only and the Lake Shore driver sends only
queries; a test asserts this by inspecting the commands each module actually
sends. Setpoint control is milestone 8.

### They are found by asking, not by COM port

Neither CAEN unit exposes a USB serial number and both present the same
VID/PID, so the COM port tells you nothing. The driver enumerates matching
ports, asks every board for its `BDSNUM`, and binds each to the serial recorded
in `devices.yaml`:

```
  COM4 reports DT1470ET / 19198  -> hv_1
  COM5 reports DT1470ET / 79     -> hv_2
```

**Swapping the two USB cables is therefore harmless** — each unit is found
wherever it is. So is a COM renumbering by Windows.

### "no port reported the expected identity"

The service refuses to start. This is correct behaviour, not a fault: a device
that has not said who it is is not trusted and not written to.

Check in this order:

1. Is the unit powered? `python tools/clear_retained.py` is unrelated — use
   Device Manager or simply look at the front panel.
2. Is something else holding the port? LabVIEW is the usual answer.
3. Has a board been replaced? A new board has a new serial, and
   `devices.yaml` must be edited deliberately — that is the point.

### What the HV Status column means

Read from the board's own `STAT` word, **not** from the voltage:

| shown | meaning |
|---|---|
| `ON` | output enabled and putting volts out |
| `enabled` | output enabled, sitting at **zero volts** — still live |
| `off` | output disabled (STAT bit 10) |
| `TRIP`, `INTERLOCK`, … | a fault flag, shown in red in place of the state |
| `no reading` | the status word could not be read |

**`enabled` is not `off`.** The supplies have a physical enable per channel,
and a channel can be switched on with its setpoint at zero — one turn from
putting volts on an electrode. The page said "off" for exactly that case until
17 September 2026, because it inferred the state from `VMON > 1` instead of
asking the board. Treat `enabled` as live.

The full bitmask is stored as `hv_*_stat`, so the history carries `TRIP`,
`INTERLOCK`, `OVER_CURRENT` and `OVER_TEMP` too, whether or not anything
alarms on them yet.

### Lake Shore serial settings

**57600 baud, 7 data bits, ODD parity, 1 stop bit.** 7-O-1 is the 335's factory
setting and is not a typo. At 8-N-1 the port opens happily and the instrument
returns nothing intelligible, which looks exactly like a dead instrument.

Its sensors read **Celsius**. The driver sends `CRDG?`, not `KRDG?`.

### Unplugging an instrument

Cutting the link is safe and recovers on its own:

- readings are published `quality=error`, never a stale number
- the service **stays running**
- after two failed cycles it re-enumerates the ports and re-asks every board
  for its serial before resuming

That last step matters: a relink **re-verifies identity** rather than just
reopening the port. The handle held before the unplug is dead in any case, a
replugged unit can come back on a different COM number, and if two cables were
swapped while the link was down the service finds each unit where it now is
instead of reading the wrong supply under the right name.

Expect this in `logs\caen.log`, within about ten seconds of replugging:

```
WARNING  hv_1 stopped answering; its channels now read error and a relink will be attempted
INFO     2 candidate port(s) for 21E1:0003: COM4, COM5
INFO     COM4 reports DT1470ET / 19198
INFO     hv_1: relinked on COM4, serial 19198 re-verified
```

**If only one of the two CAEN supplies is unplugged**, the other keeps being
read normally throughout — its numbers do not stop and its channels do not go
stale. Only the unplugged unit's eight channels read `error`.

> **Fixed 17 September 2026.** Before that date this case did *not* recover:
> one supply could be unplugged and replugged and would never come back, with
> nothing in the log to say so, because liveness was judged over the whole
> service and the healthy supply kept it looking fine. If you are running an
> older checkout and a single supply is stuck on `error`, restart the service
> — and update. See §6.1.

A supply that stays unplugged is retried every 10 seconds indefinitely. It
costs the healthy one nothing, so there is no hurry to plug it back in.

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
