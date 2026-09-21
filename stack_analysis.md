# XAMS Slow Control — stack analysis

**Date:** 2026-09-21 · **Commit:** `origin/main` @ `9bfe277` · **Scope:** the whole
stack — drivers, bus, storage, alarms, web UI, CLI, configuration, tests, tooling.

This is a review of the code as it stands, not of the design specification. Where
the two disagree, that is noted. Findings are ordered by how much they would cost
if they went wrong, not by how easy they are to fix.

---

## 1. What the stack is

Eight Python processes on one lab PC, joined by a loopback MQTT broker:

| Layer | Modules | LOC |
|---|---|---|
| Instrument drivers | `devices/{caen,cdaq,lakeshore,ups,derived,sim}.py` | ~2 400 |
| Service framework | `service.py`, `bus.py`, `model.py`, `scaling.py` | ~840 |
| Configuration | `config.py` + `config/*.yaml` | ~570 |
| Storage sinks | `sinks/{jsonl,pg,audit,alarm,flow}_writer.py` | ~570 |
| Alarming | `alarms/{engine,notify,mail,flight_recorder,daily}.py` | ~1 300 |
| Web UI | `api/{app,state}.py` + 8 Jinja templates | ~1 730 |
| Operator CLI | `cli/xams_ctl.py` | ~900 |
| Tools | `tools/*.py`, `tools/*.ps1` | ~2 000 |

About 4 600 executable statements in `src/`, 525 tests, 33 pages of documentation.
76 channels defined, 63 enabled, read at 1 Hz and published as 10 s means.

The shape is a clean fan-out: drivers publish and forget; storage, alarming and
the UI are independent subscribers. Nothing but the sinks touches PostgreSQL, and
nothing but a driver touches an instrument.

---

## 2. Strong points

These are not politeness. They are the things a reviewer would be pleased to find
and usually does not.

### 2.1 The bus boundary is real, not aspirational

`bus.py` is the only module that knows MQTT exists, and every service reaches
hardware or storage through it. The consequence shows up in the history: the audit
writer, the alarm-state writer and the flow-period writer were each added without
touching a driver. That is the test of a layering claim, and this one passes.

Two specific decisions are better than the obvious alternative:

- **Handlers are a list, not a dict keyed by topic** (`bus.py:124`). Three
  consumers legitimately subscribe to `xams/meas/#`. A dict would have let the
  second silently displace the first — a bug that shows up months later as missing
  data, with no error anywhere.
- **The outage buffer is bounded** (`bus.py:110`), so a broker outage costs a
  recorded gap rather than a memory leak. The same reasoning is applied again in
  `PgWriter.max_pending`. Consistency of reasoning across modules is rarer than
  correctness in any one of them.

### 2.2 The HV write path is genuinely defensive

`caen.py:_handle_vset` is the safety-critical function in the system, and it reads
like someone thought about it for a long time. In order: parse, resolve the channel
by name, require `kind == "hv_vset"` and `enabled`, refuse in simulation, require a
live reader, check the software range from `channels.yaml`, **read the board's
status word and refuse a non-zero setpoint on a channel that is not enabled**, then
write and read back before calling it successful.

The status-word check is the good one. Without it a person could stage 4.2 kV on a
disabled channel, walk to the supply, flip the hand switch and get exactly the
unannounced ramp the design exists to prevent. Refusing that is not obvious, and
the comment explains why it is there — so the next person will not "simplify" it
away.

The deliberate omissions are as good as the checks: nothing writes `MAXV`, `RUP`,
`RDW`, `TRIP` or `ISET`, and the channel enable stays a hand operation.

### 2.3 Configuration really is data

Adding a sensor is a YAML entry. `config.py` validates strictly at startup —
duplicate names, bad `kind`, `sign` outside {-1, 1}, `limits` with `min > max`,
`rtd` kind without an `rtd:` block — and raises `ConfigError` rather than failing at
the first bad read six months later. A channel with no `limits` refuses every write,
which is the correct default: a range nobody wrote down is not permission.

The config hash (SHA-256 over the normalised `channels`/`devices`/`alarms`, first 7
hex) is written into every JSONL day header. That makes archived data
self-describing — you can tell which configuration produced a file without
consulting git. `recipients.yaml` and `hv_defaults.yaml` are deliberately excluded
because they do not affect the data. That distinction is correct and is documented.

### 2.4 The file archive is the source of truth

JSONL first, PostgreSQL as an index over it, with `flush()` + `os.fsync()` at least
every 10 s. A database loss is a re-import, not a data loss. The two-writer topology
(one local, one on a Nikhef VM) falls out of this for free, and a VM outage is
invisible to everything local.

### 2.5 The command path has one owner

The UI never touches an instrument. It publishes to `xams/cmd/...` and waits for an
ack, so validation, read-back and the audit record happen in the one process that
owns the hardware — identically whether the request came from a browser or from
`xams-ctl`. A timeout is reported as **failure**, never as success. That is the right
default and it is easy to get wrong.

### 2.6 The comments explain *why*

Almost every non-obvious line carries the reason it exists, often with the failure
that motivated it. `operator_names()` spends fifteen lines explaining why the
"acting as" box offers suggestions rather than a closed list, and why a plausible
wrong name in the audit trail is worse than `webui (unnamed)`. That is the comment
style that survives contact with a new student, which is the stated design goal.

There are **zero** `TODO`, `FIXME`, `XXX` or `HACK` markers in `src/`.

### 2.7 A test that catches undefined names

`tests/test_no_undefined_names.py` runs pyflakes over the tree. A module that
imports is not a module whose functions run, and a `NameError` in an unexercised
branch is otherwise invisible until somebody in the lab runs the command. Given the
coverage gaps in §3.1 this is doing real work.

### 2.8 The documentation is unusually good

33 pages, built with mkdocs and served by the web UI itself at `/manual`, with a
drift check (`check_mimic_tags`) that flags channels missing from the P&ID drawing.
Documentation that the system verifies against itself does not rot quietly.

---

## 3. Weak points

### 3.1 Coverage is 52 %, and the gaps are in the wrong places

Measured, not estimated (`coverage run -m pytest`):

| Module | Stmts | Cover | Why it matters |
|---|---:|---:|---|
| `sinks/audit_writer.py` | 49 | **0 %** | The audit trail is a safety claim. Nothing tests that it writes. |
| `sinks/alarm_writer.py` | 72 | **0 %** | Alarm history persistence, untested. |
| `sinks/flow_writer.py` | 66 | **0 %** | Flow periods, untested. |
| `sinks/__main__.py` | 109 | **0 %** | The supervisor for all three. |
| `alarms/__main__.py` | 115 | **0 %** | Wires the engine to the bus and notifier. |
| `alarms/daily.py` | 77 | **0 %** | The daily report. |
| `devices/ups.py` | 142 | **0 %** | Mains-loss detection. |
| `cli/xams_ctl.py` | 549 | **15 %** | The operator's interface during an incident. |
| `service.py` | 218 | **32 %** | The run loop, reconnect and window emission every driver inherits. |

The tested part is tested well — `config.py` 92 %, `app.py` 82 %, `engine.py` 81 %.
The untested part is the part that runs unattended at three in the morning. The CLI
at 15 % is the sharpest edge: it is what someone reaches for when the web UI is
unreachable, which is precisely when it must not have a `NameError` in an error
branch.

The `__main__` modules are process entry points and awkward to test, which is why
they got skipped. They are also where the wiring bugs live.

**The cause was scaffolding cost, and that part is now fixed.** There was no
`conftest.py`. `FakeBus` was hand-built ten times, `StubDrift` four, the
`config_dir` fixture four — so every new test file began with forty lines of setup
before it tested anything, and nobody started. The copies had drifted, too: three
recorded parsed payloads where the rest recorded strings, two keyed handlers by
topic where the real bus keeps a list (leaving those files structurally unable to
catch the fan-out bug they nominally covered), and two imported their doubles from
*other test modules*. When `recipients.yaml` left the repository, three of the four
`config_dir` copies were updated and the fourth was not — which is how the suite
came to fail on a fresh clone.

One `tests/doubles.py` and one `tests/conftest.py` now hold a single `RecordingBus`,
`StubDrift` and `FakePg`, and 14 test files were migrated onto them: 192 insertions
against 535 deletions, net −343 lines, suite unchanged at green. Coverage did not
move and was not meant to — this buys nothing except that the next test is cheap to
write, which is the precondition for everything below.

### 3.2 A dropped database row is logged at DEBUG

Principle 4 is "fail loudly, never silently". `PgWriter._dropped` increments when
`max_pending` (100 000 rows) is reached, and the only consumer is
`sinks/__main__.py:161`, which logs `pg.stats` at **`log.debug`**. Default log level
is INFO. So the system's own stated worst case — data discarded — is invisible in
normal operation.

The same applies to `Bus.dropped`: it is reset inside `_flush()`, so the property
almost always reads 0 when anybody asks. The overflow *is* logged at ERROR, which is
right; the counter just cannot be queried afterwards.

**Neither of these raises an alarm**, though the alarm engine is right there and the
design says a gap must be visible.

### 3.3 Drain rate after an outage is slower than it looks

`PgWriter.pump()` calls `flush()` once per `flush_interval_s` (10 s), and `flush()`
writes at most `batch_size` (500) rows. So the maximum drain is 50 rows/s. A full
`max_pending` backlog of 100 000 rows takes **~33 minutes** to clear, during which
new readings keep arriving and — at the cap — are being dropped.

A `flush()` that loops until the pending list is empty or a time budget expires
would drain in seconds. The current shape is fine in steady state and poor exactly
when it is needed.

### 3.4 Acks have no correlation id

`state.command()` clears the ack topic, publishes, then polls for anything arriving
on that topic:

```python
with self._lock:
    self._acks.pop(ack_topic, None)
self.bus.publish_raw(topic, json.dumps(payload))
# ... poll for self._acks[ack_topic]
```

Two operators on two browsers setting two different HV channels at the same time
both wait on `xams/ack/caen/vset`. Whichever ack lands first is returned to
whichever request polls first. Each operator can be shown the other's result — and
since the ack carries `old` and `new` voltages, the wrong number is displayed
confidently.

The window is short (the CAEN serial lock serialises the writes) and needs two
simultaneous operators, so this is unlikely rather than impossible. It is also
cheap to close: put a `cmd_id` in the command, echo it in the ack, ignore
non-matching acks.

### 3.5 The CLI reimplements the ack protocol, three times

`state.command()` is a clean 20-line send-and-await. `xams_ctl.py` does the same
thing by hand for `hv set`, `hv output`, `notify` and `flow-reset` — with
hand-rolled connect-wait loops, a `time.sleep(0.3)` settle, `acks.append` closures,
and **a different timeout each time** (15 s in the CLI, 10 s in the UI).

This is the largest duplication in the codebase and it sits on the control path. One
shared `send_and_await(bus, topic, ack_topic, payload, timeout)` in `bus.py` would
delete roughly 120 lines, unify the timeouts, and give both callers the correlation
id from §3.4 at once. It would also be the single cheapest way to lift the CLI's
15 % coverage, since the tested logic would move into a tested module.

### 3.6 `app.py` is 1 255 lines and mixes four concerns

It holds route handlers, form parsing, an audit helper, log-file reading, ANSI
colourisation (`_colourise`, `_newest_first`, `_rotated`, `_log_files` — about 120
lines that have nothing to do with HTTP), P&ID drift checking, and recipient
editing. `hv_defaults_save` alone runs from line 670 to 778.

Nothing here is wrong, but it is the file a new student will be most afraid of, and
that conflicts with principle 1. The log-viewer helpers and the recipient-editing
helpers are each a module-sized cohesive unit that could move out without touching a
route.

### 3.7 No CI, no lockfile, no type checking

- **No `.github/`.** 525 tests that nobody is obliged to run. The
  `test_template_handlers.py` fixture broke during the `recipients.yaml` purge and
  reached `origin/main` unnoticed; the pyflakes guard in §2.7 had been skipping for
  longer than anyone knows. Both are exactly the class of failure CI catches, and
  neither is visible to someone reading a green local run.
- **Every dependency is `>=` with no upper bound and no lockfile.** A fresh install
  on the lab PC resolves to whatever PyPI serves that day. `paho-mqtt>=2.0`,
  `fastapi>=0.110` and `psycopg[binary]>=3.1` can all ship a breaking change into a
  reinstall of an instrument control system. There is already a
  `StarletteDeprecationWarning` about `httpx` in the test output.
- **No mypy, no ruff config.** pyflakes via a test is a good floor, but it will not
  catch a `str` where a `float` is expected — and this codebase passes values
  through JSON, YAML and a serial protocol, which is where those errors live.

### 3.8 Security posture is honest but thin

Both of these are *documented as* weaknesses, which is the right first step, but
they remain:

- **No authentication on the web UI.** It can set high voltage and energise
  channels. The mitigation is the loopback bind — defensible for a lab PC, and
  argued explicitly in `api/__main__.py`, which warns loudly on a non-loopback
  `--host`. The residual risk is that anyone with a session on that PC (or any
  local process, or a browser visiting a malicious page that can reach
  `127.0.0.1:8000`) has full control authority. The operator cookie is
  `samesite="lax"`, which blocks cross-site form POSTs in current browsers — that is
  effectively the only CSRF defence, and it is incidental rather than chosen.
- **MQTT is unauthenticated and QoS 0.** Anything that can open `127.0.0.1:1883` can
  publish `xams/cmd/caen/vset`. Same loopback mitigation, same residual risk. QoS 0
  on the *audit* topic is the part I would revisit: `TOPIC_AUDIT` exists so a write
  record survives the database being down, but at QoS 0 a lost packet loses the
  audit record silently.
- **Secrets are a gitignored `config/secrets.yaml`** with no permission check and no
  environment-variable path. Fine on a single-user lab PC; worth `os.stat` asserting
  the mode is not world-readable, given the file holds an SMS gateway API key that
  costs money per message.

### 3.9 Reaching into another object's lock

`caen.py:680` and `caen.py:805` do `with reader._lock:` — the service grabbing the
reader's private lock. It is correct today (it is an `RLock` and the intent is to
hold the serial port across a write-then-read-back), but it is the kind of coupling
that breaks when someone changes `CaenChannelReader`'s internals. A public
`reader.transaction()` context manager says the same thing and is safe to rely on.

---

## 4. What I would do, in order

| # | Change | Effort | Why this order |
|---|---|---|---|
| 1 | Add GitHub Actions: `pytest` + `pyflakes` on push | hours | Everything else is easier to land safely once this exists. Would have caught the fixture break. |
| 2 | Alarm on dropped rows and buffer overflow; move `pg.stats` to INFO | hours | Closes the gap between principle 4 and the code. The mechanism is already there. |
| 3 | `send_and_await()` in `bus.py`, with a `cmd_id`; use it in the UI and all four CLI commands | 1 day | Fixes §3.4 and §3.5 together, deletes ~120 lines, lifts CLI coverage as a side effect. |
| 4 | Tests for `sinks/audit_writer.py`, `alarm_writer.py`, `flow_writer.py` | 1 day | 0 % on the audit trail is the coverage gap with the worst consequence. The `fake_pg` fixture for this already exists. |
| 5 | Loop `flush()` until drained or out of budget | hours | Turns a 33-minute recovery into seconds. |
| 6 | Pin dependencies: add upper bounds, commit a `requirements.lock` for the lab PC | hours | A reinstall should reproduce, not resolve. |
| 7 | Split `app.py`: `api/logview.py`, `api/recipients_form.py` | 1 day | Principle 1. Pure move, no behaviour change. |
| 8 | Smoke tests for the `__main__` wiring modules | 1–2 days | Import, construct, assert the subscriptions exist. Catches wiring bugs cheaply. |
| 9 | `reader.transaction()` replacing `reader._lock` | hours | Small, tidy, prevents a future break. |
| 10 | `os.stat` check on `secrets.yaml` mode at startup | hours | Cheap, and it holds a billable API key. |

Items 1, 2, 5, 6, 9 and 10 are each under a day and together address the two
findings that would actually cost data (§3.2, §3.3) plus the reproducibility gap.

---

## 5. Overall

This is well above the norm for lab instrument software. The architecture is sound
and — unusually — the code actually obeys it; the safety-critical path is thought
through rather than merely guarded; the documentation is both extensive and
self-checking; and the comments record reasoning rather than restating syntax. It is
plainly the work of someone who had watched a previous system fail on
maintainability and set out not to repeat it.

The weaknesses are of one kind: **the engineering discipline visible in the design
has not been extended to the delivery pipeline.** There is no CI, dependencies float,
the process entry points are untested, and the system's own "fail loudly" principle
is not applied to its own data-loss counters. None of that is architectural. All of
it is a week or two of unglamorous work, and the first two items on the list above
would remove most of the risk.

---

*Since this was written, the shared test scaffolding in §3.1 has been built and the
`[dev]` extra installed; the coverage figures below predate neither, since neither
changed them. Everything else stands.*

*Method: full read of `bus.py`, `config.py`, `service.py`, `pg_writer.py`,
`jsonl_writer.py`, `alarms/engine.py`, `api/app.py`, `api/state.py`,
`devices/caen.py` and `cli/xams_ctl.py`; structural survey of the rest; coverage
measured with `coverage run --source=src/xams_sc -m pytest` (525 passed).*
