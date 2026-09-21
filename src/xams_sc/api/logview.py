"""Reading the service logs for the Logs tab. See DESIGN.md §8.1.

Finding the files, picking a rotation, putting the newest line at the top and
turning a log into HTML. None of it is about HTTP, and all of it used to sit
in `app.py` between the route handlers, which is the one file a new student
has to read to understand the web UI (principle 1).

The escaping in `colourise` is the part that matters. A log line is not
trusted text: it carries device replies, exception strings and MQTT payloads
straight off the wire.
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

from ..config import LOG_DIR

# `setup_logging` keeps 5 rotated files per service; anything outside this
# range is not a log this system wrote.
ROTATIONS = (1, 2, 3, 4, 5)

# The start of a log line, per the format in `setup_logging`: a date, a time.
_RECORD = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def log_files() -> dict[str, Path]:
    """The logs the page may show, by the name it shows them under.

    A whitelist by construction: the selected name is looked up here rather
    than interpolated into a path, so `?service=../something` selects nothing
    instead of reading it.
    """
    if not LOG_DIR.is_dir():
        return {}
    return {p.stem: p for p in sorted(LOG_DIR.glob("*.log"))}


def rotated(path: Path, n: int) -> Path:
    """`caen.log` -> `caen.log.2`, as RotatingFileHandler names them."""
    return path.with_name(f"{path.name}.{n}")


def newest_first(lines: list[str]) -> list[str]:
    """Reverse the log RECORDS, not the lines.

    A traceback is many lines of one record, and reversing line by line
    prints it inside out — which is exactly the record somebody came to the
    page to read. Lines that do not start a record stay with the line above
    them, and a window that opens mid-record keeps its orphan lines together.
    """
    records: list[list[str]] = []
    for line in lines:
        if _RECORD.match(line) or not records:
            records.append([line])
        else:
            records[-1].append(line)
    return [line for record in reversed(records) for line in record]


# One record's header, exactly as `setup_logging` writes it (service.py):
#     2026-09-20 11:26:28,091 INFO     [sinks] xams_sc.sinks.pg_writer: text
# The run of spaces between the fields is captured rather than assumed, so the
# rendered line stays aligned with the file it came from - a log read side by
# side with the real file must not shift under the reader.
_HEADER = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)"
    r"(?P<gap1>\s+)(?P<level>[A-Z]+)(?P<gap2>\s+)"
    r"(?P<service>\[[^\]]*\])(?P<gap3>\s*)"
    r"(?P<logger>[\w.]+): (?P<message>.*)$")

# The backup is driven from PowerShell (tools/backup.ps1), not through
# `setup_logging`, and writes a shorter record: no milliseconds, no service
# tag, no logger, and WARN where Python writes WARNING.
#     2026-09-18 09:43:52 INFO    backup starting (target nikhef-backup:/...)
# Without a pattern of its own every line of backup.log missed `_HEADER` and
# came out as continuation grey - and backup.log is the one log where a failed
# nightly copy has to catch the eye.
_HEADER_PLAIN = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"(?P<gap1>\s+)(?P<level>[A-Z]+)(?P<gap2>\s+)(?P<message>.*)$")

# A class per level rather than a colour per level: the palette belongs in the
# stylesheet with every other colour in the interface (§8.1), not in here.
# WARN and WARNING are one severity spelled two ways - the shell writes one,
# `logging` the other.
_LEVEL_CLASS = {"DEBUG": "lg-debug", "INFO": "lg-info", "WARN": "lg-warn",
                "WARNING": "lg-warn", "ERROR": "lg-error",
                "CRITICAL": "lg-crit"}


def colourise(text: str) -> str:
    """The log as HTML, one span per field, for the Logs tab (§8.1).

    Done here rather than in the browser so that it is testable like the rest
    of the page, and so a reader with JavaScript off still gets the colours.

    EVERY piece is escaped BEFORE any markup goes near it. A log line is not
    trusted text: it carries device replies, exception strings and MQTT
    payloads straight off the wire, and one `<script>` in a CAEN error string
    would otherwise run on this page.
    """
    out = []
    for line in text.splitlines():
        match = _HEADER.match(line)
        # Only where the long format did not match: a `setup_logging` line
        # carries milliseconds, which `_HEADER_PLAIN` refuses anyway, but
        # trying that pattern first keeps it an argument, not a dependency.
        plain = None if match else _HEADER_PLAIN.match(line)
        head = match or plain
        if head is None:
            # A traceback body, or any line that does not start a record.
            # Dimmed as one block so the eye falls to the next timestamp
            # instead of reading the stack frames first.
            out.append('<span class="lg-cont">%s</span>' % escape(line))
            continue
        level = head.group("level")
        cls = _LEVEL_CLASS.get(level, "lg-info")
        # ERROR and CRITICAL colour the MESSAGE too, not just the level word.
        # Everything else leaves it in the body colour: if every line shouts,
        # the one that matters stops standing out.
        message = escape(head.group("message"))
        if level in ("ERROR", "CRITICAL"):
            message = '<span class="%s">%s</span>' % (cls, message)
        stamp = ('<span class="lg-time">%s</span>%s'
                 '<span class="%s">%s</span>%s'
                 % (escape(head.group("time")), head.group("gap1"),
                    cls, escape(level), head.group("gap2")))
        if match is None:
            # The shell format has no service and no logger to colour, and
            # neither may be invented for it: the line has to read the same
            # as the file it came from.
            out.append(stamp + message)
            continue
        out.append(
            '%s<span class="lg-svc">%s</span>%s'
            '<span class="lg-name">%s</span>: %s'
            % (stamp, escape(match.group("service")), match.group("gap3"),
               escape(match.group("logger")), message))
    return "\n".join(out)
