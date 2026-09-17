"""Web UI. See DESIGN.md §8.1.

    python -m xams_sc.api

**Binds to 127.0.0.1, never 0.0.0.0.** `0.0.0.0` is the default in most
tutorials and in much example code; on this system it would expose the control
surface to the building network. Every bind address is explicit, and loopback
is the only accepted value without a recorded decision to the contrary (§8).

**Read-only.** Nothing here writes to an instrument. The control surface is
milestone 8 and needs §10's open decision resolved first. The one action the
UI offers is the flow-integrator reset, which changes a record rather than
any hardware, and which is audited like any other control action (§7.5).

Deliberately plain: server-rendered HTML, a meta refresh, no JavaScript
framework and no build step. In three years a student must be able to change
it without installing a toolchain (§8.1).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from ..bus import Bus
from ..config import load
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

    def page(request: Request, name: str, **context):
        status, css = state.overall()
        # state.config, not the copy captured at startup: it is replaced on
        # reload and the page must show the current one.
        return templates.TemplateResponse(request, name, {
            "state": state, "config": state.config, "overall": status,
            "overall_class": css, "alarms": state.active_alarms(),
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
                    ups=state.channels("ups"))

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
                channels.append({
                    "index": index,
                    "label": ch.name.replace("hv_", "").replace("_vmon", ""),
                    "description": ch.description,
                    "vmon": state.channel(ch.name),
                    "imon": state.channel(imon_name),
                    "expect": expect,
                    "limits": ch.limits or {},
                    "sign": ch.sign,
                })
            supplies.append({
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

    return app
