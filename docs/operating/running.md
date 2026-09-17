# Starting and stopping

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl start
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl status
```

`start` brings up the sinks and the cDAQ service. **`stop` releases all
hardware for LabVIEW** — do that before starting LabVIEW, and stop LabVIEW
before starting these, because every device admits only one process.

For development with no hardware attached, the simulated service publishes
every enabled channel instead:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.devices sim
```

Do not run `sim` and `cdaq` together: they publish the same channel names and
would overwrite each other.

Then open <http://127.0.0.1:3000> → **XAMS Overview**.

Then open the web UI at <http://127.0.0.1:8000>:

| Page | What it answers |
|---|---|
| `/` | is everything all right? — alarms, services, UPS, integrated flow, known faults |
| `/status` | every channel: value, unit, age, quality, alarm |
| `/hv` | both CAEN supplies: VMON, IMON, on/off, and the board's own protection settings |
| `/mimic` | the P&I drawing with live values on it |
| `/logs` | the last lines of any service log |

It reads the **retained MQTT topics, never the database** — the status view has
to work when PostgreSQL does not, which is exactly when it is needed (§8.1).

To watch everything crossing the bus, with no UI and no database involved:

```powershell
& "$env:ProgramFiles\mosquitto\mosquitto_sub.exe" -t "xams/#" -v
```

Full day-to-day instructions, including what to do when something breaks, are
in [Operating](index.md).
