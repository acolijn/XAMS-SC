# Routine tasks

Things done occasionally and deliberately: looking at the raw data, adding a
sensor, importing history, saving a dashboard. Day-to-day work is in
[Every day](index.md); when something is broken, go to
[Troubleshooting](troubleshooting.md).

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

**Do not guess a unit.** A wrong label propagates into every plot, every
threshold, and eventually into a paper. `unit: TBD` is the honest state until
somebody establishes what the sensor actually reports — the pressure channels
sat at `TBD` until the group supplied the answer (bar), and no channel carries
it now. Ask; do not infer it from the numbers looking about right.

### Changing a calibration

Same procedure — edit `offset` / `multiplier`, check, commit, reload.

Scaling is `value = (raw - offset) * multiplier`, in that order, matching
LabVIEW exactly. **Do not change the convention**: historical comparisons break
silently if you do.

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

## Saving a Grafana dashboard

### Editing a dashboard, the right way round

**Grafana owns the dashboard. `grafana/dashboards-archive/` is the copy git
tracks.** Edit in one place — the Grafana UI — and copy it to the other. The
loop:

```powershell
# 1. edit in the Grafana UI, and save there as normal
# 2. pull it into git  (no password needed: it uses the read-only token)
.\.venv\Scripts\python.exe tools\save_dashboard.py --save
# 3. commit
git add grafana/dashboards-archive
git commit -m "grafana: <what changed>"
```

!!! warning "Run it with the venv Python, not as `.	ools\save_dashboard.py`"
    Windows maps `.py` to the `py` launcher, which uses a different
    interpreter and sends the script's errors to stderr, where PowerShell's
    file-association path discards them. **A failed run then looks exactly
    like a successful one** — on 18 September 2026 that made a save appear to
    work, the commit afterwards find nothing to commit, and the drift warning
    stay up with nothing to explain it.

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
            tools\save_dashboard.py --save
```

This reads Grafana through a **read-only service account** (`xams-drift-check`,
role Viewer), whose token lives in `config/secrets.yaml` — which is gitignored.
It can look at dashboards and nothing else: it cannot edit a panel, delete
anything or touch a datasource.

**`--save` and `--check` use that same token**, so neither needs a password:
both only read from Grafana and write to disk. **`--load` still requires the
admin password**, because it is the one that writes back into Grafana. The
asymmetry is deliberate — the operation you run after every edit should have
no friction, and the one that can overwrite a live dashboard should.

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

