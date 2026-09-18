# Editing dashboards

**Every panel on the XAMS Overview is hand-written SQL.** That is the thing to
know before anything else: there is no list of channels to tick, and the
graphical query builder cannot express what these panels do. Adding a channel to
a graph means **editing the `WHERE` clause of that panel's query**.

The rest of this page is that, and then the panel settings worth copying, and
then how to get an edit back into git.

---

## Adding a channel to an existing graph

1. Open **XAMS Overview**, hover the panel, press **e** (or panel menu →
   **Edit**).
2. In the query editor, make sure the toggle on the right of the query row says
   **Code**, not **Builder**. If it says Builder, click **Code**. Builder mode
   cannot produce the `$__timeGroupAlias` / `metric` shape these panels need, and
   switching to it can discard the query.
3. Edit the `WHERE` clause. Nothing else changes.
4. **Refresh** (the arrows, top right) to see it. Then **Save dashboard**.
5. Back in a terminal, put it in git — [below](#getting-an-edit-into-git).

### Which clause to edit

Each panel selects its channels in one of two ways.

**An explicit list.** *Pressures*, for example:

```sql
WHERE channel IN ('pmain','p101','p102','p103','p104')
```

Add the new one inside the brackets, in quotes, comma-separated:

```sql
WHERE channel IN ('pmain','p101','p102','p103','p104','p105')
```

**A pattern.** *Temperatures — detector* takes everything in the 2xx series:

```sql
WHERE channel LIKE 'tt2%'
```

A channel whose name matches appears **on its own**, with no edit at all — which
is the point of the naming convention. To add one that does not match, `OR` it
in, and note the brackets: the `AND quality = 'ok'` that follows must apply to
the whole selection.

```sql
WHERE (channel LIKE 'tt2%' OR channel = 'tt9') AND quality = 'ok' AND $__timeFilter(t)
```

*Temperatures — gas system & ambient* is already written that way and is the one
to copy from.

### Why nothing appears after adding it

In order of how often it is the answer:

| symptom | cause |
|---|---|
| the series is missing entirely | the channel name is wrong. Check it on the [Channels page](../operating/webui.md#channels) — it is the exact string in `channels.yaml`, lowercase |
| the series is missing, name is right | nothing has been **stored** for it yet. Values reach the database every 10 s; a channel added minutes ago has data, one added seconds ago may not |
| it appears but is empty over the chosen range | the channel did not exist then. Widen the time range, or shorten it to now |
| everything vanished, not just the new one | a SQL error — Grafana shows it in red under the query. A missing bracket after an `OR` is the usual one |
| the new series draws, others turn dotted | you added a channel from a **different service** — see [below](#mixing-channels-from-two-services) |

### Mixing channels from two services

Every channel read by one service shares a timestamp; another service is on its
own loop phase and never lands on the same instant. Grafana pivots the result by
**exact timestamp**, so a panel mixing two services produces rows that are half
NULL.

The bucketing in the standard query is what fixes this, and it is why every
panel has it:

```sql
SELECT $__timeGroupAlias(t, $__interval), channel AS metric, avg(value) AS value
FROM meas
WHERE <your selection> AND quality = 'ok' AND $__timeFilter(t)
GROUP BY 1, 2
ORDER BY 1
```

`$__timeGroupAlias` rounds every reading onto the same grid, so a cDAQ channel
and a Lake Shore channel land in the same bucket. **Keep it.** A query written
without it works perfectly while every channel comes from one service and breaks
the day somebody adds one that does not.

---

## Adding a new panel

**Duplicate an existing one** rather than starting from *Add visualization*: the
settings below come with it, and each one exists because something went wrong
without it.

Panel menu → **More** → **Duplicate**, then edit the query, the title and the
unit.

If you do start from scratch: datasource **XAMS**, editor mode **Code**, format
**Time series**, then the query above, then the settings below.

### The settings that matter

| where | setting | value | why |
|---|---|---|---|
| Query options | **Min interval** | `10s` | The floor. It matches how often a value is actually stored, so a finer bucket could only invent empty ones |
| Query options | **Max data points** | `3000` | The ceiling. `3000 × 10 s` = **8.3 hours** of true 10 s resolution; past that Grafana widens the bucket itself rather than pulling tens of thousands of points per series into the browser |
| Graph styles | **Connect null values** | `45s` (`spanNulls: 45000`) | Enough to bridge sampling jitter between services, far too short to hide a real outage. A service being down still breaks the line, which is how a gap is supposed to look (§9.1) |
| Standard options | **Unit** | see below | A number with no unit on a plot is how a paper ends up with the wrong one |

Units used by the existing panels: `celsius`, `pressurebar`, `volt`,
`microamp`, and `suffix:g/min` for the flow — the last being how Grafana takes a
unit it has no entry for.

To hold 10 s resolution over a longer range, raise **Max data points**
deliberately: 24 hours at 10 s needs 8640, and that is 8640 points *per series*
on a panel that may draw nine.

### What is in the table, if you write your own query

`meas` holds one row per channel per log interval:

| column | |
|---|---|
| `t` | timestamp, UTC in the database, displayed in `Europe/Amsterdam` |
| `channel` | the name from `channels.yaml` |
| `value` | **the mean of the ten 1 Hz samples** in that window |
| `vmin`, `vmax` | the extremes within the window, for channels configured with `log_minmax` — a spike between two stored points is recorded, it is just not what is plotted by default |
| `raw` | the value before scaling |
| `unit`, `quality` | as published |
| `src` | `xams`, or **`labview`** for the imported history |

Two consequences worth knowing. Plotting `min(vmin)` and `max(vmax)` alongside
`avg(value)` is how you show what a mean is hiding. And a panel that should show
only what this system measured needs `AND src = 'xams'` — otherwise the week of
imported LabVIEW history is in the plot too, which is usually what you want and
occasionally is not.

---

## Dashboard variables

**There are none today**, and the panels are written without them. If you want a
dropdown of channels — one graph that plots whichever channels are picked —
this is the shape:

1. **Dashboard settings → Variables → New variable**, type **Query**,
   datasource **XAMS**, name `channel`, and **Multi-value** on.
2. Query:

   ```sql
   SELECT DISTINCT channel FROM meas WHERE channel LIKE 'tt%' ORDER BY 1
   ```

3. Use it in a panel with `IN`, and **no quotes** — Grafana quotes each value
   itself when the variable is multi-value:

   ```sql
   WHERE channel IN ($channel) AND quality = 'ok' AND $__timeFilter(t)
   ```

The two traps: writing `IN ('$channel')`, which collapses every selection into
one literal string and plots nothing; and leaving the variable single-value,
where `IN` still works but only ever one series appears.

A variable is worth it for exploring. The fixed panels stay written out, because
the Overview dashboard has to show the same thing to everyone who opens it
without anybody first setting a dropdown.

---

## Getting an edit into git

**Grafana owns the live dashboard. `grafana/dashboards-archive/` is the copy git
tracks.** Edit in one place — the UI — and copy it to the other:

```powershell
# 1. edit in the Grafana UI, and save there as normal
# 2. pull it into git  (no password needed: it uses the read-only token)
.\.venv\Scripts\python.exe tools\save_dashboard.py --save
# 3. commit
git add grafana/dashboards-archive
git commit -m "grafana: <what changed>"
```

!!! warning "Run it with the venv Python, not as `.\tools\save_dashboard.py`"
    Windows maps `.py` to the `py` launcher, which uses a different interpreter
    and sends the script's errors to stderr, where PowerShell's file-association
    path discards them. **A failed run then looks exactly like a successful
    one** — on 18 September 2026 that made a save appear to work, the commit
    afterwards find nothing to commit, and the drift warning stay up with
    nothing to explain it.

Step 2 is the one that matters: **a dashboard that exists only in Grafana's own
database is lost when that database is.** Nothing runs it for you, which is why
the system watches for it — see [the drift check](drift.md).

| command | direction | when |
|---|---|---|
| `--save` *(the default)* | Grafana → git | after **every** editing session |
| `--check` | compare only | before `--load`, before a reinstall, before a `git pull` |
| `--load` | git → Grafana | **only** to restore: fresh install, or Grafana's database lost |

`--save` and `--check` need no password — they only read Grafana, through the
read-only token in `secrets.yaml`. **`--load` requires the admin password**,
because it writes. The asymmetry is deliberate: the operation you run after
every edit should have no friction, and the one that can overwrite a live
dashboard should.

**Never edit a file in `dashboards-archive/` by hand.** That is how both copies
end up changed at once, and `--load` then silently reverts whatever was done in
the UI. This happened on 17 September 2026: a panel setting was edited into the
archive file while the layout had been rearranged in the UI, and `--load` would
have thrown the layout away. If a change is easier to express as JSON than by
clicking, run `--save` first, apply it to the file, and `--load` it straight
back — so the two are only ever out of step for a moment.

Dashboards are deliberately **not** provisioned from a file, which is why the UI
can save them at all; see [Grafana](index.md#why-dashboards-are-not-provisioned).
