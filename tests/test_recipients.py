"""The Alarms page and the recipient list. See DESIGN.md §4.4, §8.1, §11.

The alarm chain is the least visible part of this system and the most
consequential, and this page is where it becomes visible: what has fired, what
*would* fire, and who is told.

Two things here are worth more than the rest:

**Nobody enabled is a WARNING, not a refusal.** Turning everyone off may be
exactly what somebody means during an intervention. It is said loudly and
allowed.

**A recipient with neither an email nor a phone is WARNED ABOUT, not refused.**
They would sit on the list looking notified and hear nothing, which is the
silent failure this whole subsystem exists to avoid - so it is said, loudly and
by name. It is not a reason to refuse the save: a blank field is somebody
mid-edit far more often than it is a mistake, and one of them must not hold the
rest of the list hostage.
"""

import json
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from xams_sc import config as config_module
from xams_sc.api import app as app_module
from xams_sc.model import iso, utcnow
from xams_sc.config import (read_recipients, recipient_warnings,
                            validate_recipients, write_recipients)

REPO_CONFIG = config_module.ROOT / "config"


class FakeBus:
    def __init__(self):
        self.published = []
        self.handlers = {}
        # Set by a test that wants to play the service on the other end:
        # called with every published command, so an ack can be fed back the
        # way a running service would.
        self.on_publish = None

    def subscribe(self, topic, handler):
        self.handlers[topic] = handler

    def connect(self): pass
    def disconnect(self): pass
    def publish_state(self, service, state): pass
    def publish_heartbeat(self, service): pass
    def publish_measurement(self, m): pass

    def publish_raw(self, topic, payload, retain=False):
        self.published.append((topic, payload))
        if self.on_publish is not None:
            self.on_publish(topic, payload)


class StubDrift:
    def get(self):
        return {"state": "ok", "dashboards": [], "detail": "stubbed"}


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    d = tmp_path / "config"
    d.mkdir()
    for name in ("channels.yaml", "devices.yaml", "alarms.yaml",
                 "recipients.yaml"):
        shutil.copy(REPO_CONFIG / name, d / name)
    monkeypatch.setattr(config_module, "CONFIG_DIR", d)
    return d


@pytest.fixture
def client(config_dir, monkeypatch):
    bus = FakeBus()
    monkeypatch.setattr(app_module, "Bus", lambda **kw: bus)
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    app = app_module.create_app()
    return TestClient(app), app, bus


def rows(people, **extra):
    """The form a browser would post for this list, plus the spare row."""
    data = {}
    for i, p in enumerate(people):
        data[f"name-{i}"] = p.get("name", "")
        data[f"email-{i}"] = p.get("email", "")
        data[f"phone-{i}"] = p.get("phone", "")
        if p.get("enabled"):
            data[f"enabled-{i}"] = "on"
    data[f"name-{len(people)}"] = ""
    data[f"email-{len(people)}"] = ""
    data[f"phone-{len(people)}"] = ""
    data.update(extra)
    return data


# ------------------------------------------------------------- validation

def test_blank_phone_is_allowed():
    """An empty phone MEANS "do not SMS"; it is not a missing number."""
    assert validate_recipients(
        [{"name": "A", "email": "a@nikhef.nl", "phone": "", "enabled": True}]
    ) == []


def test_neither_email_nor_phone_is_allowed_but_warned_about():
    person = [{"name": "A", "email": "", "phone": "", "enabled": True}]
    assert validate_recipients(person) == []
    warnings = recipient_warnings(person)
    assert len(warnings) == 1
    assert "hear nothing" in warnings[0]
    assert "A" in warnings[0]


def test_a_reachable_person_warns_about_nothing():
    """A blank phone alone is silent, not a warning: it MEANS "do not SMS"."""
    assert recipient_warnings(
        [{"name": "A", "email": "a@nikhef.nl", "phone": ""}]) == []
    assert recipient_warnings(
        [{"name": "B", "email": "", "phone": "+31612345678"}]) == []


def test_phone_needs_a_country_code():
    problems = validate_recipients(
        [{"name": "A", "email": "a@x.nl", "phone": "0600000000"}])
    assert any("country code" in p for p in problems)


def test_malformed_email_is_refused():
    problems = validate_recipients([{"name": "A", "email": "a.nikhef.nl"}])
    assert any("not an email" in p for p in problems)


def test_missing_name_is_refused():
    problems = validate_recipients([{"name": "", "email": "a@x.nl"}])
    assert any("no name" in p for p in problems)


def test_duplicate_names_are_refused():
    problems = validate_recipients([
        {"name": "Ada", "email": "a@x.nl"},
        {"name": "ada", "email": "b@x.nl"},
    ])
    assert any("listed twice" in p for p in problems)


def test_the_real_list_is_valid():
    """The list actually in the repository must pass its own checks."""
    assert validate_recipients(read_recipients(REPO_CONFIG)) == []


# ------------------------------------------------------------ the file

def test_round_trip_keeps_the_header(config_dir):
    write_recipients([{"name": "Ada", "email": "a@x.nl", "phone": "",
                       "enabled": True}], "apc", config_dir)
    text = (config_dir / "recipients.yaml").read_text()
    assert text.startswith("# Who receives alarm notifications")
    assert "do not SMS this person" in text
    assert read_recipients(config_dir) == [
        {"name": "Ada", "email": "a@x.nl", "phone": "", "enabled": True}]


def test_missing_file_is_not_an_error(tmp_path):
    assert read_recipients(tmp_path) == []


# ------------------------------------------------------------- the page

def test_page_lists_everyone(client):
    http, _, _ = client
    text = http.get("/alarms").text
    assert "Auke-Pieter Colijn" in text
    assert "a.p.colijn@nikhef.nl" in text


def test_nav_carries_alarms(client):
    http, _, _ = client
    assert 'href="/alarms"' in http.get("/").text


def test_limits_unknown_when_engine_is_silent(client):
    """Nothing published means unknown, never "no thresholds" (§12)."""
    http, _, _ = client
    text = http.get("/alarms").text
    assert "Unknown" in text
    assert "has not published its" in text


def test_limits_shown_when_published(client):
    http, app, bus = client
    bus.handlers["xams/status/#"](
        "xams/status/limits",
        '{"pmain":{"high":2.1,"hihi":2.5,"low":0.9}}')
    text = http.get("/alarms").text
    assert "2.1" in text and "2.5" in text and "0.9" in text
    assert "pmain" in text


def test_adding_somebody(client, config_dir):
    http, _, _ = client
    people = read_recipients(config_dir)
    data = rows(people)
    data[f"name-{len(people)}"] = "Ada Lovelace"
    data[f"email-{len(people)}"] = "ada@nikhef.nl"
    data[f"enabled-{len(people)}"] = "on"
    response = http.post("/alarms/recipients", data=dict(data, by="apc"),
                         follow_redirects=False)
    assert response.status_code == 303
    assert "saved" in response.headers["location"]
    saved = read_recipients(config_dir)
    assert any(p["name"] == "Ada Lovelace" and p["enabled"] for p in saved)


def test_disabling_keeps_the_number(client, config_dir):
    """Notify off is not removal: the number survives a holiday (§4.4)."""
    http, _, _ = client
    people = read_recipients(config_dir)
    for p in people:
        if p["name"] == "Example Person 4":
            p["enabled"] = False
    http.post("/alarms/recipients", data=dict(rows(people), by="apc"),
              follow_redirects=False)
    saved = {p["name"]: p for p in read_recipients(config_dir)}
    assert saved["Example Person 4"]["enabled"] is False
    assert saved["Example Person 4"]["phone"] == "+31600000000"


def test_removing_somebody(client, config_dir):
    http, _, _ = client
    people = read_recipients(config_dir)
    index = [i for i, p in enumerate(people)
             if p["name"] == "Example Person 3"][0]
    data = rows(people)
    data[f"remove-{index}"] = "on"
    http.post("/alarms/recipients", data=dict(data, by="apc"),
              follow_redirects=False)
    saved = read_recipients(config_dir)
    assert all(p["name"] != "Example Person 3" for p in saved)
    assert len(saved) == len(people) - 1


def test_blank_phone_survives_a_save(client, config_dir):
    """The thing the user asked to keep: `phone: ""` stays an empty string."""
    http, _, _ = client
    http.post("/alarms/recipients",
              data=dict(rows(read_recipients(config_dir)), by="apc"),
              follow_redirects=False)
    body = yaml.safe_load((config_dir / "recipients.yaml").read_text())
    westveer = [p for p in body["recipients"]
                if p["name"] == "Example Person 2"][0]
    assert westveer["phone"] == ""


def test_a_bad_row_refuses_the_whole_save(client, config_dir):
    """All or nothing: a half-saved list is a list nobody chose."""
    http, _, _ = client
    before = (config_dir / "recipients.yaml").read_text()
    people = read_recipients(config_dir)
    data = rows(people)
    data[f"name-{len(people)}"] = "Nobody"
    data[f"email-{len(people)}"] = "not-an-address"
    data[f"enabled-{len(people)}"] = "on"
    response = http.post("/alarms/recipients", data=dict(data, by="apc"),
                         follow_redirects=False)
    assert "error" in response.headers["location"]
    assert (config_dir / "recipients.yaml").read_text() == before


def test_a_contactless_row_saves_with_a_warning(client, config_dir):
    """Somebody with no email and no phone is saved, loudly.

    Refusing it meant one half-filled row - a name typed while the number is
    looked up - refused every other change on the page with it.
    """
    http, _, _ = client
    people = read_recipients(config_dir)
    data = rows(people)
    data[f"name-{len(people)}"] = "Nobody"      # no email, no phone
    data[f"enabled-{len(people)}"] = "on"
    response = http.post("/alarms/recipients", data=dict(data, by="apc"),
                         follow_redirects=False)
    where = response.headers["location"]
    assert "saved" in where
    assert "WARNING" in where and "hear%20nothing" in where
    assert any(p["name"] == "Nobody" for p in read_recipients(config_dir))


def test_the_page_marks_who_hears_nothing(client, config_dir):
    """Marked where the blank is, not only in a banner that scrolls away."""
    http, _, _ = client
    people = read_recipients(config_dir)
    people.append({"name": "Nobody", "email": "", "phone": "",
                   "enabled": True})
    write_recipients(people, "apc", config_dir)
    assert "hears-nothing" in http.get("/alarms").text


def test_the_boxes_are_not_browser_default_white(client):
    """The inputs read as the dark panel they sit in, not as four lamps."""
    http, _, _ = client
    text = http.get("/alarms").text
    assert 'class="recip-box' in text
    assert 'style="width:95%"' not in text


def test_empty_spare_row_is_not_an_error(client, config_dir):
    """Saving without using the spare row must simply work."""
    http, _, _ = client
    before = read_recipients(config_dir)
    response = http.post("/alarms/recipients",
                         data=dict(rows(before), by="apc"),
                         follow_redirects=False)
    assert "saved" in response.headers["location"]
    # Against the list that was there, not a number: this fixture copies the
    # REAL recipients.yaml, and hard-coding its length made adding a colleague
    # break the test suite.
    assert len(read_recipients(config_dir)) == len(before)


def test_disabling_everyone_warns_but_is_allowed(client, config_dir):
    http, _, _ = client
    people = read_recipients(config_dir)
    for p in people:
        p["enabled"] = False
    response = http.post("/alarms/recipients",
                         data=dict(rows(people), by="apc"),
                         follow_redirects=False)
    assert "saved" in response.headers["location"]
    assert "WARNING" in response.headers["location"]
    assert all(not p["enabled"] for p in read_recipients(config_dir))


def test_the_page_says_so_when_nobody_is_enabled(client, config_dir):
    http, _, _ = client
    people = read_recipients(config_dir)
    for p in people:
        p["enabled"] = False
    write_recipients(people, "apc", config_dir)
    assert "alarms reach nobody" in http.get("/alarms").text


def test_changes_are_audited(client, config_dir):
    http, _, bus = client
    people = read_recipients(config_dir)
    for p in people:
        if p["name"] == "Example Person 4":
            p["enabled"] = False
    http.post("/alarms/recipients", data=dict(rows(people), by="apc"),
              follow_redirects=False)
    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    assert any('"action":"recipients"' in a and "Example Person 4" in a
               and "disabled" in a and '"actor":"apc"' in a for a in audits)


def test_removal_is_audited_with_what_was_lost(client, config_dir):
    """The file no longer mentions them, so the trail must (§4.4)."""
    http, _, bus = client
    people = read_recipients(config_dir)
    index = [i for i, p in enumerate(people)
             if p["name"] == "Example Person 3"][0]
    data = rows(people)
    data[f"remove-{index}"] = "on"
    http.post("/alarms/recipients", data=dict(data, by="apc"),
              follow_redirects=False)
    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    line = [a for a in audits if "Example Person 3" in a]
    assert line, audits
    assert '"new":"removed"' in line[0]
    # Their address as the file actually had it. Hard-coding one meant the
    # test failed when they changed jobs, which is not what it is watching.
    assert people[index]["email"] in line[0]


def test_an_unchanged_save_is_not_audited(client, config_dir):
    http, _, bus = client
    http.post("/alarms/recipients",
              data=dict(rows(read_recipients(config_dir)), by="apc"),
              follow_redirects=False)
    assert [p for topic, p in bus.published if topic == "xams/audit"] == []


def test_the_reload_notices_a_ticked_checkbox(client):
    """The auto-reload must not discard an unsent tick.

    A checkbox's `value` is "on" whether or not it is ticked, so comparing
    values alone cannot see one. This page is the first with checkboxes worth
    losing — **Notify** and **Remove** — and losing a *Remove* tick silently
    is the same class of bug as the HV boxes that reverted after ten seconds.
    """
    http, _, _ = client
    script = http.get("/alarms").text
    assert "defaultChecked" in script


# ------------------------------------------------- the master switch (§4.4a)
#
# One button that stops every alarm reaching anybody. It exists because the
# slow control runs when the plant does not, and the ways of getting that
# quiet WITHOUT a switch - stop the service, empty the recipient list, widen
# a threshold - all leave a system that looks armed and is not.
#
# So what is tested here is mostly the saying-so.

def notify_status(bus, enabled, by="apc", at="2026-09-20T10:00:00Z"):
    """What the alarm engine publishes, retained, about the switch."""
    bus.handlers["xams/status/#"](
        "xams/status/notify",
        json.dumps({"enabled": enabled, "by": by, "at": at}))


def test_unknown_until_the_engine_says(client):
    """Nothing published is unknown, never "on" (§12)."""
    http, _, _ = client
    text = http.get("/alarms").text
    assert "has not said whether it would notify" in text
    assert "disable all alarms" not in text


def test_the_button_is_offered_when_alarms_are_on(client):
    http, _, bus = client
    notify_status(bus, True)
    text = http.get("/alarms").text
    assert "disable all alarms" in text
    assert "Alarms are notifying" in text


def test_the_alarms_page_shouts_when_they_are_off(client):
    http, _, bus = client
    notify_status(bus, False)
    text = http.get("/alarms").text
    assert "ALARMS ARE DISABLED" in text
    assert "enable all alarms" in text
    assert "apc" in text


def test_the_overview_says_alarms_disabled_not_running(client):
    """The services row must say what the engine is DOING (§4.4a)."""
    http, _, bus = client
    bus.handlers["xams/status/#"]("xams/status/alarms/state", "running")
    notify_status(bus, False)
    text = http.get("/").text
    assert "alarms disabled" in text
    assert "ALARMS ARE DISABLED" in text


def test_the_overview_says_running_when_they_are_on(client):
    http, _, bus = client
    bus.handlers["xams/status/#"]("xams/status/alarms/state", "running")
    notify_status(bus, True)
    text = http.get("/").text
    assert "alarms disabled" not in text


def all_well(app, bus):
    """Every service beating and every channel fresh.

    The badge reports the worst thing it can find, so a fixture where nothing
    has reported yet says "N CHANNEL(S) NOT OK" and never gets as far as the
    switch. To test what the badge says about the switch, everything else has
    to be genuinely well first.
    """
    now = iso(utcnow())
    for service in ("cdaq", "caen", "lakeshore", "ups", "derived"):
        bus.handlers["xams/status/#"](
            f"xams/status/{service}/heartbeat", now)
    for ch in app.state.system.config.enabled_channels():
        bus.handlers["xams/meas/#"](
            f"xams/meas/{ch.name}",
            json.dumps({"t": now, "ch": ch.name, "v": 1.0,
                        "u": ch.unit, "q": "ok"}))


def test_the_badge_never_says_all_ok_with_alarms_off(client):
    """"ALL OK" on a system nobody would be told about is the sentence this
    whole subsystem exists to prevent."""
    http, app, bus = client
    all_well(app, bus)
    assert http.get("/healthz").text == "ALL OK"      # the fixture is sound

    notify_status(bus, False)
    assert http.get("/healthz").text == "OK — ALARMS DISABLED"
    # The page says it in its own words, above everything else on it.
    assert "ALARMS ARE DISABLED" in http.get("/").text


def test_a_live_alarm_still_outranks_the_switch(client):
    http, _, bus = client
    notify_status(bus, False)
    bus.handlers["xams/alarm/#"](
        "xams/alarm/pmain",
        '{"state":"major","threshold":"hihi","value":2.5}')
    assert "MAJOR ALARM" in http.get("/healthz").text


def test_api_state_carries_the_switch(client):
    http, _, bus = client
    notify_status(bus, False)
    body = http.get("/api/state").json()
    assert body["notifications"] == {"known": True, "enabled": False,
                                     "by": "apc", "at": "2026-09-20T10:00:00Z"}


def test_api_state_survives_a_channel_that_has_never_reported(client):
    """One silent channel must not take the whole endpoint down.

    A channel with no reading carries an age of infinity, which strict JSON
    cannot write - so `/api/state` raised and returned 500 for EVERY caller
    the moment any one channel went quiet. The machine-readable view of the
    system failed exactly when part of the system had stopped talking, which
    is the same failure the pages are built to avoid. `null` is how a service
    with no heartbeat already reports the same thing.
    """
    http, app, bus = client
    silent = list(app.state.system.config.enabled_channels())[0].name

    body = http.get("/api/state").json()          # nothing has reported yet
    ages = {c["name"]: c["age_s"] for c in body["channels"]}
    assert ages[silent] is None
    assert all(age is None for age in ages.values())

    all_well(app, bus)
    body = http.get("/api/state").json()
    assert all(isinstance(c["age_s"], float) for c in body["channels"])


def test_switching_it_off_is_a_command_and_is_audited(client):
    """The UI asks over the bus and reports what came back - it does not
    decide for itself that the alarms are off."""
    http, _, bus = client

    def answer(topic, payload):
        # Only the command. The handler audits on the same bus, and a fake
        # service that tried to acknowledge the audit line died on it.
        if topic != "xams/cmd/alarms/notify":
            return
        request = json.loads(payload)
        bus.handlers["xams/ack/alarms/notify"](
            "xams/ack/alarms/notify",
            json.dumps({"ok": True, "enabled": request["enabled"],
                        "was": True, "active": ["pmain"],
                        "by": request["by"]}))

    bus.on_publish = answer
    response = http.post("/alarms/notify", data={"enabled": "0", "by": "apc"},
                         follow_redirects=False)
    assert response.status_code == 303
    assert "nobody%20is%20told" in response.headers["location"]

    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    assert any('"action":"alarm_notifications"' in a and '"new":"off"' in a
               and '"actor":"apc"' in a for a in audits)


def test_a_silent_engine_is_reported_as_a_refusal(client):
    """No ack means it did not happen, and the page must not claim it did."""
    http, app, _ = client
    app.state.system.command = lambda *a, **kw: {
        "ok": False, "reason": "the alarms service did not answer"}
    response = http.post("/alarms/notify", data={"enabled": "0", "by": "apc"},
                         follow_redirects=False)
    assert "error" in response.headers["location"]


# ------------------------------------------- the "acting as" suggestions

def test_the_operator_box_offers_the_recipients(client):
    """The names are typed a few times a day and end up in the audit trail,
    where `apc`, `AP Colijn` and `Auke-Pieter Colijn` are three people."""
    http, _, _ = client
    text = http.get("/").text
    assert 'list="operators"' in text
    assert '<option value="Example Person 4">' in text


def test_the_suggestions_are_on_every_page(client):
    http, _, _ = client
    for path in ("/", "/status", "/hv", "/alarms", "/logs"):
        assert '<option value="Example Person 4">' in http.get(path).text, (
            "%s offers no names under 'acting as'" % path)


def test_a_disabled_recipient_is_still_offered(client, config_dir):
    """Disabled means "do not notify me", which is usually somebody away -
    exactly when a colleague is the one at the keyboard."""
    http, _, _ = client
    people = read_recipients(config_dir)
    for p in people:
        if p["name"] == "Example Person 4":
            p["enabled"] = False
    http.post("/alarms/recipients", data=dict(rows(people), by="apc"),
              follow_redirects=False)

    assert '<option value="Example Person 4">' in http.get("/").text


def test_a_name_that_is_not_offered_is_still_accepted(client, config_dir):
    """The heart of it being a datalist and not a select. A student on shift
    is not an alarm recipient, and the alternative to typing their own name
    is picking a colleague's - which puts the WRONG name in the audit trail,
    worse than no name at all."""
    http, _, bus = client
    http.post("/operator", data={"operator": "Visiting Student"})

    people = read_recipients(config_dir)
    people[0]["phone"] = "+31000000000"
    http.post("/alarms/recipients", data=rows(people),   # no `by` on the form
              follow_redirects=False)

    audits = [p for topic, p in bus.published if topic == "xams/audit"]
    assert any('"actor":"Visiting Student"' in a for a in audits)


def test_the_names_follow_an_edit_without_a_restart(client, config_dir):
    """The list is cached on the file's timestamp, because it renders in the
    header of pages that reload every ten seconds. A cache that outlived an
    edit made on /alarms would be a stale list nobody could explain."""
    http, _, _ = client
    assert "Ada Lovelace" not in http.get("/").text

    people = read_recipients(config_dir)
    data = rows(people)
    data[f"name-{len(people)}"] = "Ada Lovelace"
    data[f"email-{len(people)}"] = "ada@nikhef.nl"
    data[f"enabled-{len(people)}"] = "on"
    http.post("/alarms/recipients", data=dict(data, by="apc"),
              follow_redirects=False)

    assert '<option value="Ada Lovelace">' in http.get("/").text


def test_no_recipient_file_leaves_the_box_working(client, config_dir):
    """Nothing to suggest is not a broken header: it is a plain text field,
    which is what it was before any of this."""
    http, _, _ = client
    (config_dir / "recipients.yaml").unlink()

    text = http.get("/").text
    # Scoped to the datalist: the page has other <select>s of its own.
    offered = text.split('<datalist id="operators">')[1].split("</datalist>")[0]

    assert "<option" not in offered
    assert 'list="operators"' in text and 'name="operator"' in text
