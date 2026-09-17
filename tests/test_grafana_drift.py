"""Dashboard drift detection. See DESIGN.md §12.

A dashboard that exists only in Grafana's own database is lost when that
database is, and `tools/save_dashboard.py --save` is what prevents that.
Nothing runs it for you, so the archive falls behind silently — which it did,
and which is why this check exists.

What these tests pin down is mostly what it must NOT say. A check that
reports drift when Grafana is merely stopped, or that reports everything fine
because it could not reach anything, is worse than no check at all: the first
trains people to ignore it, the second lets the archive rot while showing
green.
"""

import json

import pytest

from xams_sc import grafana


@pytest.fixture
def dashboard():
    return {"uid": "xams-overview", "title": "XAMS Overview",
            "panels": [{"title": "Pressures", "type": "timeseries"}]}


@pytest.fixture
def wired(monkeypatch, tmp_path, dashboard):
    """Point the module at a temporary archive and a fake Grafana."""
    monkeypatch.setattr(grafana, "ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(grafana, "_secrets",
                        lambda: {"grafana": {"token": "t",
                                             "url": "http://grafana"}})

    def write_archive(data):
        (tmp_path / "overview.json").write_text(json.dumps(data),
                                                encoding="utf-8")

    def serve(live):
        def _get(url, token):
            if "/api/search" in url:
                return [{"uid": d["uid"], "title": d["title"]} for d in live]
            uid = url.rsplit("/", 1)[-1]
            match = next(d for d in live if d["uid"] == uid)
            return {"dashboard": json.loads(json.dumps(match))}
        monkeypatch.setattr(grafana, "_get", _get)

    return write_archive, serve


class TestAgreement:
    def test_identical_is_ok(self, wired, dashboard):
        write_archive, serve = wired
        write_archive(dashboard)
        serve([dashboard])

        assert grafana.check()["state"] == "ok"

    def test_a_version_bump_alone_is_not_drift(self, wired, dashboard):
        """Grafana increments `version` on every save, including a save that
        changed nothing. Comparing it would report drift forever."""
        write_archive, serve = wired
        write_archive(dict(dashboard, version=3))
        serve([dict(dashboard, version=9, id=41)])

        assert grafana.check()["state"] == "ok"


class TestDisagreement:
    def test_an_edited_panel_is_drift(self, wired, dashboard):
        write_archive, serve = wired
        write_archive(dashboard)
        edited = json.loads(json.dumps(dashboard))
        edited["panels"][0]["title"] = "Pressures (bar)"
        serve([edited])

        result = grafana.check()
        assert result["state"] == "drift"
        assert "XAMS Overview" in result["detail"]

    def test_a_dashboard_never_saved_is_drift(self, wired, dashboard):
        """The case that loses work: created in the UI, never exported."""
        write_archive, serve = wired
        serve([dashboard])

        result = grafana.check()
        assert result["state"] == "drift"
        assert result["dashboards"][0]["state"] == "unsaved"

    def test_a_dashboard_only_in_git_is_drift(self, wired, dashboard):
        """Deleted from Grafana, or a restore that was never run."""
        write_archive, serve = wired
        write_archive(dashboard)
        serve([])

        result = grafana.check()
        assert result["state"] == "drift"
        assert result["dashboards"][0]["state"] == "missing"


class TestFailingSoftly:
    """Every one of these must be `unknown`, never `ok` and never `drift`."""

    def test_grafana_unreachable(self, wired, monkeypatch, dashboard):
        write_archive, _ = wired
        write_archive(dashboard)

        def explode(url, token):
            raise OSError("connection refused")

        monkeypatch.setattr(grafana, "_get", explode)
        result = grafana.check()

        assert result["state"] == "unknown"
        assert "not reachable" in result["detail"]

    def test_no_token_configured(self, monkeypatch):
        monkeypatch.setattr(grafana, "_secrets", lambda: {})

        result = grafana.check()

        assert result["state"] == "unknown"
        assert "no Grafana token" in result["detail"]

    def test_a_revoked_token_says_so(self, wired, monkeypatch, dashboard):
        import urllib.error
        write_archive, _ = wired
        write_archive(dashboard)

        def refuse(url, token):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr(grafana, "_get", refuse)
        result = grafana.check()

        assert result["state"] == "unknown"
        assert "revoked" in result["detail"]

    def test_unreadable_archive_file_is_not_silently_ok(self, wired, monkeypatch,
                                                        tmp_path, dashboard):
        """A corrupt file must not read as "nothing archived, all fine"."""
        write_archive, serve = wired
        (tmp_path / "overview.json").write_text("{ not json",
                                                encoding="utf-8")
        serve([dashboard])

        result = grafana.check()

        assert result["state"] == "drift"


class TestCaching:
    def test_the_second_call_does_not_ask_again(self, wired, dashboard):
        """The overview reloads every 10 s; a page view must not cost an HTTP
        round trip to Grafana."""
        write_archive, serve = wired
        write_archive(dashboard)
        serve([dashboard])

        calls = []
        original = grafana.check

        def counted():
            calls.append(1)
            return original()

        watcher = grafana.DriftWatcher()
        import unittest.mock as mock
        with mock.patch.object(grafana, "check", counted):
            watcher.get()
            watcher.get()
            watcher.get()

        assert len(calls) == 1

    def test_an_expired_cache_asks_again(self, wired, dashboard):
        write_archive, serve = wired
        write_archive(dashboard)
        serve([dashboard])

        calls = []
        watcher = grafana.DriftWatcher(cache_s=0.0)
        import unittest.mock as mock
        with mock.patch.object(grafana, "check",
                               lambda: calls.append(1) or {"state": "ok"}):
            watcher.get()
            watcher.get()

        assert len(calls) == 2
