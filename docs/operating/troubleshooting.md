# Troubleshooting

**Symptoms, and what each one means.** Every heading is the thing you see, not
the thing that is wrong, so searching this page for the message on your screen
should land on the fix.

*Every time something breaks and is fixed, it goes in here. After a year this
is the most valuable page in the repository.*

---

## Services and the bus

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

## Grafana and the database

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

---

## The cDAQ

Read-only: the chassis has no output module, so there is no control path to go
wrong. 20 channels are connected of 40 available — 6 voltage on the 9207,
7 PT1000 on the 9226, 7 PT100 on the first 9216; the second 9216 is entirely
free and gets no DAQmx task at all.

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

## The CAEN supplies and the Lake Shore

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

### A setpoint is refused: "the supply is in LOCAL mode"

The board's `BDCTR` is `LOCAL`, in which the front panel has control and every
remote `SET` is refused with `LOC:ERR` — **while `MON` keeps answering
perfectly.** That asymmetry is why a board in `LOCAL` looks exactly like a
working one until somebody tries to write, and the whole reason the refusal
names the mode instead of saying the command was not accepted.

Put the board in `REMOTE` at its front panel. Nothing in this software can
change it, by design: it is one of the two hand gates between code and an
electrode (§10a). The mode is read at startup and logged.

### A setpoint is refused: "its setpoint must stay at 0"

The channel's enable switch is off, and §10a's invariant says a disabled channel
holds `VSET` 0 — because the board ramps to `VSET` the instant the switch is
flipped. Flip the enable at the front panel first; the channel comes up at zero
volts and stays there until it is energised. Then set the voltage.

Writing **zero** to a disabled channel is always allowed. That is how the
invariant is re-established on a channel whose stored setpoint is wrong:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl hv-standby --by <you>
```

### A setpoint is refused: outside the permitted range

The range in `channels.yaml` carries the polarity, so the wrong sign is refused
as firmly as the wrong magnitude — −500 V on the anode fails against its
`0..4500`, −3000 V on the cathode against its `−2500..0`. A channel with no
range refuses everything, which is intended: a range nobody wrote down is not
permission to put volts on an electrode.

A value the board itself refuses fails at the read-back instead, reported as
*the write did not take*. Above the board's own `MAXV`, that is the protection
working — and `MAXV` is not something this software can change.

### A channel says ON after being turned off

It is ramping down at the board's own `RDW`, and bit 0 of `STAT` stays set until
it actually reaches zero. The acknowledgement says *ramping down at the board's
own rate*. **Do not re-send the command**; watch `VMON` fall.

Turning on is verified immediately, because the bit is set as soon as the board
accepts it — even though the channel then spends a minute ramping up.

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

