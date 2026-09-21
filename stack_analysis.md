# XAMS Slow Control — stack analysis

**Second pass, 2026-09-21.** The first pass is in this file's history at
`32d0ab6`; this replaces it rather than annotating it, because enough has changed
that a marked-up version would be harder to read than a fresh one.

Twelve commits separate the two. §3 says what happened to each of the first
pass's findings, including the two I got wrong. §4 is new ground.

**Scope:** drivers, bus, storage, alarming, web UI, CLI, configuration, tests,
tooling. A review of the code as it stands, not of the specification.

---

## 1. What the stack is

Eight Python processes on one lab PC, joined by a loopback MQTT broker.

| Layer | Modules | LOC |
|---|---|---|
| Instrument drivers | `devices/{caen,cdaq,lakeshore,ups,derived,sim}.py` | ~2 400 |
| Service framework | `service.py`, `bus.py`, `model.py`, `scaling.py` | ~1 030 |
| Configuration | `config.py` + `config/*.yaml` | ~570 |
| Storage sinks | `sinks/*.py` | ~700 |
| Alarming | `alarms/*.py` | ~1 300 |
| Web UI | `api/{app,state,logview,recipients_form,mimic}.py` + 8 templates | ~1 800 |
| Operator CLI | `cli/xams_ctl.py` | ~900 |
| Tools | `tools/*.py`, `tools/*.ps1` | ~2 000 |

About 9 950 lines of Python under `src/`, 7 300 lines of tests across 31 files,
33 pages of documentation. 76 channels defined, 63 enabled, read at 1 Hz and
published as 10 s means.

| | First pass | Now |
|---|---:|---:|
| Tests | 488 (+1 silently skipped) | **644** |
| Coverage | 52 % | **61 %** |
| Modules at 0 % | 8 | **4** |
| Largest file | `app.py`, 1 259 lines | `app.py`, 1 019 |

---

## 2. Strong points

### 2.1 The bus boundary is real, not aspirational

`bus.py` is the only module that knows MQTT exists. The proof is in the history:
the audit writer, the alarm-state writer and the flow-period writer were each
added without touching a driver, and this session added a health watcher and a
shared command protocol the same way.

Two decisions are better than the obvious alternative. **Handlers are a list,
not a dict keyed by topic** — three consumers legitimately subscribe to
`xams/meas/#`, and a dict would have let the second silently displace the first.
**The outage buffer is bounded**, so a broker outage costs a recorded gap rather
than a memory leak; `PgWriter.max_pending` applies the same reasoning again.
Consistency of reasoning across modules is rarer than correctness in any one.

### 2.2 The HV write path is genuinely defensive

`caen.py:_handle_vset` is the safety-critical function, and it reads like
someone thought about it for a long time. Parse, resolve by name, require
`kind == "hv_vset"` and `enabled`, refuse in simulation, require a live reader,
check the range from `channels.yaml`, **read the board's status word and refuse
a non-zero setpoint on a channel that is not enabled**, then write and read back
before calling it successful.

The status-word check is the good one. Without it somebody could stage 4.2 kV on
a disabled channel, walk to the supply, flip the hand switch and get exactly the
unannounced ramp the design exists to prevent. The comment explains why it is
there, so the next person will not simplify it away.

The omissions are as good as the checks: nothing writes `MAXV`, `RUP`, `RDW`,
`TRIP` or `ISET`, and the channel enable stays a hand operation.

### 2.3 Configuration really is data

Adding a sensor is a YAML entry. `config.py` validates strictly at startup and
raises `ConfigError` rather than failing at the first bad read six months later.
A channel with no `limits` refuses every write — a range nobody wrote down is
not permission.

The config hash (SHA-256 over the normalised channels/devices/alarms, first 7
hex) goes into every JSONL day header, so archived data is self-describing.
`recipients.yaml` and `hv_defaults.yaml` are excluded because they do not affect
the data. That distinction is correct and documented.

### 2.4 The file archive is the source of truth

JSONL first, PostgreSQL as an index over it, `flush()` + `os.fsync()` at least
every 10 s. A database loss is a re-import, not a data loss. The two-writer
topology falls out for free.

### 2.5 The command path has one owner — and now one implementation

The UI never touches an instrument. It publishes to `xams/cmd/...` and waits for
an ack, so validation, read-back and the audit record happen in the one process
that owns the hardware. **A timeout is reported as failure, never as success.**

§10 used to be written out five times. It is now `AckInbox` and `CommandSession`
in `bus.py`, shared by the web UI and all four CLI verbs, with one timeout
instead of three that disagreed.

### 2.6 Silence is now evidence everywhere

All seven services publish a heartbeat and the `/status` page holds all seven to
the same 60 s window. The two that used to be exempt were the archive and the
notifier — the two the page could never show as broken.

The sinks say so when they discard data: `ERROR`, plus a `degraded` state that
reaches `/status`, plus a run summary at shutdown.

### 2.7 The comments explain *why*

Almost every non-obvious line carries the reason it exists, often with the
failure that motivated it. `operator_names()` spends fifteen lines on why the
"acting as" box offers suggestions rather than a closed list. `ups.py` records
the three routes tried to read the UPS and why two were rejected — including
"a channel that cannot detect the thing it monitors is worse than no channel",
which is a sentence most codebases would have replaced with the broken
approximation.

There are **zero** `TODO`, `FIXME`, `XXX` or `HACK` markers in `src/`.

### 2.8 The test scaffolding now exists

`tests/doubles.py` and `tests/conftest.py` hold one `RecordingBus`, one
`StubDrift` and one `FakePg`. The doubles mirror the real objects where it
matters, and three separate fidelity bugs were found and fixed this session by
tests that failed because a double was *easier* than the thing it replaced.
That is the scaffolding working.

---

## 3. The first pass's findings, and what happened to them

| | Finding | Status |
|---|---|---|
| 3.1 | Coverage 52 %, gaps in the wrong places | **partly closed** — 61 %, sinks and CLI covered; four modules still at 0 % |
| 3.2 | Dropped rows logged at DEBUG | **closed** — `ERROR` + `degraded` + run summary |
| 3.3 | 33-minute backlog drain | **closed** — `drain()` clears in one cycle |
| 3.4 | Acks had no correlation id | **closed**, differently — see below |
| 3.5 | CLI reimplemented the ack protocol | **closed** — `CommandSession` |
| 3.6 | `app.py` 1 259 lines, four concerns | **closed** — three modules out, 1 019 lines |
| 3.7 | No CI, no lockfile, no type checking | **open**, and downgraded — see below |
| 3.8 | Security posture thin | **open**, unchanged and accepted |
| 3.9 | `reader._lock` reached into from outside | **closed** — `transaction()`, and it was four sites, not two |
| 3.10 | Services card blind to `sinks`/`alarms`; pyflakes guard inert | **closed** — found mid-work, not by either review |

Three of these deserve more than a row.

### 3.9 — trivial, and it was hiding something

Two sites in `caen.py` reached into `reader._lock`. Closing it found **two more
in `lakeshore.py`** doing the same, and something better: both test doubles —
`FakeReader` and `FakeDevice` — carried a `_lock` attribute of their own, for no
reason except that production code reached in and took it. A double forced to
mirror another object's PRIVATE state is the clearest possible evidence that the
coupling is real rather than theoretical.

`transaction()` is now a public context manager on both readers, and the two
doubles implement it as an interface instead of imitating an attribute.

### 3.10 — two more, found by doing the work rather than by reading

Neither was visible in the first pass. Both are recorded because the *way* they
were found is the point.

**The Services card could not show the archive or the notifier as broken.** The
dot was chosen on `expects_heartbeat` before `state` was ever consulted, and
`sinks` and `alarms` were exempt because neither is a `BaseService` and neither
published a heartbeat. A stopped `sinks` rendered as a grey dot beside the word
"stopped" in dim text — indistinguishable at a glance from healthy. Worse, with
no heartbeat there was no staleness signal: a process that **hangs** rather than
exits publishes no will, so the retained `running` would have stayed next to its
name indefinitely. That is the frozen-plausible-value failure principle 4 names,
in the two services it would hurt most.

Found by the owner asking why a stopped service did not say so. Closed: both now
beat every 10 s, `expects_heartbeat` is gone rather than made always-true, and
with it the "no heartbeat" cell that read as a fault on a healthy system.

**The pyflakes guard had been skipping silently.** `test_no_undefined_names.py`
is an `importorskip`, pyflakes was not installed in the checkout's virtualenv,
and the suite reported "488 passed, 1 skipped" — a line nobody reads. Installing
the `[dev]` extra turned it back on and took the suite to 525. A skipped safety
check reports the same green as a passing one, which is §3.7's argument in
miniature and is the second time this session that "it works on this machine"
turned out to mean "this machine happens to have something installed".

### 3.4 — the fix was cheaper than the diagnosis

I proposed a correlation id echoed by every service: eight publish sites across
three services that talk to hardware. Reading the code showed the discriminator
was **already on the wire** — `channel` on every CAEN ack, `output` on every
Lake Shore one. Matching on what is already there cost one method and four call
sites, and no instrument service changed.

The bug was worse than described. Reproduced against the old code:

```
alice  asked for hv_cathode_vset    was shown hv_anode_vset
bob    asked for hv_anode_vset      was shown None
```

Alice got Bob's channel *and his voltage*; Bob got a spurious timeout, because
`command` cleared the whole ack slot on its way past and threw away an ack
belonging to somebody else.

### 3.7 — real, but I over-weighted it

I ranked CI first. The owner's objection was fair: he codes with an assistant
that runs the suite every time, so CI-as-bug-catcher is largely redundant.

What survives the objection is narrower and worth keeping: **CI runs on a
different machine with a fresh install**, and that catches a class nothing else
here can. It found one immediately. `pip install -e .[api]` produced a web UI
that died at import, because `jinja2` was undeclared — present only because
mkdocs pulls it in, so a machine that had built the manual worked and one that
had not did not. That was live on `origin/main` and would have hit the next
lab-PC rebuild. Now declared, along with `pyserial` for the test suite.

So: CI's job here is proving the install works, not proving the code works.
Fifteen lines, low priority, not first.

### 3.8 — unchanged, and that is a decision

No authentication on a UI that can set high voltage; unauthenticated MQTT on
which commands travel; `secrets.yaml` with no permission check. All three are
mitigated by the loopback bind and argued explicitly in the code. The residual
risk is that any local process, or a browser visiting a page that can reach
`127.0.0.1:8000`, has full control authority. The operator cookie is
`samesite="lax"`, which is the only CSRF defence and is incidental rather than
chosen.

For a single-user lab PC this is defensible. It is recorded here so it stays a
decision rather than becoming an assumption.

---

## 4. New findings

### 4.1 Three sinks block the bus thread on a dead database

`AlarmWriter`, `AuditWriter` and `FlowPeriodWriter` call
`psycopg.connect(connect_timeout=5)` **inside the MQTT callback**. All five
writers share one `Bus`, so one paho client, so one callback thread, and
`_on_message` dispatches handlers sequentially. `PgWriter` does not do this — it
queues in the handler and writes on a pump thread.

Observed live during a deliberate outage, five seconds apart to the millisecond:

```
09:02:34,727  could not record alarm for ttamb: connection timeout expired
09:02:39,831  could not record alarm for tt302: ...
09:02:44,926  could not record alarm for p101: ...
```

While the database is down, each of those burns five seconds of the only thread
that also feeds the JSONL archive.

**But the trigger is narrow, and I initially overstated this.** `AlarmWriter`
checks for a repeat *before* connecting, so retained re-deliveries cost nothing;
only genuine transitions connect. Audit records happen when somebody writes to
an instrument. The burst above was a **startup artefact** — a fresh subscriber
receives every retained alarm topic at once and each looks like a first-time
transition.

Worst case I can construct: an alarm cascade during a database outage, twenty
channels, ~100 s of stalled archiving. Measurements queue in paho rather than
vanishing; at ~6 msg/s you would need ~160 s of continuous stalling to reach
mosquitto's default 1 000-message queue limit for a slow QoS 0 consumer.

**Not urgent.** Recorded as a known characteristic. If a long outage with active
alarms ever happens, the fix is a reconnect backoff (an hour) or matching
`PgWriter`'s queue-and-pump (half a day).

### 4.2 `max_pending` is unreachable and untunable

Hard-coded at 100 000, constructed as `PgWriter(bus, dsn)` with no override. At
~6 rows/s that is **4.4 hours** before a single row is dropped — so the §3.2
work cannot be demonstrated, and on a machine with less RAM the size of that
buffer is a decision nobody can make. A `--max-pending` argument would fix both.

### 4.3 The archive is not fsynced at the day rollover

`jsonl_writer._ensure_file` closes the previous day's file without syncing it.
`close()` flushes userspace to the OS but does not fsync, so a power loss shortly
after midnight can lose up to 10 s of the previous day — in the file the design
calls the truth, and whose own docstring promises "a power loss costs seconds,
not hours". One line: sync before close.

### 4.4 The integrator's state file is not durably written

`IntegratorState.save` writes a temp file and `replace`s it — atomic against a
crash mid-write, which is the main risk and is handled. Neither the temp file
nor the directory is fsynced, so a power loss immediately after can still leave
the old or a zero-length file. This is the **only stateful service**, and the
total is the thing a restart must not lose.

### 4.5 `next_log` catches up after a stall

`service.py:_loop` advances `next_log += self.log_interval_s` where the read
schedule uses `next_read = now + self.interval_s`. After a stall longer than the
log interval, the log branch fires repeatedly to catch up, emitting a burst of
heartbeats. Harmless — `_emit_window` clears the window, so only the first
carries data — but inconsistent with the line above it. A nit.

### 4.6 The database outage path cannot be exercised through the service manager

`install_services.ps1` sets `DependOnService = mosquitto, postgresql-x64-18` for
`XAMS-sinks`. The reasoning given is sound — starting before the database is up
is a minute of retry noise. The consequence is not stated anywhere:

  * `Stop-Service postgresql-x64-18 -Force` stops **`XAMS-sinks` with it**, so
    the service that is supposed to survive a database outage is not running to
    survive it;
  * `Start-Service XAMS-sinks` while the database is down is **refused** by
    Windows.

So the one failure mode the storage layer is explicitly designed for cannot be
rehearsed using the tools that run it in production. Testing it means running
`python -m xams_sc.sinks` by hand, which is what was done to observe §4.1 — and
which nobody would think of without reading the NSSM script.

Worth a paragraph in `docs/operating/`. A dependency that also prevents the
recovery drill is a reasonable trade, but only if it is a known one.

### 4.7 `ups.py` is 0 % covered and trivially testable

142 statements, no tests, and it is the mains-loss detector. The hardware is
behind a `UpsReader` the service holds, so `read()` can be tested against a fake
in an afternoon. The two-signal derivation of `on_battery` and the `None` start
for `_last_on_battery` — which correctly suppresses a spurious "back on line
power" on the first reading — are exactly the logic worth pinning.

---

## 5. What I would do next

| # | Change | Effort | Why |
|---|---|---|---|
| 1 | `--max-pending` on the sinks | hours | Makes §3.2 demonstrable; makes the buffer a decision |
| 2 | fsync at the day rollover and in `IntegratorState.save` | hours | Two one-line durability gaps in the two files that must survive |
| 3 | Tests for `ups.py` (§4.7) | half a day | 0 % on mains-loss detection, easy to reach |
| 4 | Pin dependencies; commit a lockfile for the lab PC | hours | A reinstall should reproduce, not resolve |
| 5 | CI: `pytest` + `pyflakes` on a clean box | hours | Proves the install, and that the guards are installed to run (§3.10) |
| 6 | Tests for `alarms/__main__.py` and `daily.py` | 1 day | The last two meaningful 0 % modules |
| 7 | `os.stat` check on `secrets.yaml` mode | hours | It holds a billable API key |
| 8 | Document the sinks/PostgreSQL service dependency (§4.6) | minutes | The recovery drill is otherwise undiscoverable |

Items 1–3 are half a week together and close the last two places where this
system can lose data quietly.

---

## 6. Overall

This remains well above the norm for lab instrument software, and the gap has
widened. The architecture was sound and the code obeyed it; what has changed is
that the delivery discipline now matches. The test scaffolding exists, the
storage path is covered, the command protocol has one implementation, and the
two counters that record data loss are wired to something a person will see.

The findings that remain are small and specific: two missing `fsync` calls, an
untunable constant, a module that nobody has tested, and a set of security
trade-offs that are recorded and accepted rather than overlooked. None is
architectural. None would take more than a day.

Two patterns are worth naming, because each recurred.

The first: **three times this session a
test failed because a double was easier than the object it stood in for** — a
bus that connected synchronously when the real one does not, acks that omitted a
field every real ack carries, a fixture that patched `_connect` but not `_conn`.
Each was fixed in the double rather than worked around in the test. A suite whose
doubles lag the real objects certifies behaviour nothing has, and that is a
harder failure to notice than a red test.

The second: **the findings that mattered most came from running the thing, not
from reading it.** The Services card blind spot surfaced because somebody
stopped a service and looked at the page. §4.1 surfaced because somebody pulled
the database out while watching the log. The undeclared `jinja2` surfaced
because an install was attempted somewhere clean. None of the three was visible
in a careful read of the source, and the first review pass — which was a careful
read of the source — missed all of them.

---

*Method: full read of `bus.py`, `config.py`, `service.py`, `pg_writer.py`,
`jsonl_writer.py`, `alarm_writer.py`, `audit_writer.py`, `flow_writer.py`,
`sinks/__main__.py`, `alarms/engine.py`, `api/app.py`, `api/state.py`,
`devices/caen.py`, `devices/ups.py`, `devices/derived.py`, `alarms/daily.py` and
`cli/xams_ctl.py`; structural survey of the rest; coverage measured with
`coverage run --source=src/xams_sc -m pytest` (644 passed). Live observation of a
deliberate PostgreSQL outage on the production machine informed §4.1.*
