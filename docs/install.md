# Installation

**This page is for someone reinstalling the system from scratch.** To run a
system that is already installed, see [Operating](operating/index.md).

## 1. Python

Python 3.11 or newer. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Add `.[hardware]` for the device drivers (`nidaqmx`, `pyserial`, `lakeshore`)
from milestone 2 onward. Those need the NI runtime and are not required for
simulation.

Check that the configuration is valid — this starts nothing:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl check
```

## 2. Mosquitto, PostgreSQL and Grafana

One script does all three, in an **elevated** PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\tools\setup_services.ps1
```

It asks for two passwords (the `postgres` superuser, and one for the `xams`
role it creates), then downloads and installs the three services, binds all of
them to loopback, creates the database, applies `sql/schema.sql`, writes
`config/secrets.yaml`, and verifies that every port is listening on `127.0.0.1`
**and nowhere else**.

It is idempotent — if a step fails, fix the cause and run it again. Roughly
870 MB of downloads, staged in `tools/installers/` (gitignored) so a re-run
does not fetch them twice.

**Why the loopback binding matters.** Control commands travel over MQTT
(`xams/cmd/#`), so anything that can reach the broker can set a high voltage. A
broker listening on `0.0.0.0` — the default in most tutorials — is an
unauthenticated control interface on the building network. If remote viewing is
ever wanted, the answer is an SSH tunnel or a read-only mirror, never opening
the port (§8).

**What the script does not do:** install the XAMS services themselves as
Windows services. Auto-start stays off while LabVIEW is the fallback, because
every device admits only one process and a service starting at boot would lock
LabVIEW out (§12).

## 3. Grafana first login

<http://127.0.0.1:3000>, `admin` / `admin`, and change the password when asked.

The **XAMS** datasource and the **XAMS Overview** dashboard are provisioned
from this repository. Dashboards live in `grafana/dashboards/*.json`, in git,
and the file is authoritative: edit a dashboard in the UI to get it right, then
export the JSON and commit it. A dashboard that exists only in Grafana's own
database is lost when that database is.

## 4. Windows services

The steps above leave the services running as plain processes, which **would
not survive a reboot**. Installing them properly is the first entry in
[what still needs doing](status.md).

## 5. The manual

This documentation is served by the web UI itself, at
<http://127.0.0.1:8000/manual>, so it is present on the lab PC whether or not
the building network is. `install_services.ps1` builds it. By hand:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[docs]"
.\.venv\Scripts\python.exe tools\build_docs.py
```
