# Backup and restore

**Everything this system knows lives on one Windows PC in one lab.** The
configuration is in git and the Grafana dashboards are in git, so those are
safe. Nothing else is.

This page is about what happens when that machine dies rather than merely needs
reinstalling — a disk failure, a theft, a flood. The distinction matters,
because [Installation](../install.md) covers the second case and is useless for
the first.

!!! warning "Not yet implemented"

    Nothing here runs today. This page states what is irreplaceable and what
    the copy should look like; the destination and the schedule are still open
    — see [What is still to decide](#what-is-still-to-decide).

## What is irreplaceable

| | Where | If the PC dies |
|---|---|---|
| The measurement archive | `data/raw/<UTC date>.jsonl` | **gone forever** |
| Flight-recorder dumps | `data/events/*.jsonl` | gone — the ten minutes before each alarm |
| Flow integrator state | `data/fm101_total.json` | gone — the running total since the last reset |
| Alarm history, flow periods, audit trail | PostgreSQL only | gone — **see below** |
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

Until one of them exists, **treat a PostgreSQL loss as data loss**, whatever
the design document says.

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

## The shape of it

Daily, not weekly. The effort is identical — one scheduled task — and the
difference is whether the worst case costs you a day of the detector's history
or a week.

Push, never pull. Outbound from the lab PC goes through the firewall; inbound
to it almost certainly does not.

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

### Make a failed backup visible

The classic backup story is discovering it stopped working three months ago.
**Do not write a second monitoring system for this** — publish a heartbeat to
MQTT at the end of a successful run, under a channel of its own, and a stale
backup then raises a staleness alarm through the machinery that is already
built, already tested, and already sends SMS.

That is the whole argument for the bus in [the design
specification](../DESIGN.md) §2, applied to something that was never in the
original inventory.

### The key

Unattended copying needs a passwordless SSH key sitting on a Windows box in a
lab. Restrict it: a dedicated key, used nowhere else, and an `authorized_keys`
entry limited to writing into the backup directory — a forced command, or a
write-only destination. Not your ordinary login key.

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

!!! warning "Fill this in"

    **TODO:** the destination. Nikhef project or group storage, SURFdrive, or
    a Nikhef VM. Home directories on login nodes will not take it — roughly
    20 GB a year before compression, so **check the quota before committing**.

    **TODO:** whether unattended SSH key authentication to that destination is
    permitted at all. If two-factor authentication is in the way, an unattended
    push is dead and the answer is SURFdrive's client or a VM inside the
    firewall. **Establish this before writing any script.**

    **TODO:** whether the Nikhef VM of [the watchdog
    item](../status.md) is the same machine. If it is, the two jobs justify it
    together far better than either does alone: one box that notices the system
    has gone quiet *and* holds the copy of what it recorded.
