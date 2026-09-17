"""Resolve serial instruments by asking them who they are. See DESIGN.md §6.2.

A device is identified by **asking it**, never by which COM port it happens to
occupy. A port renumbered by Windows, a device moved to another hub socket, or
two instruments' cables swapped are therefore all harmless.

For the CAEN supplies this is not a cross-check on the hardware ID — it is the
*only* identification, because neither unit exposes a USB serial number and
both present the same VID/PID. Step 1 cannot tell them apart at all.

The six rules of §6.2 are implemented here, once, for every serial driver:

 1. Never probe a port that did not pass the VID/PID filter. Writing bytes at
    an unknown serial device is not a neutral act — this machine also exposes
    COM3 as Intel AMT Serial-over-LAN.
 2. Match on the identity PAIR (model and serial), not the serial alone.
 3. A malformed, truncated or absent reply means unidentified. Never "probably
    the right one".
 4. Probe every candidate before binding any. Resolution is two-pass.
 5. Two ports reporting the same identity is fatal — there is no safe way to
    guess which unit is which.
 6. Re-verify on every reconnect, not only at startup. A reconnect after a USB
    glitch is exactly where a swap would otherwise slip through.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from serial.tools import list_ports

log = logging.getLogger(__name__)


class IdentityError(Exception):
    """Refusing to proceed: a device could not be identified with certainty."""


@dataclass(frozen=True)
class Candidate:
    port: str
    vid: str
    pid: str
    usb_serial: str | None
    description: str


def candidates(vid: str, pid: str) -> list[Candidate]:
    """Ports matching a configured USB hardware ID. Rule 1.

    Ports that do not match are never opened.
    """
    found = []
    for p in list_ports.comports():
        if p.vid is None or p.pid is None:
            continue
        if f"{p.vid:04X}".upper() != vid.upper():
            continue
        if f"{p.pid:04X}".upper() != pid.upper():
            continue
        found.append(Candidate(
            port=p.device, vid=f"{p.vid:04X}", pid=f"{p.pid:04X}",
            usb_serial=p.serial_number or None, description=p.description or "",
        ))
    return sorted(found, key=lambda c: c.port)


def resolve(
    vid: str,
    pid: str,
    ask: Callable[[str], tuple[str, ...] | None],
    expected: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    """Map each configured device id to the port where it actually is.

    `ask(port)` opens the port and returns the instrument's identity tuple, or
    None if it did not answer intelligibly. `expected` maps a device id to the
    identity tuple that device must report.

    Returns {device_id: port}. Raises IdentityError rather than guessing.
    """
    ports = candidates(vid, pid)
    if not ports:
        raise IdentityError(
            f"no serial port with USB id {vid}:{pid}. Is the instrument "
            f"powered and its cable connected?")

    log.info("%d candidate port(s) for %s:%s: %s", len(ports), vid, pid,
             ", ".join(c.port for c in ports))

    # Rule 4: read every identity BEFORE binding any of them. This is what
    # makes a cable swap harmless.
    seen: dict[str, tuple[str, ...]] = {}
    for cand in ports:
        identity = ask(cand.port)
        if identity is None:
            # Rule 3. Not an error yet — another port may hold the device we
            # want — but this port is not a candidate for anything.
            log.warning("%s did not return an intelligible identity; ignoring",
                        cand.port)
            continue
        log.info("%s reports %s", cand.port, " / ".join(str(x) for x in identity))
        seen[cand.port] = identity

    # Rule 5: the same instrument cannot be in two places.
    by_identity: dict[tuple[str, ...], list[str]] = {}
    for port, identity in seen.items():
        by_identity.setdefault(identity, []).append(port)
    for identity, where in by_identity.items():
        if len(where) > 1:
            raise IdentityError(
                f"identity {identity} reported by more than one port "
                f"({', '.join(where)}). That is either a duplicate serial "
                f"number or a bug; there is no safe way to guess which unit "
                f"is which. Refusing to start.")

    # Rule 2: the whole tuple must match, not just part of it.
    mapping: dict[str, str] = {}
    for device_id, want in expected.items():
        matches = [p for p, got in seen.items() if got == tuple(want)]
        if not matches:
            raise IdentityError(
                f"{device_id}: no port reported the expected identity "
                f"{tuple(want)}. Ports answered: "
                f"{ {p: i for p, i in seen.items()} or 'nothing'}. "
                f"Refusing to start; a device that has not said who it is "
                f"is not written to and not trusted.")
        mapping[device_id] = matches[0]
        log.info("%s resolved to %s", device_id, matches[0])

    return mapping
