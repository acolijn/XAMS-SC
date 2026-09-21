"""Cross-site requests to the web UI. See DESIGN.md §8, §8.1, §10.

The UI binds to 127.0.0.1 and has no login, and until this check existed the
loopback bind was doing all the work. It cannot: it keeps the BUILDING
network out, not the browser already running on the lab PC. Any page an
operator opens there can submit a hidden form to 127.0.0.1:8000, and a plain
form POST takes no CORS preflight - the browser sends it, the command runs,
and the `by` field carries whatever name the attacker chose into the audit
trail.

What these tests hold down:

  * the writes that reach an instrument (`/hv/output`, `/hv/apply`, the Lake
    Shore pair) cannot be driven from another site
  * neither can `/alarms/notify`, which is the one control that can make a
    sick plant look quiet
  * a `Host` this server does not answer to is refused outright, which is
    what stops DNS rebinding from reading /api/state and the recipient list
  * the pages themselves still work, GET and POST, from the UI's own origin

The last one matters as much as the rest. A check that refuses everything
passes the first four and leaves nobody able to turn the heater off.
"""

import pytest

# Every route that changes something. Kept as one list so a control added to
# the UI without a line here shows up as a gap rather than as silence.
WRITES = [
    ("/hv/output", {"channel": "hv_cathode_vset", "on": "0"}),
    ("/hv/apply", {"hv_cathode_vset": "-1000"}),
    ("/hv/defaults", {"hv_cathode_vset": "-1000"}),
    ("/lakeshore/setpoint", {"value": "-100"}),
    ("/lakeshore/range", {"range": "off"}),
    ("/flow/reset", {"by": "someone"}),
    ("/alarms/notify", {"enabled": "0"}),
    ("/alarms/recipients", {"name-0": "x", "email-0": "x@example.org"}),
    ("/operator", {"operator": "somebody else"}),
]

EVIL = "https://evil.example"


@pytest.mark.parametrize("path,data", WRITES)
def test_refuses_a_post_from_another_site(csrf_client, path, data):
    """The attack itself: a form on a page the operator happened to open."""
    r = csrf_client.post(path, data=data, headers={"origin": EVIL},
                         follow_redirects=False)
    assert r.status_code == 403, path


@pytest.mark.parametrize("path,data", WRITES)
def test_refuses_a_post_with_no_origin_at_all(csrf_client, path, data):
    """A browser old enough not to send `Origin`, or a curl one-liner.

    Refused rather than trusted. Nothing in the repository posts to this
    port - the CLI talks MQTT - so there is no caller to keep working, and
    "no Origin" is indistinguishable from an attacker who stripped it.
    """
    r = csrf_client.post(path, data=data, follow_redirects=False)
    assert r.status_code == 403, path


@pytest.mark.parametrize("path,data", WRITES)
def test_referer_stands_in_for_a_missing_origin(csrf_client, path, data):
    """No `Origin` but a same-site `Referer` is the UI's own form, on an
    older browser. Accepted: anything but 403 means it reached the handler."""
    r = csrf_client.post(path, data=data, follow_redirects=False,
                         headers={"referer": "http://127.0.0.1:8000/hv"})
    assert r.status_code != 403, path


def test_nothing_reaches_the_bus_when_a_post_is_refused(csrf_client, bus):
    """The refusal happens BEFORE the handler, not inside it.

    A 403 with the command already on `xams/cmd/#` would be a test passing
    over a high voltage that had moved.
    """
    csrf_client.post("/hv/output",
                     data={"channel": "hv_cathode_vset", "on": "0"},
                     headers={"origin": EVIL}, follow_redirects=False)
    assert bus.published == []


@pytest.mark.parametrize("host", ["evil.example", "attacker.test:8000",
                                  "127.0.0.1.evil.example:8000",
                                  "localhost.evil.example", ""])
def test_refuses_an_unknown_host_header(csrf_client, host):
    """DNS rebinding: the attacker's own domain, re-pointed at 127.0.0.1.

    By then the browser calls it same-origin and `Origin` is no help, so the
    name in the `Host` header is the only thing left that gives it away.
    Checked on GET too - the point of the attack is READING /api/state and
    the recipient list, names and mobile numbers included.
    """
    assert csrf_client.get("/api/state",
                           headers={"host": host}).status_code == 421


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000",
                                  "[::1]:8000", "127.0.0.1", "localhost",
                                  "localhost:8080", "127.0.0.1:9999",
                                  "[::1]:31337"])
def test_accepts_any_port_on_a_loopback_name(csrf_client, host):
    """THE SSH TUNNEL. §8 names it as the way in from outside this machine.

    The forwarded port is chosen on the far end and the browser puts THAT
    port in the Host header, so `ssh -L 8080:127.0.0.1:8000` arrives as
    `localhost:8080`. A colleague whose own 8000 is already taken has no
    other option, and refusing it meant a dead page on the one route that
    exists for an emergency.

    Matching the port bought no protection: rebinding puts the attacker's
    OWN name in this header, which is what the test above still refuses.
    """
    assert csrf_client.get("/api/state",
                           headers={"host": host}).status_code == 200


def test_a_tunnel_on_another_port_can_still_post(csrf_client):
    """Reading through the tunnel is no use if nothing can be changed."""
    r = csrf_client.post("/flow/reset", data={"by": "ap"},
                         headers={"host": "localhost:8080",
                                  "origin": "http://localhost:8080"},
                         follow_redirects=False)
    assert r.status_code == 303


def test_a_name_that_merely_contains_a_loopback_name_is_refused(csrf_client):
    """`127.0.0.1.evil.example` is a domain the attacker owns.

    Split on the port separator, not searched for a substring - the whole
    name has to be one this server answers to.
    """
    for host in ("127.0.0.1.evil.example", "evil.example:8000",
                 "notlocalhost", "localhosts"):
        assert csrf_client.get("/api/state",
                               headers={"host": host}).status_code == 421, host


def test_the_ui_still_works_from_its_own_origin(client):
    """The other half. `client` sends Host and Origin as a browser does."""
    assert client.get("/").status_code == 200
    r = client.post("/flow/reset", data={"by": "ap"}, follow_redirects=False)
    assert r.status_code == 303


def test_a_port_other_than_8000_does_not_lock_itself_out(config_dir, bus,
                                                          monkeypatch):
    """`--port 8080` must not produce a UI that refuses every request.

    The allowed hosts come from the bind arguments for this reason; a
    hardcoded 8000 would fail the first time somebody moved the port, with
    nothing on screen to say why.
    """
    from fastapi.testclient import TestClient

    from doubles import StubDrift
    from xams_sc.api import app as app_module

    monkeypatch.setattr(app_module, "Bus", lambda **kw: bus)
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    app = app_module.create_app(http_host="127.0.0.1", http_port=8080)
    moved = TestClient(app, base_url="http://127.0.0.1:8080",
                       headers={"origin": "http://127.0.0.1:8080"})
    assert moved.get("/api/state").status_code == 200
    assert moved.post("/flow/reset", data={"by": "ap"},
                      follow_redirects=False).status_code in (303, 200)
    # Another site is still another site, whatever port it claims.
    assert moved.post("/flow/reset", data={"by": "ap"},
                      headers={"origin": "https://evil.example:8080"},
                      follow_redirects=False).status_code == 403
