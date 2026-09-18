# Starting and stopping

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl start
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl stop
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl restart
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl status
```

`start` brings up the sinks, the cDAQ, both CAEN supplies, the Lake Shore, the
UPS, the derived channels, the alarm engine and the web UI.

Services start in a fixed order and stop in reverse, so the sinks outlive the
producers and the last measurements are archived rather than dropped.

Then open the [web interface](webui.md) on <http://127.0.0.1:8000>, and
Grafana on <http://127.0.0.1:3000> → **XAMS Overview**.

## Handing the hardware back to LabVIEW

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl stop
```

**This matters while LabVIEW is still the fallback.** Every device admits only
one process: the cDAQ, each CAEN supply and the Lake Shore can each be held by
exactly one program. If our services are running, LabVIEW cannot start, and
vice versa. Stop LabVIEW before starting these, and these before starting
LabVIEW.

This is also why **auto-start stays off** until LabVIEW is retired (§12). A
service that starts after an overnight Windows update would claim the hardware
and lock LabVIEW out with nobody present to notice. Once the services are
installed to start at boot, **`xams-ctl stop --for-labview`** is what hands the
hardware back: it stops the services *and* suspends auto-start.

Rolling all the way back to LabVIEW is in [Routine tasks](tasks.md).

## Running one service in the foreground

To watch a service directly — this is how you debug one:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.devices sim
.\.venv\Scripts\python.exe -m xams_sc.sinks
```

Ctrl-C stops it cleanly. Logs go to `logs\<service>.log` either way.

For development with no hardware attached, the simulated service publishes
every enabled channel instead:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.devices sim
```

Do not run `sim` and `cdaq` together: they publish the same channel names and
would overwrite each other.

To watch everything crossing the bus, with no UI and no database involved:

```powershell
& "$env:ProgramFiles\mosquitto\mosquitto_sub.exe" -t "xams/#" -v
```
