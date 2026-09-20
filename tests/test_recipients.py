"""The Alarms page and the recipient list. See DESIGN.md §4.4, §8.1, §11.

The alarm chain is the least visible part of this system and the most
consequential, and this page is where it becomes visible: what has fired, what
*would* fire, and who is told.

Two things here are worth more than the rest:

**Nobody enabled is a WARNING, not a refusal.** Turning everyone off may be
exactly what somebody means during an intervention. It is said loudly and
allowed.

**A recipient with neither an email nor a phone is refused.** They would sit on
the list looking notified and hear nothing, which is the silent failure this
whole subsystem exists to avoid.
"""

import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from xams_sc import config as config_module
from xams_sc.api import app as app_module
from xams_sc.config import (read_recipients, validate_recipients,
                            write_recipients)

REPO_CONFIG = config_module.ROOT / "config"


class FakeBus:
    def __init__(self):
        self.published = []
        self.handlers = {}

    def subscribe(self, topic, handler):
        self.handlers[topic] = handler

    def connect(self): pass
    def disconnect(self): pass
    def publish_state(self, service, state): pass
    def publish_heartbeat(self, service): pass
    def publish_measurement(self, m): pass

    def publish_raw(self, topic, payload, retain=False):
        self.published.append((topic, payload))


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


def test_neither_email_nor_phone_is_refused():
    problems = validate_recipients(
        [{"name": "A", "email": "", "phone": "", "enabled": True}])
    assert len(problems) == 1
    assert "hear nothing" in problems[0]


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
    http, _, _ = client
    before = (config_dir / "recipients.yaml").read_text()
    people = read_recipients(config_dir)
    data = rows(people)
    data[f"name-{len(people)}"] = "Nobody"      # no email, no phone
    data[f"enabled-{len(people)}"] = "on"
    response = http.post("/alarms/recipients", data=dict(data, by="apc"),
                         follow_redirects=False)
    assert "error" in response.headers["location"]
    assert (config_dir / "recipients.yaml").read_text() == before


def test_empty_spare_row_is_not_an_error(client, config_dir):
    """Saving without using the spare row must simply work."""
    http, _, _ = client
    response = http.post("/alarms/recipients",
                         data=dict(rows(read_recipients(config_dir)), by="apc"),
                         follow_redirects=False)
    assert "saved" in response.headers["location"]
    assert len(read_recipients(config_dir)) == 4


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
    assert "r.j.j.vabeek@student.hhs.nl" in line[0]


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
