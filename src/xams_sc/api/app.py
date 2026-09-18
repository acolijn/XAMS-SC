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
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..bus import (ACK_LS_RANGE, ACK_LS_SETPOINT, TOPIC_LS_RANGE,
                   TOPIC_LS_SETPOINT, Bus)
from ..config import load
from ..hv_status import (describe_status, is_disabled, is_enabled,
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
                vset_view = state.channel(ch.name.replace("_vmon", "_vset"))

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
                armed = None
                if (vset_view is not None and vset_view.healthy
                        and vset_view.value is not None):
                    enabled = (word is not None and is_enabled(word))
                    armed = (not enabled) and abs(vset_view.value) > 1.0

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
                    "armed": armed,
                    "stat": stat_view,
                    "energised": None if word is None else is_enabled(word),
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
        return page(request, "hv.html", supplies=supplies)

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
    def logs(request: Request, service: str = "alarms", lines: int = 80):
        """The last lines of each service log — saves logging in and hunting
        for files (§8.1)."""
        log_dir = Path("logs")
        available = sorted(p.stem for p in log_dir.glob("*.log"))
        text = "(no such log)"
        path = log_dir / f"{service}.log"
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                text = "\n".join(content.splitlines()[-lines:])
            except Exception as exc:
                text = f"could not read {path}: {exc}"
        return page(request, "logs.html", available=available,
                    selected=service, text=text)

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
