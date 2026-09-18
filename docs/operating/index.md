# Operating

**How to run the system, and what to do when it misbehaves.** Not architecture
— that is [the design specification](../DESIGN.md). Not installation from
scratch — that is [Installation](../install.md).

This document **grows with each milestone** (§14). It is deliberately written
during construction rather than afterwards: written at the end it would record
what the author remembers rather than what a reader does not understand.

**The acceptance test for this document** (§14): a colleague, using these pages
alone, can stop the system, add a channel and start it again — without asking
anyone. If that fails, the document is unfinished however thorough it looks.

| | |
|---|---|
| The interface you use every day, tab by tab | [The web interface](webui.md) |
| Starting, stopping, handing the hardware back | [Starting and stopping](running.md) |
| Adding a channel, importing history, saving a dashboard | [Routine tasks](tasks.md) |
| Something is broken | [Troubleshooting](troubleshooting.md) |
| Backups | [Backup and restore](backup.md) |
| Alarm mail and the daily report | [Email and reports](email.md) |
| Taped to the rack | [Emergency card](emergency.md) |

---

## Every day

Open <http://127.0.0.1:8000>. The badge in the header is the whole system in one
word; the Overview page is the detail behind it. What each tab shows is in
[The web interface](webui.md).

The same picture from a terminal, in
`C:\Users\localadmin\Documents\XAMS-SC`:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl status
```

This shows each service, whether it is running, and how long ago it last sent a
heartbeat. Heartbeat ages come from the **retained MQTT topics, not the
database** — the status view works even when PostgreSQL does not, which is
precisely when it is needed.

For history and plots: Grafana on <http://127.0.0.1:3000>, the **XAMS Overview**
dashboard.

### What healthy looks like

- every service running, every heartbeat a few seconds old;
- the **Channels not reading OK** card absent, and Grafana's equivalent panel
  **empty** — that emptiness is the point, because a frozen plausible value is
  worse than a gap;
- no active alarms, or only ones somebody is already dealing with;
- backup `ok` and dashboards `saved to git` on the Overview page.

---

## What this system will and will not do to the hardware

**One instrument is writable: the Lake Shore heater** — setpoint and heater
range, on output 1, from [the Overview page](webui.md#the-controls) or over the
bus. Every write is range-checked against `channels.yaml`, read back from the
instrument, acknowledged and audited.

**Nothing else actuates.** The CAEN supplies are read with `CMD:MON` only and
there is deliberately no code that can write a setpoint; the cDAQ chassis has no
output module; the UPS is read, not commanded. There is no automatic actuation
anywhere — the decided heater cut on a `pmain` hihi (§10) is specified but not
built, and `KILL VOLTAGE` remains open. Named procedures are milestone 9.

**This is not a protection system.** Interlocks belong in hardware and limits
belong in the instrument, which the software verifies at startup and alarms on,
but does not write (§10 rules 1–2).

---

## What each alarm means

The thresholds live in `config\alarms.yaml` and what fires is described in
[the alarm engine](../software/alarms.md); who gets told, and how, is in
[Email and reports](email.md).

**Still missing: the prescribed response to each threshold.** The numbers of the
old LabVIEW system exist in the VIs, but the response to each exists only in
people's heads, and a threshold without a prescribed action is half the
information (§14). Filling that in, channel by channel, is outstanding work —
see [Project status](../status.md).

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
| Web UI | <http://127.0.0.1:8000> |

**Everything binds to loopback, deliberately.** Commands travel over MQTT, so
anything that can reach the broker can change a setpoint. If remote viewing is
wanted, the answer is an SSH tunnel or a read-only mirror — never opening the
control port (§8).
