"""xams-ctl — start, stop, check and reload the services. See DESIGN.md §12.

One command, correct order, every time.

    xams-ctl start | stop | restart | status | reload | check

`stop` releases all hardware for LabVIEW, which matters while LabVIEW is still
the fallback: every device admits only one process.

Milestone 1 runs the services as plain processes. From the trial phase they
run under NSSM as Windows services (§12), and this command drives those
instead — which is why the verbs are chosen to match.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from ..config import CONFIG_DIR, ConfigError, load

# Started in this order; stopped in reverse, so sinks outlive the producers
# and the last measurements are archived rather than dropped.
#
# `sim` is deliberately NOT here. It publishes every enabled channel, including
# the ones the cdaq service owns, so running both means two producers writing
# the same channel names and the archive recording whichever arrived last. Run
# it by hand for development on a machine with no hardware:
#
#     python -m xams_sc.devices sim
#
# Real drivers join this list as milestone 6 is built (ups).
SERVICES = ["sinks", "cdaq", "caen", "lakeshore"]

PID_DIR = Path("logs")


def _pid_file(name: str) -> Path:
    return PID_DIR / f"{name}.pid"


def _running(name: str) -> int | None:
    """Return the pid if the service looks alive, else None."""
    f = _pid_file(name)
    if not f.exists():
        return None
    try:
        pid = int(f.read_text().strip())
    except (ValueError, OSError):
        return None

    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return pid if str(pid) in out else None
    except Exception:
        return None


def _command_for(name: str) -> list[str]:
    if name == "sinks":
        return [sys.executable, "-m", "xams_sc.sinks"]
    return [sys.executable, "-m", "xams_sc.devices", name]


def cmd_start(args) -> int:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    for name in SERVICES:
        if _running(name):
            print(f"  {name:12s} already running")
            continue
        proc = subprocess.Popen(
            _command_for(name),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _pid_file(name).write_text(str(proc.pid))
        print(f"  {name:12s} started (pid {proc.pid})")
        time.sleep(0.5)
    return 0


def cmd_stop(args) -> int:
    for name in reversed(SERVICES):
        pid = _running(name)
        if not pid:
            print(f"  {name:12s} not running")
            _pid_file(name).unlink(missing_ok=True)
            continue
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
        _pid_file(name).unlink(missing_ok=True)
        print(f"  {name:12s} stopped")
    print("\nAll hardware released. LabVIEW can be started.")
    return 0


def cmd_restart(args) -> int:
    cmd_stop(args)
    time.sleep(1)
    return cmd_start(args)


def cmd_status(args) -> int:
    try:
        config = load()
    except ConfigError as exc:
        print(f"config: INVALID — {exc}")
        return 2

    enabled = config.enabled_channels()
    print(f"config    {config.config_hash}  "
          f"{len(enabled)} enabled of {len(config.channels)} channels")
    print()

    for name in SERVICES:
        pid = _running(name)
        print(f"  {name:12s} {'running (pid %d)' % pid if pid else 'stopped'}")

    # Heartbeat ages come from the retained MQTT topics, never from the
    # database: the status view must work when the database does not (§8.1).
    print()
    try:
        _print_bus_status(args)
    except Exception as exc:
        print(f"  broker    unreachable — {exc}")
    return 0


def _print_bus_status(args) -> None:
    from ..bus import TOPIC_STATUS, Bus
    from ..model import parse_iso, utcnow

    seen: dict[str, str] = {}
    bus = Bus(client_id="xams-ctl-status", host=args.broker, port=args.port)
    bus.subscribe(f"{TOPIC_STATUS}/#", lambda t, p: seen.__setitem__(t, p))
    bus.connect()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not bus.connected:
        time.sleep(0.1)
    if not bus.connected:
        bus.disconnect()
        print("  broker    not reachable")
        return
    time.sleep(1.5)
    bus.disconnect()

    now = utcnow()
    for topic in sorted(seen):
        if topic.endswith("/heartbeat"):
            service = topic.split("/")[2]
            try:
                age = (now - parse_iso(seen[topic])).total_seconds()
                print(f"  {service:12s} heartbeat {age:.0f}s ago")
            except Exception:
                print(f"  {service:12s} heartbeat unparseable: {seen[topic]!r}")


def cmd_reload(args) -> int:
    """Re-read the YAML without restarting, so a change costs no data gap."""
    try:
        config = load()
    except ConfigError as exc:
        print(f"REFUSED: configuration is invalid, nothing reloaded.\n  {exc}")
        return 2

    from ..bus import Bus
    bus = Bus(client_id="xams-ctl-reload", host=args.broker, port=args.port)
    bus.connect()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not bus.connected:
        time.sleep(0.1)
    if not bus.connected:
        print("broker not reachable; nothing reloaded")
        bus.disconnect()
        return 1

    bus.publish_raw("xams/cmd/all/reload", json.dumps({"config": config.config_hash}))
    time.sleep(0.5)
    bus.disconnect()
    print(f"reload requested (config {config.config_hash})")
    return 0


def cmd_check(args) -> int:
    """Validate the configuration and print what it defines. No side effects."""
    try:
        config = load()
    except ConfigError as exc:
        print(f"INVALID: {exc}")
        return 2

    by_device: dict[str, int] = {}
    for c in config.enabled_channels():
        by_device[c.device] = by_device.get(c.device, 0) + 1

    print(f"config {config.config_hash} from {CONFIG_DIR}")
    print(f"{len(config.channels)} channels defined, "
          f"{len(config.enabled_channels())} enabled\n")
    for device in sorted(by_device):
        print(f"  {device:12s} {by_device[device]:3d} enabled")

    tbd = [c.name for c in config.enabled_channels() if c.unit == "TBD"]
    if tbd:
        print(f"\n  {len(tbd)} enabled channels have unit TBD (§16): "
              f"{', '.join(tbd)}")

    enabled_recipients = [
        r for r in (config.recipients.get("recipients") or []) if r.get("enabled")
    ]
    if not enabled_recipients:
        print("\n  WARNING: no enabled recipients — alarms would reach nobody (§4.4)")
    return 0


def main(argv=None) -> int:
    # The Windows console defaults to a codepage that cannot render the §
    # section references this project uses throughout. Ask for UTF-8 rather
    # than avoiding the character.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    p = argparse.ArgumentParser(prog="xams-ctl", description=__doc__.split("\n")[0])
    p.add_argument("--broker", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1883)
    sub = p.add_subparsers(dest="command", required=True)
    for verb, fn in [
        ("start", cmd_start), ("stop", cmd_stop), ("restart", cmd_restart),
        ("status", cmd_status), ("reload", cmd_reload), ("check", cmd_check),
    ]:
        sub.add_parser(verb).set_defaults(func=fn)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
