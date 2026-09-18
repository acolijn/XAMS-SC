# Installation

**This page is for someone reinstalling the system from scratch.** To run a
system that is already installed, see [Operating](operating/index.md).

## 1. Python

Python 3.11 or newer. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Add `.[hardware]` for the device drivers (`nidaqmx`, `pyserial`, `lakeshore`).
`nidaqmx` is only a wrapper — it needs the NI-DAQmx driver from §2, and
**installing the Python package does not install the driver**. The serial
instruments need no vendor driver: the CAEN supplies and the Lake Shore
enumerate through Windows' generic `usbser.sys`, and the UPS through HID.

Everything except the cDAQ works without §2. The simulated service publishes
every enabled channel, and the sinks, alarms, web UI and Grafana run on it
unchanged — which is exactly why a missing NI-DAQmx is not noticed until
`xams-ctl start` reaches the cDAQ service.

Check that the configuration is valid — this starts nothing:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl check
```

## 2. NI-DAQmx, and the module aliases

**This is the one step that is not scripted, and the one most likely to be
missed.** The lab PC has had NI-DAQmx installed since before this project
existed, because LabVIEW ran on it — so on that machine this section has never
been performed by anyone. On a clean machine it must be.

Two separate things, and only the second comes from pip:

| | |
|---|---|
| **NI-DAQmx** | the driver: `nicaiu.dll`, NI-MAX, Windows services. A download from NI |
| **`nidaqmx`** | the Python wrapper, installed by `.[hardware]` in §1 |

### 2.1 The driver

Free of charge but **not freely available**: closed source, and the download is
behind an ni.com account. It cannot be scripted the way §3 scripts Mosquitto
and Grafana, because there is a login in the way. Installed through NI Package
Manager.

!!! danger "NI-DAQmx **24.5.0** — install this version, not “the latest”"

    Read from the lab PC on 18 September 2026. This is the version that was
    demonstrably driving the cDAQ-9174, including
    `ni-daqmx-cdaq-firmware 24.5.0`.

    Record the number rather than "the latest", for the ordinary reason: it is
    the version this system has been run and verified against, and a driver
    upgrade is a change worth making deliberately rather than by accident.

    On 18 September 2026 NI’s download page offered **26.5.0**, not 24.5.0.
    26.5.0 has not been tried against this hardware, so upgrading is a thing to
    do on purpose, with the cDAQ service watched afterwards — not a thing to do
    while rebuilding a dead machine.

    !!! note "Correction, 18 September 2026"

        An earlier version of this page called the cDAQ-9174 a **discontinued
        chassis**. **That was wrong.** NI lists it as *Active* and sells it
        (about €1,680). The claim went unchecked into this page and then into
        [Backup and restore](operating/backup.md), where it was used to argue
        that the driver might become unobtainable. It might not. The archived
        copy is still worth having — see there for the honest reason.

**The driver is archived on the cluster**, at
`/data/xenon/xams_slow_control/software/` (see
[Backup and restore](operating/backup.md)):

| | |
|---|---|
| `nipm-package-cache-2026-09-18.tar` | the whole NI Package Manager cache from the lab PC, ~6.8 GB, **containing NI-DAQmx 24.5.0** |
| `ni-daqmx_26.5.0_offline.iso` | NI’s current offline installer, 3.2 GB, version **not** verified against the 9174 |

A rebuild therefore does not depend on someone remembering an ni.com password,
on the account still existing, on NI still publishing 24.5.0, or on the lab PC
reaching the internet at all.

Two honest caveats, both recorded in the README beside those files: the cache
needs **NI Package Manager itself**, which is not archived because it was not
present on the lab PC as a standalone installer; and **nobody has yet installed
24.5.0 from that cache onto a clean machine.** It is very likely to work, and
that is not the same as knowing.

**Do not run the hardware services under WSL2.** USB passthrough to WSL2 does
not work with NI-DAQmx. Anything that touches an instrument runs natively on
Windows; see [the decision document](OPTIONS.md).

### 2.2 The aliases — do not skip this

This system addresses cDAQ modules by **alias**, never by slot, so that a
module moved between slots is harmless. Aliases are set in NI-MAX and stored in
the **host's** configuration database — *not* on the chassis. A fresh Windows
install therefore has none of them, however untouched the hardware is, and the
cDAQ service refuses to start with:

```
FATAL: module alias '9207' not found in NI-MAX. Aliases are configured
there and this service addresses modules by alias, never by slot.
```

Correct behaviour, and baffling if you do not know why. In NI-MAX, rename each
module to the alias below. The serials are authoritative — they are what
`config/devices.yaml` checks at every startup, and a mismatch is fatal by
design:

| Alias | Model | Slot | Serial |
|---|---|---|---|
| `9207` | NI 9207 | 1 | `020DFD57` |
| `9216_1` | NI 9216 | 2 | `020F64D1` |
| `9216_2` | NI 9216 | 3 | `020F64D0` |
| `9226` | NI 9226 | 4 | `02159CBB` |

The chassis itself must appear as `cDAQ1`, serial `020C5E1C`.

If a module has genuinely been replaced, the new serial goes into
`config/devices.yaml` as a deliberate, committed change. Never edit it to make
an error go away.

### 2.3 Check it

The identity check runs at **service startup**, so the way to verify this
section is to start the cDAQ service and read the first few log lines. There is
no one-shot mode.

That needs the broker, so it has to wait until §3 is done:

```powershell
.\.venv\Scripts\python.exe -m xams_sc.devices cdaq
```

A good start logs the chassis serial and then all four module serials as
confirmed, before any reading is published. Ctrl-C once you have seen them.
Any `FATAL:` line names exactly what disagrees — a missing alias, or a serial
that does not match `config/devices.yaml`.

!!! warning "Fill this in at the lab PC"

    **TODO(lab PC):** paste a known-good startup log here — the chassis line
    and the four module lines. A recorded good output is what makes this
    section checkable rather than merely readable, and it is the thing you
    compare against at two in the morning.

## 3. Mosquitto, PostgreSQL and Grafana

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

## 4. Grafana first login

<http://127.0.0.1:3000>, `admin` / `admin`, and change the password when asked.

The **XAMS** datasource is provisioned from this repository. **Dashboards are
not** — provisioning refuses every UI save, so Grafana owns the live dashboard
and `grafana/dashboards-archive/` is the copy git tracks. On a fresh install,
load them:

```powershell
.\.venv\Scripts\python.exe tools\save_dashboard.py --load --password <admin pw>
```

Afterwards the direction reverses: edit in the UI, then
`tools\save_dashboard.py --save` and commit. A dashboard that exists only in
Grafana's own database is lost when that database is — see
[Grafana](grafana/index.md).

## 5. Windows services

The steps above leave the services running as plain processes, which **would
not survive a reboot**. Installing them properly is the first entry in
[what still needs doing](status.md).

## 6. The manual

This documentation is served by the web UI itself, at
<http://127.0.0.1:8000/manual>, so it is present on the lab PC whether or not
the building network is. `install_services.ps1` builds it. By hand:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[docs]"
.\.venv\Scripts\python.exe tools\build_docs.py
```
