# Backup and restore

**Everything this system knows lives on one Windows PC in one lab.** The
configuration is in git and the Grafana dashboards are in git, so those are
safe. Nothing else is.

This page is about what happens when that machine dies rather than merely needs
reinstalling — a disk failure, a theft, a flood. The distinction matters,
because [Installation](../install.md) covers the second case and is useless for
the first.

!!! success "Implemented 18 September 2026"

    `tools\backup.ps1` copies the irreplaceable data to
    `/data/xenon/xams_slow_control/archive/` on the Nikhef cluster, nightly at
    03:30 via a Scheduled Task, and publishes its result so a silent failure
    becomes visible.

    **Still outstanding: the restore rehearsal.** Nobody has pulled a day back
    and replayed it. Until that is done the restore path is a claim, not a
    fact — see [the warning below](#restoring).

## What is irreplaceable

| | Where | If the PC dies |
|---|---|---|
| The measurement archive | `data/raw/<UTC date>.jsonl` | **gone forever** |
| Flight-recorder dumps | `data/events/*.jsonl` | gone — the ten minutes before each alarm |
| Flow integrator state | `data/fm101_total.json` | gone — the running total since the last reset |
| Alarm history, flow periods, audit trail | PostgreSQL only | gone — **accepted, see below** |
| Credentials | `config/secrets.yaml` | gone; recoverable, painfully |
| Channel and alarm definitions | git | safe |
| Grafana dashboards | git | safe |
| NI-MAX module aliases | [Installation §2.2](../install.md) | safe, now that they are written down |
| LabVIEW | the machine | gone, and it is the fallback |

## The database is not entirely an index

[The design specification](../DESIGN.md) says *the files are the truth; the
database is an index over them* — if PostgreSQL is lost, replay the archive
into a new one. That is the right architecture and it is true of measurements.

**It is not true of three tables.** `jsonl_writer` subscribes to the
measurement topic and nothing else, so anything published on another topic
reaches PostgreSQL and never reaches the archive:

| Table | Written by | Replayable from `data/raw/`? |
|---|---|---|
| `meas` | `pg_writer` | **yes** — this is the index |
| `alarm_events` | `alarm_writer` | no |
| `flow_periods` | `flow_writer` | no |
| `audit` | `flow_writer` | no |

So the audit trail — who reset the integrator, when, and what the total was —
exists in exactly one place, and that place is the component the architecture
describes as disposable. The same goes for the alarm history.

Two ways to close that, neither done:

- **Archive every topic, not just measurements.** The honest fix: it makes the
  claim true rather than nearly true. Costs a subscription and some disk.
- **Include a PostgreSQL dump in the copy.** Cheaper to write, and it
  contradicts the architecture in a small way each time it runs.

**Decided 18 September 2026: this is accepted, not fixed.** Alarm history is
not worth a schedule for a lab instrument, and the measurements are what
matter. So neither option was taken: PostgreSQL is not backed up, and a
PostgreSQL loss costs the alarm history, the flow periods and the audit trail.

The consequence to keep in view is that **`audit` is not alarm history.** Since
the Lake Shore control path was built it records who changed the cryostat —
setpoint and heater writes, with old and new values. That is traceability, not
history, and it now lives in exactly one place. If that ever becomes
uncomfortable, the cheap fix is one extra topic subscription in `jsonl_writer`
so those records land in the archive the backup already copies; no schedule and
no `pg_dump` required.

The upside of accepting it is real: the backup is a **pure file copy**, it
never contradicts the architecture, and PostgreSQL stays genuinely disposable.

## What not to copy

**`config/secrets.yaml`.** Do not push plaintext credentials onto shared
institutional storage, where the default permissions are rarely what you
assume. Copy it once, by hand, into a password manager. It is a handful of
lines and it changes about once a year — [the MessageBird key is due for
rotation](../status.md), so do both in one sitting.

**The bulk of PostgreSQL.** Backing up `meas` would contradict the
architecture: it is derived from the archive and can be rebuilt from it. Copy
the archive and prove the replay works. The three tables above are the
exception, and they are small.

**`logs/`.** Useful for a week, worthless after. Not worth a schedule.

## Why this is not an rsync problem

`data/raw/` is **append-only and date-partitioned**: today's file grows,
yesterday's never changes again. `data/events/` is write-once. So rsync's delta
algorithm buys nothing here — *copy the files that changed since the last run*
is trivially correct, and Windows 10 and later already ship an OpenSSH client.

That means no cwRsync, no WSL, no new component on the lab PC. The same
argument as the web UI having no build step: in three years someone must be
able to read this and fix it without installing a toolchain.

Use rsync if the link turns out to be flaky enough to want `--partial`. Not
before.

## How it runs

```powershell
.\tools\backup.ps1              # copy what changed since the last run
.\tools\backup.ps1 -Full        # everything, ignoring the stamp
.\tools\backup.ps1 -WhatIf      # list what would go, send nothing
```

Registered as a Scheduled Task (once, from an **elevated** prompt):

```powershell
.\tools\install_backup_task.ps1 -RunNow
```

Daily, not weekly: the effort is identical and the difference is whether the
worst case costs a day of the detector's history or a week. Push, never pull —
outbound from the lab PC goes through the firewall; inbound almost certainly
does not.

**A Scheduled Task, not an NSSM service.** The services run as LocalSystem,
whose profile is `C:\Windows\System32\config\systemprofile` — it would look
for the SSH key there, not find it, and fail with `Permission denied
(publickey)`: an error identical to a broken key, from a command that works
perfectly by hand. The task runs as `localadmin`, which is where the key is.

### What it does each night

1. Lists files under `data/raw`, `data/events` and `data/quarantine` modified
   since the last **successful** run, plus `data/fm101_total.json`.
2. Checks there is room at the far end, and refuses rather than half-filling a
   shared volume.
3. Packs them, sends the archive, unpacks it, removes the staging file.
4. **Verifies** the bytes arrived, then writes the stamp.
5. Publishes the result, retained, to `xams/backup/status`.

The stamp moves **only** after a verified success, so a failed night is
repeated rather than skipped.

### Three Windows traps, each of which cost a run

**Binary cannot be piped between native commands in PowerShell 5.1.** The
obvious `tar -cf - ... | ssh ... tar -xf -` is re-encoded as text and arrives
corrupt: *"This does not look like a tar archive"*, with nothing at either end
to say why. The tar is staged to a file and sent with `scp`.

**`mosquitto_pub -m "<json>"` loses the quotes.** Windows argument parsing
strips them, so the broker received `{detail:2 file(s), 14,5 MB,bytes:...}`,
which is not JSON, and every reader of it failed. The payload is written to a
file and sent with `-f`.

**Verify against what was packed, not what was measured.** `data/raw/<today>`
is being appended to *while the backup runs*, so it is already larger by the
time `tar` reads it than when the file list was built. Comparing the
destination against the earlier figure reports a mismatch on a perfectly good
backup, every single night — and a check that cries wolf nightly is the one
people learn to ignore. The comparison is against the tar's own listing.

```powershell
# Sketch, not a working script. Copies anything written since the last
# successful run, then records the new high-water mark.
$dest  = "TODO@TODO:TODO/xams-backup"     # see below
$stamp = "$env:LOCALAPPDATA\xams-backup.stamp"

$since = if (Test-Path $stamp) { Get-Content $stamp | Get-Date }
         else { [DateTime]::MinValue }

$files = Get-ChildItem data\raw, data\events -File |
         Where-Object { $_.LastWriteTime -gt $since }

foreach ($f in $files) { scp -i $key $f.FullName $dest }
scp -i $key data\fm101_total.json $dest

(Get-Date -Format o) | Set-Content $stamp
```

Run it from Task Scheduler, as a task that runs whether or not anyone is logged
in.

### Making a failed backup visible

The classic backup story is discovering it stopped working three months ago.
The result is therefore published **retained** to `xams/backup/status`, and
read back in the two places already looked at daily:

```
  backup    ok, 0.0 h ago (2 file(s), 14,6 MB)
  backup    OVERDUE - last success 41 hours ago
  backup    NEVER REPORTED - the archive is on this PC only
```

and as a row in the *UPS & dashboards* card on the web UI overview.

**Nothing published is reported as "never reported", never as "ok".** The
absence of news is an absence, not good news — that is the exact shape of the
three-months-dead backup.

!!! warning "This does NOT reach SMS, and here is why"

    The original plan was to publish a heartbeat as an ordinary channel and let
    the existing staleness alarm do the work. **That cannot work as the alarm
    engine stands:** `stale_after_seconds` is a single global default of 60 s,
    with no per-channel override, so a nightly heartbeat would raise a major
    alarm sixty seconds after every successful run.

    Reaching SMS needs one of: a per-channel staleness override, or a
    long-running service publishing "hours since last backup" as a channel with
    an ordinary `high`/`hihi` threshold. The second is about fifteen lines and
    reuses everything. Neither is done, so **a failed backup is visible but
    will not wake anybody.**

### The key

Unattended copying needs a passwordless SSH key sitting on a Windows box in a
lab. Restrict it: a dedicated key, used nowhere else, and an `authorized_keys`
entry limited to writing into the backup directory — a forced command, or a
write-only destination. Not your ordinary login key.

## The NI driver: the part with a clock on it

The measurement archive can wait for a schedule. This could not, and it is done.

The cryostat is read through a **cDAQ-9174** and NI-DAQmx **24.5.0**.

!!! warning "This was first justified with a false claim"

    The original argument here was that the 9174 is a *discontinued* chassis,
    so NI would eventually drop it and the hardware would become unreadable.
    **The 9174 is not discontinued** — NI lists it as Active and sells it. The
    claim was inherited from [Installation](../install.md), repeated without
    checking, and it overstated the risk considerably.

The copy is still worth the 11 GB, for smaller and more ordinary reasons:

- A rebuild does not need an ni.com account, a password somebody has forgotten,
  or internet access from the lab PC.
- It pins the **exact version this system has been verified against**, so a
  rebuild does not silently become a driver upgrade at the worst moment.
- 24.5.0 is no longer on NI’s download page; 26.5.0 is. Getting 24.5.0 back
  would mean going through NI support rather than clicking a link.

That is insurance against inconvenience, not against catastrophe. Worth having,
not worth alarm.

Archived to `/data/xenon/xams_slow_control/software/` on 18 September 2026:

| File | |
|---|---|
| `nipm-package-cache-2026-09-18.tar` | 6.8 GB — the whole NI Package Manager cache from the lab PC, **containing NI-DAQmx 24.5.0**, the version demonstrably driving the 9174 |
| `ni-daqmx_26.5.0_offline.iso` | 3.2 GB — NI’s current offline installer |
| `README.md` | what each artefact is, and what has not been verified |

**The risk was already real when we looked.** The lab PC runs 24.5.0; NI no
longer offers it for download. The current offline installer is 26.5.0, and
whether 26.5.0 still supports the 9174 was **not established** — so the
trustworthy artefact is the package cache, not the ISO.

Two things are archived but unproven, and the README says so plainly: the cache
needs **NI Package Manager**, which was not on the lab PC as a standalone
installer and so is not archived; and **nobody has installed 24.5.0 from that
cache onto a clean machine.**

## Restoring

The order matters, and only the first step is urgent.

1. **Get the archive back first**, onto any machine. It is the only thing that
   cannot be reconstructed, and it is readable with nothing more than a text
   editor — that is why the format was chosen.
2. Reinstall from [Installation](../install.md). NI-DAQmx and the module
   aliases are §2.
3. Replay the archive into the new PostgreSQL. Grafana comes back from git.
4. `config/secrets.yaml` from the password manager.
5. Accept that the alarm history, flow periods and audit trail are gone, unless
   one of the two fixes above has been done by then.

!!! warning "A backup you have never restored is a hypothesis"

    Do this once, early, while nothing is on fire: pull a single day back from
    the destination and replay it into a scratch database. The replay path is
    claimed by the architecture and, as far as anyone knows, **has never been
    exercised**. Finding out that it works is worth an afternoon; finding out
    that it does not, after a disk failure, is worth rather more.

## What is still to decide

**Settled 18 September 2026:**

| | |
|---|---|
| Destination | `/data/xenon/xams_slow_control/` — group `xenon`, setgid, z37 can write. **Backed up by Nikhef**, so this is genuine off-site redundancy rather than a second copy in one failure domain. |
| Unattended SSH | **Permitted and working.** Key auth from the lab PC, no 2FA in the way, via `login.nikhef.nl` as a jump host to `stbc-i2`. The alias is `nikhef-backup`. |
| Vendor binaries on that storage | Acceptable at Nikhef. |
| PostgreSQL | Not backed up — [accepted](#the-database-is-not-entirely-an-index). |

!!! warning "Watch the free space"

    `/data/xenon` was **95% full** when this was set up — 1.3 TB free of 25 TB.
    Against ~20 GB a year that is decades, but it is **shared xenon-group
    storage** and somebody else’s dataset can eat it. A backup that stops
    silently because the volume filled is the classic version of this story, so
    the nightly job should check free space and complain rather than simply
    write.

!!! warning "Still to do"

    **The nightly copy itself.** Until it exists the measurement archive has
    one copy, on the lab PC.

    **The restore rehearsal.** Pull one day back and replay it into a scratch
    database. The replay path is claimed by the architecture and has never been
    exercised.

    **Install NI Package Manager + DAQmx 24.5.0 from the archived cache onto a
    clean VM**, once, to turn "very likely to work" into "known to work". This
    is the one that would hurt most to get wrong, because you find out during a
    rebuild.

    **The Nikhef VM** of [the watchdog item](../status.md) — whether it is the
    same machine. If it is, the two jobs justify it together far better than
    either does alone.
