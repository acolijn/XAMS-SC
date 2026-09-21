"""The manual is served by the web UI itself, at /manual.

That is the whole point of building it into the package rather than running a
second server: the documentation is present whenever the UI is, including when
the building network is not. This pins the two failure modes that would make
that false — the mount silently missing, and a missing manual taking the
monitoring down with it.
"""

import pytest
from fastapi.testclient import TestClient

from xams_sc.api import app as app_module
from doubles import RecordingBus, StubDrift, ui_client  # noqa: F401

MANUAL = app_module.HERE / "site"
built = pytest.mark.skipif(
    not MANUAL.is_dir(),
    reason="the manual has not been built (python tools/build_docs.py)",
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "Bus", lambda **kw: RecordingBus())
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    return ui_client(app_module.create_app())


def test_app_starts_without_the_manual(monkeypatch):
    """An unbuilt manual is a nuisance, not a fault. The monitoring must come
    up regardless — this is the one that stops a documentation change from
    ever being able to stop the slow control."""
    monkeypatch.setattr(app_module, "Bus", lambda **kw: RecordingBus())
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    monkeypatch.setattr(app_module.Path, "is_dir", lambda self: False)
    client = ui_client(app_module.create_app())
    assert client.get("/healthz").status_code == 200
    assert client.get("/manual/").status_code == 404


@built
def test_manual_is_served(client):
    response = client.get("/manual/")
    assert response.status_code == 200
    assert "XAMS Slow Control" in response.text


@built
def test_generated_reference_pages_are_present(client):
    """The channel and alarm tables are generated from config/*.yaml at build
    time. If they stop being generated they stop being right, and nobody
    notices — so their presence is checked rather than assumed."""
    for path in ("/manual/reference/channels/", "/manual/reference/alarms/"):
        assert client.get(path).status_code == 200, path


@built
def test_every_nav_page_reached_the_site(client):
    """`strict: true` catches broken links inside the manual. This catches the
    other direction: a page that is in the navigation but never built."""
    for path in ("/manual/install/", "/manual/status/",
                 "/manual/operating/", "/manual/operating/running/",
                 "/manual/drivers/", "/manual/software/architecture/",
                 "/manual/software/config/", "/manual/grafana/",
                 "/manual/reference/api/", "/manual/reference/hardware/",
                 "/manual/DESIGN/"):
        assert client.get(path).status_code == 200, path


def test_the_manual_is_linked_from_every_page(client):
    """A manual nobody can find is a manual nobody reads. The link lives in
    the shared nav, so checking one page checks them all."""
    assert 'href="/manual"' in client.get("/").text
