# Project status

Where the work has got to, and what is outstanding. Each milestone has an
acceptance criterion in [the design specification §15](DESIGN.md); do not start
the next before the current one passes.

## Milestones

| # | Milestone | State |
|---|---|---|
| 1 | Skeleton: config, bus, sinks, simulation, install | **done** |
| 2 | cDAQ read-only | **done** |
| 3 | Channel verification against physical sensors | **partial** — see below |
| 4 | Scaling and history import | **done** |
| 5 | Lake Shore + CAEN monitoring | **done** |
| 6 | UPS, alarms, flow integrator | **done** |
| 7 | Web UI and P&ID mimic | **mostly done** |
| 8 | Control path | |
| 9 | Procedures | |
| 10 | Production | |

Each has an acceptance criterion in [the design specification §15](DESIGN.md). Do
not start the next before the current one passes.

**Milestone 1 acceptance, met 17 September 2026:** the simulated service
publishes, both sinks store independently, Grafana plots from the provisioned
dashboard, and the install is scripted and reproducible.

**Milestone 2 acceptance, met 17 September 2026:** all 20 connected channels
(6 voltage + 14 RTD) read and logged with LabVIEW stopped, values plausible
(`ttamb` 24.2 °C, cryostat −90 °C, `pmain` 1.495 against LabVIEW's 1.53), and
readings continuous across a service restart to better than 0.7%.

**Milestone 3, partially met 17 September 2026.** Every channel's physical
location is recorded in `channels.yaml`, and the 1xx / 2xx / 3xx series are now
known to mean xenon circulation, detector vessel and bucket, and cooling and
heat exchange respectively (DESIGN.md §3). Readings were checked against the
LabVIEW project and agree.

**The empirical per-sensor check is still outstanding.** Agreement with LabVIEW
confirms we read the same hardware the same way — but it cannot catch a tag
that was already on the wrong channel *in LabVIEW*, because we would inherit
the error and the comparison would agree perfectly. That is the specific
failure §15 says this milestone exists to catch, so it is not yet closed.

**Milestone 4 acceptance, met 17 September 2026.** The column-count census of
§9.6 was run over all 733 log files, one week of history was imported
(6.3 million readings, tagged `src="labview"`), and every channel present in
both systems agrees across the changeover:

```
19 of 19 channels agree, within 0.4%
  pmain    LabVIEW 1.495   XAMS 1.495   -0.03%
  fm101    LabVIEW 7.136   XAMS 7.127   -0.13%
  tt206    LabVIEW -90.317 XAMS -90.460  0.16%
```

Reproduce with `python tools/compare_to_labview.py`.

The largest differences are the cryostat sensors, all drifting the same way by
about 0.2 °C across the 4.8-minute changeover gap — consistent with slow
cooling, not with a scaling error.

**Milestone 5 acceptance, met 17 September 2026.** Both CAEN supplies and the
Lake Shore are read, identity-verified and logged:

```
  2 candidate ports for 21E1:0003: COM4, COM5
  COM4 reports DT1470ET / 19198    -> hv_1
  COM5 reports DT1470ET / 79       -> hv_2
  COM6 reports MODEL335 / 335A12T  -> lakeshore
```

**Unplug test passes.** With the link cut under a running service: readings are
published `quality=error`, the service goes `degraded` and stays alive, and
after two failures it re-enumerates the ports and re-asks every board for its
serial before resuming. A reconnect re-verifies identity rather than merely
reopening the port — a swapped cable during an outage would otherwise resume
under the wrong name (§6.2 rule 6).

Two things the instruments confirmed that the design had recorded from the old
system: the Lake Shore PID is **100, 20, 0**, exactly as §7.3 says, and all
eight HV channels are `DISABLED` with the protection settings in `devices.yaml`
matching the boards.

**Milestone 6 acceptance, met 17 September 2026.**

- **Thresholds** from `alarms.yaml` on `pmain`, `tt104`, `tt302` and three UPS
  channels. Editing one and running `xams-ctl reload` applies it live.
- **Staleness alarms fire.** Proven twice on unplanned faults, not just tests:
  a service that died from a bad config deploy was reported 60 s later, and a
  channel rename produced a staleness alarm for the old name.
- **Flight-recorder dumps** are written on every alarm, holding the ten
  minutes of full-rate data leading up to it.
- **SMS delivered** — one real message through the existing MessageBird
  gateway. Email is not yet configured (no `smtp_host`).
- **The integrator survives a restart** and records gaps rather than
  inventing flow across them. A reset closes a period into `flow_periods`
  with its total, its gaps and who did it, and is written to the audit log.

The UPS is read from its HID **alongside PowerChute**, which keeps doing its
safe-shutdown job. Two other routes were tried and rejected: WMI reports no
battery at all, and `GetSystemPowerStatus` describes the wall socket rather
than the UPS — it would have read "on line power" while running on battery.

**Milestone 7, 17 September 2026.** Web UI on `127.0.0.1:8000` with the five
pages above, and the P&I mimic built from the drawing by
`tools/build_mimic.py`: rotated to landscape, frame and title block removed,
recoloured for the dark interface, with a live value in each instrument bubble.
A **stale channel greys out and shows a dash, never its last number** — a
frozen value displayed as though it were live is the failure this kind of page
exists to avoid. The SVG-vs-`channels.yaml` tag check runs at startup and
reports drift in both directions.

Still outstanding for this milestone: editing `recipients.yaml` from the UI,
and the Python client for notebooks.

**The pressure units are now known: bar**, supplied by the group along with
each transmitter's location. **No channel carries `unit: TBD` any more.** They
were never guessed — the milestone 4 comparison validated the *numbers* to
0.03% while leaving the *labels* open, which is the distinction the `fm101`
"SLPM" story exists to make (§4.2).

The verification found one fault: **the `tt202` sensor has failed** (bottom of
the detector vessel). It is `enabled: false` pending replacement, so the cDAQ
carries **19 live channels, not 20**.

## What still needs doing

**One action, now:** the services run as plain processes and **would not
survive a reboot**. To install them as Windows services that start at boot and
restart on crash, in an elevated PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\tools\install_services.ps1
```

`-Manual` installs them with restart-on-crash but not at boot; `-Uninstall`
reverses it. Afterwards **`xams-ctl stop --for-labview`** is what hands the
hardware back: it stops the services *and* suspends auto-start, so a Windows
Update reboot at three in the morning does not quietly reclaim the
instruments.

Then, roughly in order of how much it matters:

| | Why it matters |
|---|---|
| **Email delivery** — set `smtp_host` in `config/secrets.yaml` | SMS works and has been proven with a real message; email has never been sent. Try the Nikhef relay with no credentials first. |
| **The Nikhef VM watchdog** — one Grafana rule, "no measurement for 15 minutes" (§12) | The one failure this system cannot report is its own machine being off. Auto-start makes that *less* likely to be noticed, not more, because the system now looks after itself well enough that nobody checks. |
| **Replace the `tt202` sensor** | Then `enabled: true` in `channels.yaml` and `xams-ctl reload`. Nothing else changes. |
| **Rotate the MessageBird key** | It sat in plaintext in three copies on the Desktop for years. Update `secrets.yaml` and the LabVIEW scripts together while LabVIEW is still the fallback. |
| **Alarm thresholds for the remaining channels, and the response to each** | A threshold without a prescribed action is half the information (§14). [Operating](operating/index.md) has the section waiting. |
| **Milestone 3's empirical check** | Readings agree with LabVIEW, which cannot catch a tag that was already on the wrong channel *in* LabVIEW. Warming one sensor at a time and watching which value moves is the only thing that can. |
