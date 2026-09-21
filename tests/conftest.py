"""Shared fixtures. See DESIGN.md §2.1, and `doubles.py` for the stand-ins.

Every test file used to carry its own `FakeBus`, its own `StubDrift` and its
own `config_dir` — ten, four and four copies respectively. That is not merely
repetition. The copies drifted: one recorded parsed payloads and the rest
recorded strings, one keyed handlers by topic where the real bus keeps a list,
and when `recipients.yaml` left the repository three of the four `config_dir`
copies were updated and the fourth was not, which broke the suite on a fresh
clone.

So there is one of each here. A test that needs different behaviour should
still define its own — shadowing a fixture is normal — but it should be
because the test needs it, not because it was written before this file.
"""

from __future__ import annotations

import shutil

import pytest

from doubles import FakePg, RecordingBus, StubDrift  # noqa: F401  (re-exported)
from xams_sc import config as config_module

REPO_CONFIG = config_module.ROOT / "config"


@pytest.fixture
def bus():
    return RecordingBus()


@pytest.fixture
def config():
    """The real configuration, loaded from the repository."""
    return config_module.load()


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """A copy of the real configuration, so a test never writes into git."""
    d = tmp_path / "config"
    d.mkdir()
    for name in ("channels.yaml", "devices.yaml", "alarms.yaml"):
        shutil.copy(REPO_CONFIG / name, d / name)
    # ALWAYS the template, never config/recipients.yaml. That file holds real
    # colleagues' names and mobile numbers, it is gitignored, and a fresh clone
    # does not have one. Asserting against real people also put their numbers
    # in this file, which is how they ended up in a public repository once.
    shutil.copy(REPO_CONFIG / "recipients.example.yaml", d / "recipients.yaml")
    monkeypatch.setattr(config_module, "CONFIG_DIR", d)
    return d


@pytest.fixture
def webui(config_dir, bus, monkeypatch):
    """The web UI wired to the recording bus: (client, app, bus).

    `config_dir` comes first deliberately: `create_app()` reads the
    configuration at construction, so CONFIG_DIR must already point at the
    copy before the app is built.
    """
    from fastapi.testclient import TestClient

    from xams_sc.api import app as app_module

    monkeypatch.setattr(app_module, "Bus", lambda **kw: bus)
    monkeypatch.setattr(app_module, "DriftWatcher", StubDrift)
    app = app_module.create_app()
    app.state.bus_for_test = bus
    return TestClient(app), app, bus


@pytest.fixture
def client(webui):
    """Just the client, for tests that never look at the app or the bus."""
    return webui[0]


@pytest.fixture
def fake_pg():
    return FakePg()
