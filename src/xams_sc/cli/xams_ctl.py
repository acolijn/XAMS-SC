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
import os
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
SERVICES = ["sinks", "cdaq", "caen", "lakeshore", "ups", "derived", "alarms"]

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
    if name in ("sinks", "alarms"):
        return [sys.executable, "-m", f"xams_sc.{name}"]
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


def _lock_held(name: str) -> bool:
    """True while a service still holds its lock file.

    On Windows an open file cannot be deleted, so being able to remove it is
    proof that nothing holds it. The lock is recreated by the service itself
    on the next start, so removing a released one here is harmless.
    """
    path = PID_DIR / f"{name}.lock"
    if not path.exists():
        return False
    try:
        path.unlink()
        return False
    except OSError:
        return True


def cmd_stop(args) -> int:
    stopped = []
    for name in reversed(SERVICES):
        pid = _running(name)
        if not pid:
            print(f"  {name:12s} not running")
            _pid_file(name).unlink(missing_ok=True)
            continue
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
        _pid_file(name).unlink(missing_ok=True)
        stopped.append(name)
        print(f"  {name:12s} stopped")

    # WAIT FOR THE PROCESSES TO ACTUALLY GO.
    #
    # taskkill returns as soon as it has asked. The process may take a moment
    # to die, and until it does it still holds its single-instance lock and
    # its hardware. `restart` then failed with "another cdaq service is
    # already running" — the lock working exactly as intended against a stop
    # that had not finished. Waiting for the locks to clear is what makes
    # stop mean stopped.
    held = []
    if stopped:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            held = [n for n in stopped if _lock_held(n)]
            if not held:
                break
            time.sleep(0.3)
        if held:
            print(f"\n  WARNING: still holding a lock after 15s: {', '.join(held)}")
            print("  Their hardware may not be released yet.")
            return 1

    print("\nAll hardware released. LabVIEW can be started.")
    return 0


def cmd_restart(args) -> int:
    if cmd_stop(args) != 0:
        print("\nNot restarting: something did not stop cleanly.")
        return 1
    print()
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

    # Collect acknowledgements and report what ACTUALLY reloaded.
    #
    # An earlier version published the command and printed "reload requested",
    # which was true and useless: nothing subscribed to the topic, so the
    # command reported success while changing nothing. A threshold edit
    # followed by `reload` left the old limit in force, silently. Reporting
    # only what came back is the fix.
    acks: dict[str, dict] = {}
    bus.subscribe("xams/ack/+/reload",
                  lambda t, p: acks.__setitem__(t.split("/")[2], json.loads(p)))
    time.sleep(0.3)
    bus.publish_raw("xams/cmd/all/reload", json.dumps({"config": config.config_hash}))
    time.sleep(2.5)
    bus.disconnect()

    if not acks:
        print("No service acknowledged the reload.")
        print("Nothing has changed. Are the services running?")
        return 1

    applied = sorted(k for k, v in acks.items() if v.get("applied"))
    refused = sorted(k for k, v in acks.items() if not v.get("applied"))

    print(f"config {config.config_hash}")
    for name in applied:
        print(f"  {name:12s} reloaded")
    for name in refused:
        why = acks[name].get("error")
        print(f"  {name:12s} {'REFUSED: ' + why if why else 'needs a restart to apply this'}")

    if refused and not any(acks[n].get("error") for n in refused):
        print()
        print("Services that hold hardware rebuild their tasks from the channel")
        print("map at startup, so a channel change needs:")
        print("  xams-ctl restart")
    return 0 if not any(acks[n].get("error") for n in refused) else 2


def cmd_flow_reset(args) -> int:
    """Close the running flow-integrator period and open a new one (§7.5).

    Nothing is erased. The closed period keeps its total and its gaps, so the
    history of how much passed through during each period survives — unlike a
    counter somebody zeroed, which is gone.
    """
    from ..bus import TOPIC_FLOW_RESET, Bus

    who = args.by or os.environ.get("USERNAME") or "unknown"
    result = {}

    bus = Bus(client_id="xams-ctl-flow-reset", host=args.broker, port=args.port)
    bus.subscribe("xams/ack/derived/flow_reset",
                  lambda t, p: result.update(json.loads(p)))
    bus.connect()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not bus.connected:
        time.sleep(0.1)
    if not bus.connected:
        print("broker not reachable; nothing was reset")
        bus.disconnect()
        return 1

    time.sleep(0.3)
    bus.publish_raw(TOPIC_FLOW_RESET, json.dumps({"by": who}))
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not result:
        time.sleep(0.2)
    bus.disconnect()

    if not result:
        print("The derived service did not acknowledge. Is it running?")
        print("Nothing has been reset.")
        return 1

    print(f"Flow period closed by {who}:")
    print(f"  started   {result.get('start')}")
    print(f"  stopped   {result.get('stop')}")
    print(f"  total     {result.get('total_g', 0):.3f} g")
    print(f"  gaps      {result.get('gaps_s', 0):.0f} s")
    if result.get("gaps_s", 0) > 0:
        print("            (the total is an underestimate by whatever flowed")
        print("             during those gaps — that is why they are recorded)")
    print()
    print("A new period is now open. The closed one is kept in flow_periods.")
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

    flow = sub.add_parser("flow-reset",
                          help="close the flow-integrator period and open a new one")
    flow.add_argument("--by", help="who is doing this (recorded in the audit log)")
    flow.set_defaults(func=cmd_flow_reset)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
