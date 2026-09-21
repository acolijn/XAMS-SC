# Web UI

**How to use it is in [The web interface](../operating/webui.md).** This page is
how it is built.

Server-rendered HTML, one small inline script, no JavaScript framework and no
build step — so that in three years a student can change it without installing
a toolchain. Binds to `127.0.0.1`, never `0.0.0.0`.

Pages: P&I mimic (`/`, the landing page and read-only), Channels (`/status`),
Control (`/hv`), System health (`/system`), Alarms (`/alarms`), Logs
(`/logs`). `/mimic` is a 301 to `/`, kept for bookmarks. This manual is served by the same
process, at `/manual`, so it is present on the lab PC whether or not the
building network is.

It renders from the **retained MQTT topics, never the database** (§8.1). The
database can be down and every page still answers.

**Writes go through the bus, not through the web process.** A control form posts
to FastAPI, which publishes on `xams/cmd/...` and waits for the matching
`xams/ack/...`; the service that owns the serial port does the validating, the
writing, the read-back and the auditing. The web layer therefore holds no copy
of a permitted range and no instrument handle — a command from this UI and one
from the CLI get identical treatment. POST then redirect, so a refresh cannot
repeat a write.

---

## The routes

| | |
|---|---|
| `GET /` | the P&ID mimic, live and read-only |
| `GET /status` | every channel, as a table |
| `GET /system` | System health: services, faults, channels not reading |
| `GET /hv` | Control: high voltage, cryostat setpoint, flow reset |
| `GET /hv/defaults` | what *Load defaults* offers, as an editable form |
| `GET /mimic` | 301 to `/` |
| `GET /alarms` | what fired, the thresholds in force, the recipient list |
| `GET /logs` | service logs |
| `GET /api/state` | everything the pages show, as JSON |
| `GET /healthz` | plain text, for a watchdog |
| `GET /manual/…` | this manual, mounted as static files |
| `POST /hv/apply`, `/hv/output` | HV setpoint, energise/de-energise |
| `POST /hv/defaults` | write `hv_defaults.yaml`; touches no instrument |
| `POST /lakeshore/setpoint`, `/lakeshore/range` | Lake Shore, output 1 |
| `POST /flow/reset` | close the integrator period |
| `POST /alarms/recipients` | write `recipients.yaml`; touches no instrument |
| `POST /operator` | remember who is at the keyboard, in a cookie |

Every POST that commands an instrument publishes on `xams/cmd/…` and waits for
its acknowledgement. None of them touch an instrument.

**The two that write a file are the exception** — `/hv/defaults` and
`/alarms/recipients` change `config/*.yaml` and nothing else. There is no
service on the other end to validate them, so they are checked here, in
`config.py`, before anything is written: all or nothing, because a half-saved
list is a list nobody chose. Both are still audited like any other change, and
both files are tracked by git.

**Who did it** comes from a cookie set once, rather than a name typed before
every command — retyping a name for each setpoint is the kind of friction
that gets worked around by leaving it blank, which costs the audit trail the
thing it exists for. It is taken on trust: there is no login, so this
identifies a browser, not a person, and that is recorded honestly rather than
dressed up.

---

## `/api/state`

For the Python client, and for anything else that wants the state without
scraping HTML. One object, built from the same retained topics the pages
render from:

```json
{
  "overall": "ok",
  "config": "89f5d1d",
  "services": {…},
  "alarms": […],
  "notifications": {"known":true,"enabled":true,"by":"apc","at":"…"},
  "faults": […],
  "channels": [
    {"name":"tt301","value":-92.4,"unit":"C","quality":"ok",
     "age_s":3.1,"alarm":null,"healthy":true},
    {"name":"tt302","value":null,"unit":"C","quality":"no data",
     "age_s":null,"alarm":null,"healthy":false}
  ]
}
```

`age_s` is how long ago the reading arrived, which is what makes a stale
channel visible to a caller that has no clock of its own. It is **`null` for
a channel that has never reported at all** — the same way a service with no
heartbeat reports one — rather than a very large number, which a caller
would have to know to treat specially. `healthy` folds quality and age into
the one boolean a dashboard usually wants.

`notifications` says whether an alarm would reach anybody (§4.4a).
`enabled: null` with `known: false` means the alarm engine has not said, which
is not the same as `true`: a caller deciding "is the plant being watched"
must not read silence as a yes.

---

## The mimic

`/` serves a static SVG with live values written into it.

```powershell
python tools/build_mimic.py
```

That converts `notes/xams_piping_and_instrumentation.pdf` into
`src/xams_sc/api/static/xams_pid.svg` and adds an empty `<text>` node beside
every instrument tag that corresponds to a channel. The page fills those in
on each render; the SVG itself never changes at runtime.

**Re-run it when the P&ID is revised.** A mimic quietly out of date with the
plant is a liability.

### The tag check, in both directions

At startup the web service compares the ids in the SVG against
`channels.yaml` and logs every mismatch. Both directions matter, and they
fail differently:

| | |
|---|---|
| an id with no channel | the drawing shows an instrument this system does not read, and the bubble would sit empty forever |
| a channel with no id | a reading nobody can find on the drawing |

It is never fatal — a mimic that has drifted is still more useful than no
mimic — and it is roughly ten lines. Channels that legitimately have no place
on a piping drawing (the ambient room temperature, for instance) set
`on_pid: false` in [`channels.yaml`](config.md), because a warning that is
always on is one nobody reads.

**The drawing is the authoritative list of tag names.** Mostly they are the
channel names in lower case; the pressures are the exception, where the P&ID
says `PT101`–`PT104` and `channels.yaml` says `p101`–`p104`, following what
LabVIEW logged. That mapping is declared in `TAG_ALIASES` rather than inferred
by a regular expression, so the divergence is stated rather than hidden.

---

## Refreshing, and the one page that does not

Every page reloads itself every 10 s — except **Logs**. A log that reloads
while you are reading it takes the line away mid-sentence, so that page is a
snapshot and says when it was taken.

The refresh is done in JavaScript with a `<noscript>` meta fallback, and it
**pauses while the page holds anything unsent**. A meta refresh cannot be
cancelled once parsed, and it would clear a half-entered operator name and
throw away the click that was about to follow.

The test is `value !== defaultValue` over every enabled field — *does this page
differ from what the server rendered?* — not whether anything has focus. Focus
was the original rule and it was wrong in the one place it mattered: *Load
defaults* on the HV page writes into boxes nobody is touching, so nothing took
focus, the timer ran, and the reload replaced the loaded setpoints with the
empty boxes the server renders. A box filled by a button and a box filled by
hand are both unsent operator intent and are now indistinguishable to the
timer.

It reschedules rather than giving up, so a box left filled and forgotten
delays each tick instead of freezing the page for the rest of the day.

The same script does one other thing, on `/hv` only: an energise form disables
its button and relabels it `switching…` on submit. The round trip is long
enough that the row still read *not energised* while it was in flight, and a
second click after a refresh had swapped the button to **turn off** would
de-energise the channel the first had just started. *Apply setpoints* is
idempotent and is deliberately left alone.

---

## Logs

`/logs` tails `<repo>\logs\<service>.log`, newest first, from the folder
[`config.LOG_DIR`](config.md) names — anchored to the repository rather than
to whatever directory the service was started from.

Two details worth keeping:

- **It reverses records, not lines.** A traceback is one record of several
  lines, and a line-by-line reverse prints it inside out — exactly the record
  somebody opened the page for.
- **The selectable names are a whitelist.** The name from the URL is looked
  up among the files that exist, never interpolated into a path. Loopback
  binding is a second line of defence, not the first.

Rotated files (`.1` … `.5`) are offered beside the current one, because a
talkative service can have its last hour in `.1` while `caen.log` looks
nearly empty.

---

## What it deliberately does not do

- **No JavaScript framework, no build step, no bundler.** The whole UI is
  Jinja templates and one small script.
- **No database access.** Retained MQTT only, so every page answers when
  PostgreSQL does not.
- **No validation of control values.** Ranges live in `channels.yaml` and are
  enforced by the service that owns the port. Duplicating them here would
  mean two numbers to keep in step, and the copy in the config is the one
  that counts. The page checks only *is it a number*. The exception is the
  two forms that write a YAML file rather than command an instrument: there
  is no service downstream to check them, so they are checked where the
  change is made.
- **No instrument handles.** The web process could not write to hardware if
  it tried.
