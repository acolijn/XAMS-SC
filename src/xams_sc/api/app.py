"""Web UI. See DESIGN.md §8.1.

    python -m xams_sc.api

**Binds to 127.0.0.1, never 0.0.0.0.** `0.0.0.0` is the default in most
tutorials and in much example code; on this system it would expose the control
surface to the building network. Every bind address is explicit, and loopback
is the only accepted value without a recorded decision to the contrary (§8).

**Almost everything here is read-only.** Three things are not:

  * the flow-integrator reset, which changes a record rather than hardware
    (§7.5)
  * the Lake Shore setpoint, on output 1
  * the Lake Shore heater range, on output 1

The last two write to an instrument (§10). This module validates none of it
beyond "is that a number": the range, the connection, the read-back and the
audit record all belong to the service that owns the port, so a command from
this page is treated exactly like one from the CLI. Duplicating the limits
here would mean two numbers to keep in step, and the copy in channels.yaml is
the one that counts.

Deliberately plain: server-rendered HTML, a meta refresh, no JavaScript
framework and no build step. In three years a student must be able to change
it without installing a toolchain (§8.1).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..bus import (ACK_HV_OUTPUT, ACK_HV_VSET, ACK_LS_RANGE,
                   ACK_LS_SETPOINT, ACK_NOTIFY, TOPIC_AUDIT, TOPIC_HV_OUTPUT,
                   TOPIC_HV_VSET, TOPIC_LS_RANGE, TOPIC_LS_SETPOINT,
                   TOPIC_NOTIFY, TOPIC_RELOAD, Bus)
from ..config import (LOG_DIR, ConfigError, load, read_hv_defaults,
                      read_recipients, recipient_warnings,
                      validate_recipients, write_hv_defaults,
                      write_recipients)
from ..hv_status import (describe_status, is_disabled, is_energised,
                         status_faults)
from ..grafana import DriftWatcher
from .state import SystemState

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def check_mimic_tags(config) -> list[str]:
    """Compare the SVG's ids against channels.yaml, in BOTH directions (§8.2).

    The SVG is a copy of a drawing that will eventually change, and a mimic
    quietly out of date with the plant is a liability. This is what makes that
    visible rather than silent — roughly ten lines, and it is what makes the
    page survivable three years from now.

    Both directions matter and they fail differently:

      * an id with no channel  — the drawing shows an instrument this system
        does not read, and the bubble would sit empty forever
      * a channel with no id   — a reading nobody can find on the drawing,
        which is the half-identity §3 warns about

    Returns the problems. Logged at startup; never fatal, because a mimic that
    has drifted is still more useful than no mimic.
    """
    svg_path = HERE / "static" / "xams_pid.svg"
    if not svg_path.exists():
        return ["the mimic SVG has not been built (run tools/build_mimic.py)"]

    ids = set(re.findall(r'id="v-([^"]+)"', svg_path.read_text(encoding="utf-8")))
    problems = []

    for name in sorted(ids - set(config.channels)):
        problems.append(f"the drawing has a value slot for {name!r}, which is "
                        f"not a channel in channels.yaml")

    # Only channels that describe a point in the plant belong on a P&ID. HV,
    # UPS, heaters and derived values have no place on a piping drawing, and
    # listing them as missing would be noise that trains people to ignore this.
    on_drawing = {c.name for c in config.enabled_channels()
                  if c.on_pid and (c.kind in ("rtd", "temperature")
                                   or (c.kind == "voltage" and c.device == "cdaq"))}
    for name in sorted(on_drawing - ids):
        problems.append(f"{name!r} is read but has no place on the drawing")
    return problems


OPERATOR_COOKIE = "xams_operator"


def safe_next(target: str) -> str:
    """Where to return after setting the operator.

    Only a path on this site. A redirect target taken from a form is an open
    redirect if it is used as given - "//evil.example" is a protocol-relative
    URL, not a path - so anything that is not a single-slash path is thrown
    away rather than repaired.
    """
    target = (target or "/").strip()
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def operator_of(request: Request, submitted: str = "") -> str:
    """Who is performing this action (section 10 rule 5).

    Taken from a cookie set once, rather than typed before every command.
    Retyping a name for each setpoint is the kind of friction that gets
    worked around - by leaving it blank, which costs the audit trail the
    thing it exists for.

    Still taken on trust: there is no login on this UI, so this identifies
    a browser, not a person. That is weak and is recorded honestly rather
    than dressed up. A name given on the request itself wins, so the CLI and
    the API are unaffected by any of this.
    """
    name = (submitted or "").strip()
    if not name:
        name = (request.cookies.get(OPERATOR_COOKIE) or "").strip()
    return name or "webui (unnamed)"


def _remember(response, name: str):
    """Keep the operator name for a year. Harmless if never set."""
    if name and name != "webui (unnamed)":
        response.set_cookie(OPERATOR_COOKIE, name, max_age=365 * 24 * 3600,
                            samesite="lax", path="/")
    return response


async def _form(request: Request):
    """The submitted form as a plain dict."""
    return dict(await request.form())


def _short(channel: str) -> str:
    """`hv_cathode_vset` reads as `cathode` in a message to a person."""
    return channel.replace("hv_", "").replace("_vset", "")


def _limits_view(state) -> dict:
    """The thresholds in force, arranged for display (§11).

    Returns `{"known": bool, "rows": [...]}`. **`known` is false when the
    engine has published nothing**, and the page then says so rather than
    showing an empty table - "no thresholds are configured" and "I cannot see
    the alarm engine" are different facts, and only one of them is about the
    plant.
    """
    published = state.limits()
    if published is None:
        return {"known": False, "rows": []}

    order = ("lolo", "low", "high", "hihi")
    rows = []
    for channel in sorted(published):
        thresholds = published[channel] or {}
        cfg = state.config.channels.get(channel)
        rows.append({
            "channel": channel,
            "description": cfg.description if cfg else "",
            "unit": cfg.unit if cfg else "",
            "levels": [(name, thresholds.get(name)) for name in order],
        })
    return {"known": True, "rows": rows}


def _people_from_form(form: dict) -> tuple[list[dict], list[dict]]:
    """Rebuild the recipient list from the posted rows.

    Rows arrive as `name-0`, `email-0`, `phone-0`, `enabled-0`, `remove-0`.
    The index ties the fields of one person together and is otherwise
    meaningless - the saved order is the order of the rows on the page.

    An entirely blank row is dropped rather than refused: the page offers a
    spare row at the bottom for adding somebody, and submitting without using
    it must not be an error.
    """
    indices = sorted({key.split("-", 1)[1] for key in form
                      if key.startswith("name-") and "-" in key},
                     key=lambda i: (len(i), i))
    people, removed = [], []
    for i in indices:
        person = {
            "name": (form.get(f"name-{i}") or "").strip(),
            "email": (form.get(f"email-{i}") or "").strip(),
            # Kept as an empty string when blank, which MEANS "do not SMS
            # this person" - they are notified by email alone. It is not a
            # number somebody forgot to fill in.
            "phone": (form.get(f"phone-{i}") or "").strip(),
            "enabled": bool(form.get(f"enabled-{i}")),
        }
        if not any((person["name"], person["email"], person["phone"])):
            continue
        # Removal is a checkbox applied on save, never a button that deletes
        # on click: this page sits open beside a self-refreshing UI, and a
        # one-click irreversible delete next to that is the wrong affordance.
        if form.get(f"remove-{i}"):
            removed.append(person)
            continue
        people.append(person)
    return people, removed


def _recipient_changes(before: list[dict], after: list[dict],
                       removed: list[dict]) -> list[dict]:
    """What changed, as audit lines (§4.4).

    Keyed by name, because that is what a person is called in a conversation
    about who was on the list. It stays recoverable who would have been
    notified when a given alarm fired - which matters most for somebody who
    was REMOVED, since the file no longer mentions them at all.
    """
    def summarise(person):
        bits = [person.get("email") or "no email",
                person.get("phone") or "no phone",
                "enabled" if person.get("enabled") else "disabled"]
        return ", ".join(bits)

    old_by_name = {(p.get("name") or "").casefold(): p for p in before}
    new_by_name = {(p.get("name") or "").casefold(): p for p in after}
    lines = []

    for person in removed:
        lines.append({"target": person.get("name") or "(unnamed)",
                      "old": summarise(person), "new": "removed"})
    for key, person in new_by_name.items():
        old = old_by_name.get(key)
        if old is None:
            lines.append({"target": person.get("name"),
                          "old": None, "new": summarise(person)})
        elif summarise(old) != summarise(person):
            lines.append({"target": person.get("name"),
                          "old": summarise(old), "new": summarise(person)})
    for key, old in old_by_name.items():
        if key not in new_by_name and not any(
                (r.get("name") or "").casefold() == key for r in removed):
            # Vanished without the remove box being ticked - a blanked-out
            # row. Recorded all the same; a person who stops being notified
            # must leave a trace however they left.
            lines.append({"target": old.get("name") or "(unnamed)",
                          "old": summarise(old), "new": "removed"})
    return lines


def _audit(state, actor: str, action: str, target: str, old, new,
           result: str = "ok", detail: str = "") -> None:
    """Record a change on the audit topic (§10 rule 5).

    The web UI audits this one directly, where every write to an INSTRUMENT is
    audited by the service that owns the port. The rule is the same in both
    cases - whoever performs the change records it - and here the thing being
    changed is a file this process owns, not a port.
    """
    import json as _json
    from ..model import utcnow
    state.bus.publish_raw(TOPIC_AUDIT, _json.dumps(
        {"t": utcnow().isoformat().replace("+00:00", "Z"),
         "actor": actor or "unknown", "action": action, "target": target,
         "old": None if old is None else str(old),
         "new": None if new is None else str(new),
         "result": result, "detail": detail}, separators=(",", ":")))


# `setup_logging` keeps 5 rotated files per service; anything outside this
# range is not a log this system wrote.
_ROTATIONS = (1, 2, 3, 4, 5)

# The start of a log line, per the format in `setup_logging`: a date, a time.
_RECORD = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _log_files() -> dict[str, Path]:
    """The logs the page may show, by the name it shows them under.

    A whitelist by construction: the selected name is looked up here rather
    than interpolated into a path, so `?service=../something` selects nothing
    instead of reading it.
    """
    if not LOG_DIR.is_dir():
        return {}
    return {p.stem: p for p in sorted(LOG_DIR.glob("*.log"))}


def _rotated(path: Path, n: int) -> Path:
    """`caen.log` -> `caen.log.2`, as RotatingFileHandler names them."""
    return path.with_name(f"{path.name}.{n}")


def _newest_first(lines: list[str]) -> list[str]:
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


def _redirect_hv(error: str | None, ok: str | None = None):
    """Back to /hv, carrying what happened. POST then redirect: that page
    reloads itself, and a refresh must not repeat a write to an instrument."""
    from urllib.parse import quote
    if error:
        return RedirectResponse("/hv?hv_error=" + quote(error), status_code=303)
    return RedirectResponse("/hv?hv_ok=" + quote(ok or "done"), status_code=303)


def _redirect_ls(error: str | None, ok: str | None = None):
    """Back to the overview, carrying what happened. POST then redirect: a
    refresh must not repeat a write to an instrument."""
    from urllib.parse import quote
    if error:
        return RedirectResponse("/?ls_error=" + quote(error), status_code=303)
    return RedirectResponse("/?ls_ok=" + quote(ok or "done"), status_code=303)


def create_app(broker: str = "127.0.0.1", port: int = 1883) -> FastAPI:
    # The INITIAL configuration only. Handlers read `state.config`, which is
    # replaced on `xams-ctl reload` — a description or a unit edited in
    # channels.yaml must appear on the page without restarting anything, or
    # the reload looks as though it did nothing.
    config = load()
    bus = Bus(client_id="webui", host=broker, port=port)
    state = SystemState(config, bus)
    state.start()

    for problem in check_mimic_tags(config):
        log.warning("mimic drift: %s", problem)

    app = FastAPI(title="XAMS Slow Control", docs_url=None, redoc_url=None)
    app.state.system = state
    app.state.config = config
    # Cached: this page reloads every 10 s and must not make an HTTP request
    # to Grafana per view, nor wait on one.
    drift = DriftWatcher()
    app.state.drift = drift

    def page(request: Request, name: str, **context):
        status, css = state.overall()
        # state.config, not the copy captured at startup: it is replaced on
        # reload and the page must show the current one.
        return templates.TemplateResponse(request, name, {
            "state": state, "config": state.config, "overall": status,
            "overall_class": css, "alarms": state.active_alarms(),
            # Site-wide (§4.4a): a page must never be able to show a healthy
            # system without showing that its alarms are switched off.
            "notify": state.notifications(),
            # Site-wide: the operator identifies the session, not one
            # instrument, and the HV control surface will want the same name
            # without setting it again (section 10 rule 5).
            "operator": request.cookies.get(OPERATOR_COOKIE, ""),
            **context})

    # ------------------------------------------------------------- pages

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        """The page to bookmark. Answers "is everything all right?" with no
        clicks and no scrolling, and only then offers links (§8.1)."""
        return page(request, "overview.html",
                    services=state.services(),
                    unhealthy=state.unhealthy_channels(),
                    faults=state.known_faults(),
                    flow=state.flow_total(),
                    drift=drift.get(),
                    backup=state.backup_status(),
                    lakeshore=lakeshore_view(),
                    operator=request.cookies.get(OPERATOR_COOKIE, ""),
                    ls_ok=request.query_params.get("ls_ok"),
                    ls_error=request.query_params.get("ls_error"),
                    reset=request.query_params.get("reset"),
                    reset_total=request.query_params.get("total"),
                    ups=state.channels("ups"))

    def lakeshore_view():
        """The Lake Shore as the landing page shows it (§8.1).

        Sensors and outputs are listed SEPARATELY rather than paired up. Which
        input drives which output is set by OUTMODE on the instrument and is
        not read here, so pairing them on the page would be a guess presented
        as a fact — and a convincing one, since output 1's setpoint and tt401
        currently agree to a millikelvin.
        """
        # OUTPUT 1 ONLY. Output 2 is not used on this cryostat, and a row
        # that always reads 0 % against a setpoint nobody acts on is noise on
        # the page people are meant to scan in one glance (section 8.1).
        #
        # It is still READ, still archived, and still on /status - an unused
        # heater that starts doing something remains worth catching. What
        # changes here is only that it has no place on the landing page.
        return {
            "sensors": [state.channel(n) for n in ("tt401", "tt402")],
            "outputs": [
                {"n": n,
                 "setpoint": state.channel("ls_setpoint_%d" % n),
                 "percent": state.channel("ls_heater_%d" % n),
                 "watts": state.channel("ls_heater_%d_w" % n)}
                for n in (1,)],
        }

    @app.post("/operator")
    def set_operator(request: Request, operator: str = Form(""),
                     next: str = Form("/")):
        """Remember who is at the keyboard, so commands need no name.

        Set from the header, so it can be submitted from any page and has to
        return to the one it was submitted from.
        """
        name = (operator or "").strip()[:40]
        response = RedirectResponse(safe_next(next), status_code=303)
        if name:
            return _remember(response, name)
        response.delete_cookie(OPERATOR_COOKIE, path="/")
        return response

    @app.post("/hv/apply")
    def hv_apply(request: Request):
        """Write the setpoints the operator has filled in (§10a).

        **Every non-empty box is applied, as one action.** Empty boxes are
        left alone, so a page full of channels can be moved one at a time or
        all together without a different button for each case.

        The service does the validating - range, polarity, whether the channel
        is enabled, and the read-back. Duplicating those rules here would mean
        two copies, and the copy that matters is the one next to the hardware.
        """
        import asyncio
        form = asyncio.run(_form(request))
        who = operator_of(request, form.get("by", ""))

        results, failures = [], 0
        for name, raw in form.items():
            if not name.startswith("hv_") or not name.endswith("_vset"):
                continue
            text = (raw or "").strip()
            if not text:
                continue
            try:
                value = float(text)
            except ValueError:
                results.append(f"{name}: {text!r} is not a number")
                failures += 1
                continue
            answer = state.command(TOPIC_HV_VSET, ACK_HV_VSET,
                                   {"channel": name, "value": value,
                                    "by": who})
            if answer.get("ok"):
                results.append(f"{_short(name)} now {answer['new']:+.1f} V")
                log.warning("HV setpoint %s -> %+.1f by %s", name, value, who)
            else:
                results.append(f"{_short(name)}: {answer.get('reason')}")
                failures += 1

        if not results:
            return _redirect_hv(None, "nothing to apply - every box was empty")
        joined = "; ".join(results)
        return _redirect_hv(joined if failures else None,
                            None if failures else joined)

    # ------------------------------------------------- HV default setpoints
    #
    # The one place this UI edits a file in git, and it is allowed for the
    # reason §8.1 gave in advance: the need is demonstrated. This is an R&D
    # setup, the operating point changes often, and the alternative is an
    # editor and a reload for a number that reaches no instrument.
    #
    # It stays inside the line §8.1 draws. `limits` are NOT editable here:
    # they are the range the write path validates against, and a page that
    # could widen its own limit and then write to it is not a guardrail.

    @app.get("/hv/defaults", response_class=HTMLResponse)
    def hv_defaults(request: Request):
        """Edit the values `load defaults` offers (§4.6)."""
        raw = read_hv_defaults()
        rows = []
        for ch in state.config.channels.values():
            if ch.kind != "hv_vset":
                continue
            rows.append({
                "name": ch.name,
                "label": _short(ch.name),
                "description": ch.description,
                "default": ch.default_setpoint,
                "limits": ch.limits or {},
                "unit": ch.unit,
            })
        rows.sort(key=lambda r: r["name"])
        return page(request, "hv_defaults.html", rows=rows,
                    updated=raw.get("updated"), updated_by=raw.get("by"),
                    saved=request.query_params.get("saved"),
                    error=request.query_params.get("error"))

    @app.post("/hv/defaults")
    def hv_defaults_save(request: Request):
        """Write hv_defaults.yaml, then reload so /hv offers the new values.

        **Validated here against `channels.yaml`, and written all or nothing.**
        A partial save would leave the file describing a set of defaults
        nobody chose, which is the failure this page exists to prevent.
        """
        import asyncio
        from urllib.parse import quote
        form = asyncio.run(_form(request))
        who = operator_of(request, form.get("by", ""))

        values: dict[str, float] = {}
        problems: list[str] = []
        for ch in state.config.channels.values():
            if ch.kind != "hv_vset":
                continue
            if ch.name not in form:
                # ABSENT is not the same as EMPTY, and conflating them loses
                # data. A box left blank on this page is submitted as an empty
                # string and means "no default". A field that is not in the
                # form at all did not come from this page - a partial POST, or
                # some future caller sending one channel - and must leave the
                # other seven alone rather than clear them.
                keep = state.config.channels[ch.name].default_setpoint
                if keep is not None:
                    values[ch.name] = keep
                continue
            text = (form.get(ch.name) or "").strip()
            if not text:
                # An empty box means "no default for this channel" - the box
                # on /hv is then simply not filled by the button, which is
                # what hv_pmt_top looked like before anyone set one.
                continue
            try:
                volts = float(text)
            except ValueError:
                problems.append(f"{_short(ch.name)}: {text!r} is not a number")
                continue
            if not ch.limits:
                problems.append(
                    f"{_short(ch.name)}: no limits in channels.yaml, so no "
                    f"default can be applied to it")
                continue
            if not ch.in_limits(volts):
                problems.append(
                    f"{_short(ch.name)}: {volts:+.1f} V is outside "
                    f"{ch.limits['min']:+.0f}..{ch.limits['max']:+.0f} V. "
                    f"Limits live in channels.yaml and are not changed here.")
                continue
            values[ch.name] = volts

        if problems:
            return RedirectResponse(
                "/hv/defaults?error=" + quote("; ".join(problems)),
                status_code=303)

        # What changed, for the audit trail - computed before the write, while
        # the old values are still loaded.
        before = {ch.name: ch.default_setpoint
                  for ch in state.config.channels.values()
                  if ch.kind == "hv_vset"}

        try:
            write_hv_defaults(values, who)
        except OSError as exc:
            log.exception("could not write hv_defaults.yaml")
            return RedirectResponse(
                "/hv/defaults?error=" + quote(f"could not write the file: {exc}"),
                status_code=303)

        # Reload before reporting success. If the file we just wrote does not
        # load, saying "saved" would be a lie of exactly the kind §7.5 calls
        # out: reporting work that did not take effect.
        try:
            state.reload_config()
        except ConfigError as exc:
            log.error("hv_defaults.yaml written but will not load: %s", exc)
            return RedirectResponse(
                "/hv/defaults?error=" + quote(f"written, but it will not "
                                              f"load: {exc}"),
                status_code=303)

        changed = 0
        for name, old in sorted(before.items()):
            new = values.get(name)
            if (old is None and new is None) or (
                    old is not None and new is not None and old == new):
                continue
            changed += 1
            _audit(state, who, "hv_default", name, old, new)
            log.warning("HV default %s: %s -> %s by %s", name, old, new, who)

        # Every other service re-reads too, so `xams-ctl check` and a second
        # browser do not sit on the old file.
        state.bus.publish_raw(TOPIC_RELOAD, "{}")

        if not changed:
            return RedirectResponse("/hv/defaults?saved=" +
                                    quote("saved - nothing was different"),
                                    status_code=303)
        return RedirectResponse(
            "/hv/defaults?saved=" + quote(
                f"saved {changed} default{'s' if changed != 1 else ''}; "
                f"/hv now offers them"),
            status_code=303)

    @app.post("/hv/output")
    def hv_output(request: Request, channel: str = Form(""),
                  on: str = Form(""), by: str = Form("")):
        """Energise or de-energise one channel (§10a step 4).

        NOT the enable switch: that is a hand operation at the supply and
        nothing here can change it. This energises a channel that is already
        enabled, and it ramps to whatever setpoint is loaded - which is why
        the answer says which voltage that is.
        """
        who = operator_of(request, by)
        wanted = on.strip().lower() in ("1", "true", "on", "yes")
        answer = state.command(TOPIC_HV_OUTPUT, ACK_HV_OUTPUT,
                               {"channel": channel.strip(), "on": wanted,
                                "by": who})
        if answer.get("ok"):
            detail = answer.get("detail") or ("on" if wanted else "off")
            log.warning("HV output %s -> %s by %s", channel,
                        "on" if wanted else "off", who)
            return _redirect_hv(None, f"{_short(channel)}: {detail}")
        return _redirect_hv(f"{_short(channel)}: {answer.get('reason')}")

    @app.post("/lakeshore/setpoint")
    def lakeshore_setpoint(request: Request, value: str = Form(""),
                           by: str = Form("")):
        """Change the Lake Shore setpoint (§10).

        This page validates NOTHING beyond "is it a number". The range, the
        instrument being connected, the read-back and the audit record all
        live in the service that owns the port, so a command from the CLI
        gets exactly the same treatment as one from this form. Duplicating
        the range here would mean two numbers to keep in step, and the copy
        in channels.yaml is the one that counts.
        """
        who = operator_of(request, by)
        try:
            wanted = float((value or "").strip())
        except ValueError:
            return _redirect_ls("that is not a number")
        result = state.command(TOPIC_LS_SETPOINT, ACK_LS_SETPOINT,
                               {"output": 1, "value": wanted, "by": who})
        if result.get("ok"):
            log.warning("setpoint changed to %.3f C by %s", wanted, who)
            return _redirect_ls(None, "setpoint now %.3f C" % result["new"])
        return _redirect_ls(result.get("reason", "refused"))

    @app.post("/lakeshore/range")
    def lakeshore_range(request: Request, range: str = Form(""),
                        by: str = Form("")):
        """Switch the heater on (high) or off (§10)."""
        who = operator_of(request, by)
        result = state.command(TOPIC_LS_RANGE, ACK_LS_RANGE,
                               {"output": 1, "range": (range or "").strip(),
                                "by": who})
        if result.get("ok"):
            log.warning("heater range set to %s by %s", result["new"], who)
            return _redirect_ls(None, "heater %s" % result["new"])
        return _redirect_ls(result.get("reason", "refused"))

    @app.post("/flow/reset")
    def flow_reset(request: Request, by: str = Form("")):
        """Close the flow period and open a new one (§7.5).

        The one thing this UI can change, and it changes a RECORD, not an
        instrument: the closed period keeps its total and its gaps in
        `flow_periods`. That is the whole difference between this and zeroing
        a counter, and it is why it can exist before milestone 8.

        Audited like any other control action (§10), which means it needs a
        name. There is no login on this UI, so the name is typed and taken on
        trust — weak, but a weak attribution recorded honestly beats an
        anonymous one, and it matches what `xams-ctl flow-reset --by` does.

        POST, then redirect: a GET that mutates would fire on a refresh or a
        prefetch, and this page refreshes itself every ten seconds.
        """
        who = operator_of(request, by)
        closed = state.reset_flow(who)
        if closed is None:
            log.warning("flow reset by %s was not acknowledged", who)
            return RedirectResponse("/?reset=failed", status_code=303)
        log.info("flow period closed by %s: %.3f g over %.0f s of gaps",
                 who, closed.get("total_g", 0.0), closed.get("gaps_s", 0.0))
        return RedirectResponse(
            "/?reset=ok&total=%.3f" % closed.get("total_g", 0.0),
            status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request):
        by_device: dict[str, list] = {}
        for view in state.channels():
            by_device.setdefault(view.device, []).append(view)
        return page(request, "status.html", by_device=by_device)

    @app.get("/hv", response_class=HTMLResponse)
    def high_voltage(request: Request):
        """The CAEN supplies: what each channel reads, and what the board is
        configured to allow. The limits are DISPLAYED, never writable (§8.3)."""
        supplies = []
        for spec in state.config.devices.get("caen", []) or []:
            channels = []
            for ch in state.config.channels.values():
                if ch.device != spec["id"] or ch.kind != "hv_vmon":
                    continue
                index = int(ch.phys)
                imon_name = ch.name.replace("_vmon", "_imon")
                expect = (spec.get("expect") or {}).get(index, {})
                stat_view = state.channel(ch.name.replace("_vmon", "_stat"))
                vset_name = ch.name.replace("_vmon", "_vset")
                vset_view = state.channel(vset_name)
                default_channel = state.config.channels.get(vset_name)

                # The board's own STAT word decides on/off, NEVER whether
                # VMON is above zero: a channel can be enabled and sitting at
                # zero volts (§7.2). None means the word could not be read,
                # and the page says so rather than guessing from the voltage.
                word = None
                if (stat_view is not None and stat_view.healthy
                        and stat_view.value is not None):
                    word = int(stat_view.value)

                # THE SECTION 10a INVARIANT: a channel that is not enabled
                # must have VSET 0, because the enable is a hand operation and
                # the board ramps to whatever VSET holds the moment it is
                # flipped. Where that is not true, flipping the switch is a
                # step into an unannounced voltage - so the page says so
                # rather than leaving it to be discovered at the supply.
                # is_disabled - THE SWITCH - not is_energised, the output.
                #
                # This asked whether the channel was putting out volts, so a
                # channel whose switch was on but which was not energised
                # counted as "disabled with a setpoint" and produced the
                # warning "flipping the enable would ramp straight to it"
                # about a channel already enabled. The fourth bug of this
                # shape in one afternoon, which is what prompted the rename.
                armed = None
                if (vset_view is not None and vset_view.healthy
                        and vset_view.value is not None and word is not None):
                    armed = is_disabled(word) and abs(vset_view.value) > 1.0

                # FOUR STATES, and the distinction between the middle two
                # is the whole of section 10a:
                #
                #   bit 10 set              the front-panel switch is off
                #   neither bit            switch on, output NOT energised
                #   bit 0 set, VMON ~ 0    energised, sitting at zero volts
                #   bit 0 set, VMON != 0   energised with volts out
                #
                # `enabled` used to mean bit 0, on the mistaken belief that
                # bit 0 was the enable. It is not: bit 0 is the OUTPUT. A
                # channel whose switch had been flipped on but which had not
                # been energised therefore matched nothing and displayed as
                # "off" - which is what it looked like on 18 September 2026
                # after the nai channel was re-enabled by hand.
                channels.append({
                    "index": index,
                    "label": ch.name.replace("hv_", "").replace("_vmon", ""),
                    "description": ch.description,
                    "vmon": state.channel(ch.name),
                    "imon": state.channel(imon_name),
                    "vset": vset_view,
                    "vset_channel": vset_name,
                    "default": default_channel.default_setpoint
                               if default_channel else None,
                    "armed": armed,
                    "stat": stat_view,
                    "energised": None if word is None else is_energised(word),
                    "switched_off": None if word is None else is_disabled(word),
                    "faults": [] if word is None else status_faults(word),
                    "flags": "" if word is None else describe_status(word),
                    "expect": expect,
                    "limits": ch.limits or {},
                    "sign": ch.sign,
                })
            supplies.append({
                "armed": [c for c in channels if c.get("armed")],
                "id": spec["id"], "serial": spec.get("board_serial"),
                "model": spec.get("board_name"),
                "firmware": spec.get("firmware"),
                "channels": sorted(channels, key=lambda c: c["index"]),
            })
        return page(request, "hv.html", supplies=supplies,
                    hv_ok=request.query_params.get("hv_ok"),
                    hv_error=request.query_params.get("hv_error"))

    # ------------------------------------------------------------- alarms
    #
    # The alarm chain, end to end, on one page: what has fired, what WOULD
    # fire, and who is told. It exists because the chain was the least visible
    # part of the system and the most consequential - the thresholds in force
    # were published on `xams/status/limits` (§11) and displayed nowhere, and
    # the recipient list could only be changed by editing a file.
    #
    # Everything here is read from retained MQTT or from a file. No database
    # (§8.1): the page that says whether anyone will be told must work at
    # precisely the moment PostgreSQL is down.

    @app.get("/alarms", response_class=HTMLResponse)
    def alarms_page(request: Request):
        people = read_recipients()
        return page(request, "alarms.html",
                    active=state.active_alarms(),
                    limits=_limits_view(state),
                    people=people,
                    enabled_count=sum(1 for p in people if p.get("enabled")),
                    saved=request.query_params.get("saved"),
                    error=request.query_params.get("error"))

    @app.post("/alarms/recipients")
    def alarms_recipients(request: Request):
        """Save the recipient list (§4.4).

        **All or nothing, and validated before anything is written.** A
        half-saved list is a list nobody chose, and this one decides who finds
        out that something is wrong.
        """
        import asyncio
        from urllib.parse import quote
        form = asyncio.run(_form(request))
        who = operator_of(request, form.get("by", ""))

        before = read_recipients()
        people, removed = _people_from_form(form)

        problems = validate_recipients(people)
        if problems:
            return RedirectResponse(
                "/alarms?error=" + quote("; ".join(problems)),
                status_code=303)

        try:
            write_recipients(people, who)
        except OSError as exc:
            log.exception("could not write recipients.yaml")
            return RedirectResponse(
                "/alarms?error=" + quote(f"could not write the file: {exc}"),
                status_code=303)

        for line in _recipient_changes(before, people, removed):
            _audit(state, who, "recipients", line["target"],
                   line["old"], line["new"])
            log.warning("recipients: %s %s -> %s by %s", line["target"],
                        line["old"], line["new"], who)

        enabled = sum(1 for p in people if p.get("enabled"))
        note = f"saved {len(people)} recipient{'' if len(people) == 1 else 's'}"
        # Said after the save, not instead of it: a blank contact field is
        # somebody mid-edit far more often than it is a mistake, and taking
        # the whole list hostage over one is the wrong trade (§4.4).
        for warning in recipient_warnings(people):
            note += f" - WARNING: {warning}"
        if not enabled:
            # A warning, not a refusal: turning everyone off may be exactly
            # what somebody means to do during an intervention. The engine
            # raises its own alarm about it too (§4.4).
            note += " - WARNING: nobody is enabled, so alarms reach nobody"
        return RedirectResponse("/alarms?saved=" + quote(note),
                                status_code=303)

    @app.post("/alarms/notify")
    def alarms_notify(request: Request, enabled: str = Form(""),
                      by: str = Form("")):
        """The master switch: turn alarm DELIVERY off or on (§4.4a).

        **This is the most dangerous control on the site**, and it is here
        because the alternative is worse. The slow control runs when the plant
        does not, and a fortnight of "the cryostat is warm" at three in the
        morning is how an operator learns to ignore the message that matters.
        Stopping that properly is a switch somebody threw on purpose, with
        their name on it - not a recipient list quietly emptied, not a service
        stopped, and not a threshold widened until it never fires again.

        What it does NOT do is make the system look well. The engine still
        evaluates, the pages still show every active alarm, the history is
        unbroken, and the badge at the top of every page says the alarms are
        off for as long as they are.
        """
        from urllib.parse import quote
        who = operator_of(request, by)
        wanted = enabled.strip().lower() in ("1", "true", "on", "yes")
        answer = state.command(TOPIC_NOTIFY, ACK_NOTIFY,
                               {"enabled": wanted, "by": who})
        if not answer.get("ok"):
            return RedirectResponse(
                "/alarms?error=" + quote(str(answer.get("reason", "refused"))),
                status_code=303)

        _audit(state, who, "alarm_notifications", "all",
               "on" if answer.get("was") else "off", "on" if wanted else "off")
        log.critical("ALARM NOTIFICATIONS %s by %s",
                     "ON" if wanted else "OFF", who)
        if wanted:
            still = answer.get("active") or []
            note = "alarm notifications are ON"
            if still:
                note += (" - %d channel(s) still in alarm will notify on "
                         "their next reading: %s"
                         % (len(still), ", ".join(still)))
        else:
            note = ("WARNING: alarm notifications are OFF - alarms are still "
                    "evaluated and shown here, but nobody is told")
        return RedirectResponse("/alarms?saved=" + quote(note),
                                status_code=303)

    @app.get("/mimic", response_class=HTMLResponse)
    def mimic(request: Request):
        """The P&ID with live values on it (§8.2).

        Answers the question the status table cannot — *where* is tt203, and
        what is it next to. It earns its own page rather than a place on the
        overview: that must answer "is everything all right?" in one glance
        with no scrolling, and a full P&ID needs zoom and attention. Both are
        wanted, at different moments.
        """
        svg_path = HERE / "static" / "xams_pid.svg"
        if not svg_path.exists():
            return HTMLResponse(
                "<p>The mimic has not been built. Run "
                "<code>python tools/build_mimic.py</code>.</p>", status_code=503)
        return page(request, "mimic.html",
                    svg=svg_path.read_text(encoding="utf-8"))

    @app.get("/logs", response_class=HTMLResponse)
    def logs(request: Request, service: str = "alarms", lines: int = 80,
             older: int = 0):
        """The last lines of each service log — saves logging in and hunting
        for files (§8.1).

        Newest first, because the reason anybody opens this page is the most
        recent thing a service said.

        `older=N` reads the Nth rotated file instead, `<service>.log.N`. The
        handler writes 10 MB before rotating and keeps 5 of them, so a busy
        service's last hour can already be in `.log.1` — without this the page
        showed almost nothing and looked like silence.
        """
        # Chosen from the whitelist, never pasted together out of what the URL
        # said: `logs/{service}.log` reads any .log file on the disk, loopback
        # binding or not.
        choices = _log_files()
        current = choices.get(service)
        rotations = [n for n in _ROTATIONS
                     if current is not None and _rotated(current, n).exists()]
        path = current if not older else (
            _rotated(current, older) if current is not None
            and older in _ROTATIONS else None)

        if path is None:
            text = "(no such log)"
        elif not path.exists():
            text = f"({path.name} does not exist — it has not rotated that far)"
        else:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                text = "\n".join(_newest_first(content.splitlines()[-lines:]))
            except Exception as exc:
                text = f"could not read {path}: {exc}"

        return page(request, "logs.html", available=sorted(choices),
                    selected=service, text=text, rotations=rotations,
                    older=older, lines=lines,
                    read_at=datetime.now().strftime("%H:%M:%S"))

    # --------------------------------------------------------------- api

    @app.get("/api/state")
    def api_state():
        """Everything the pages show, as JSON. For the Python client and for
        anything else that wants it without scraping HTML."""
        status, _ = state.overall()
        return JSONResponse({
            "overall": status,
            "config": state.config.config_hash,
            "services": state.services(),
            "alarms": state.active_alarms(),
            # Whether anybody would be told (§4.4a). A caller polling this to
            # decide "is the plant being watched" needs the switch as much as
            # it needs the alarms; `enabled: null` is the engine not saying.
            "notifications": state.notifications(),
            "faults": state.known_faults(),
            "channels": [
                {"name": c.name, "value": c.value, "unit": c.unit,
                 "quality": c.quality, "age_s": round(c.age_s, 1),
                 "alarm": c.alarm, "healthy": c.healthy}
                for c in state.channels()],
        })

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        status, _ = state.overall()
        return status

    # ------------------------------------------------------------- manual

    # The documentation, mounted rather than served from a second process.
    # The point is that it is present whenever the UI is: the lab PC cannot be
    # assumed to reach the internet, and a manual you can only read when the
    # network is healthy is missing exactly when it is wanted.
    #
    # HERE, not the working directory: this runs as a service, whose working
    # directory is not the repository.
    #
    # /manual, not /docs, although docs_url=None leaves /docs free — "docs" in
    # a FastAPI application means Swagger to anyone who has seen one before.
    manual = HERE / "site"
    if manual.is_dir():
        app.mount("/manual", StaticFiles(directory=str(manual), html=True),
                  name="manual")
    else:
        # Not fatal. A missing manual is a nuisance; refusing to start the
        # monitoring because of it would be a fault.
        log.warning("the manual has not been built — run "
                    "`python tools/build_docs.py`; /manual will return 404")

    return app
