"""Notice when Grafana's dashboards have drifted from git. See DESIGN.md §12.

Grafana owns the live dashboard and the UI can edit it freely;
`grafana/dashboards-archive/` is the copy git tracks, and
`tools/save_dashboard.py --save` is what copies one into the other. Nothing
runs that for you, so the archive falls behind silently — and a dashboard
that exists only in Grafana's own database is lost when that database is,
which is the thing §12 exists to prevent.

`--save` writes a file and stops there, so the archive being up to date is only
half the question: a file nobody has committed is still on one disk. The check
therefore compares Grafana against the files *and* the files against git.

**This module only ever looks.** It reads dashboards, compares them to the
files and asks `git status` about those files; it never saves, never loads,
never edits and never commits. It authenticates with a
Grafana service account whose role is Viewer, so that is also true at the
other end and not merely a promise made here. Saving and loading still take
the admin password, deliberately: those write.

Every failure is soft. Grafana being down, uninstalled, unreachable or
unconfigured is not an error worth interrupting anybody over — it means the
question cannot be answered right now, which is reported as `unknown` rather
than as drift. A monitoring page that cries wolf about its own plumbing
trains people to ignore it.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from .config import CONFIG_DIR, ROOT

log = logging.getLogger(__name__)

ARCHIVE_DIR = ROOT / "grafana" / "dashboards-archive"

# Fields Grafana maintains about the stored copy rather than about the
# dashboard. `version` changes on every save, so comparing it would report
# drift after a save that changed nothing.
VOLATILE = ("id", "version", "iteration")

# How long a result is reused before asking Grafana again. The web UI reloads
# every 10 s and this must not turn into an HTTP request per page view.
CACHE_S = 120.0


def _secrets() -> dict:
    path = CONFIG_DIR / "secrets.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("could not read secrets.yaml")
        return {}


def _get(url: str, token: str):
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read())


def _canonical(dashboard: dict) -> str:
    for key in VOLATILE:
        dashboard.pop(key, None)
    return json.dumps(dashboard, sort_keys=True)


def _archived() -> dict[str, tuple[Path, dict]]:
    """Every archived dashboard, by uid, as (file, contents).

    Keyed on the uid INSIDE each file rather than on the filename, because
    that is what `tools/save_dashboard.py` matches on. The path comes back
    with it because git is asked about the file by name.
    """
    out = {}
    if not ARCHIVE_DIR.is_dir():
        return out
    for path in sorted(ARCHIVE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("could not parse %s", path.name)
            continue
        if data.get("uid"):
            out[data["uid"]] = (path, data)
    return out


def _uncommitted() -> set[str]:
    """Archive filenames whose current contents are not in a commit.

    `--save` writes a file and stops there; only a commit puts a dashboard
    in git, and until then it still lives on this disk alone — which is the
    thing §12 exists to prevent. Staged-but-uncommitted counts as
    uncommitted, deliberately: `git add` is not a backup either.

    Best effort, and soft like everything else here. When git cannot be
    asked — not a checkout, git not installed, a deployment from a tarball —
    the answer is "nothing known to be uncommitted" and the check falls back
    to comparing Grafana against the files, as it did before.
    """
    try:
        done = subprocess.run(
            ["git", "status", "--porcelain", "--", str(ARCHIVE_DIR)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=5)
    except Exception as exc:
        log.debug("could not ask git about the archive (%s)", exc)
        return set()
    if done.returncode != 0:
        log.debug("git status failed: %s", done.stderr.strip())
        return set()
    names = set()
    for line in done.stdout.splitlines():
        entry = line[3:].strip()
        if not entry:
            continue
        # A rename reads "old -> new"; the file on disk is the new one.
        entry = entry.rsplit(" -> ", 1)[-1].strip('"')
        names.add(Path(entry).name)
    return names


def check() -> dict:
    """Compare Grafana against the archive.

    Returns {"state": ..., "detail": ..., "dashboards": [...]} where state is:

      ok        every dashboard in Grafana matches its file, and git has
                that file's current contents
      drift     at least one differs, exists on only one side, or has been
                exported but never committed
      unknown   Grafana could not be reached, or no token is configured
    """
    config = (_secrets().get("grafana") or {})
    token = config.get("token")
    if not token:
        return {"state": "unknown", "dashboards": [],
                "detail": "no Grafana token in secrets.yaml"}
    base = (config.get("url") or "http://127.0.0.1:3000").rstrip("/")

    try:
        found = _get(base + "/api/search?type=dash-db&query=", token)
    except urllib.error.HTTPError as exc:
        detail = ("Grafana rejected the token (%s); it may have been revoked"
                  % exc.code) if exc.code in (401, 403) else \
                 ("Grafana returned %s" % exc.code)
        return {"state": "unknown", "dashboards": [], "detail": detail}
    except Exception as exc:
        return {"state": "unknown", "dashboards": [],
                "detail": "Grafana not reachable at %s (%s)" % (base, exc)}

    archive = _archived()
    uncommitted = _uncommitted()
    uids = {d["uid"] for d in found
            if "xams" in (d.get("uid", "") + d.get("title", "")).lower()}

    dashboards = []
    for uid in sorted(uids | set(archive)):
        path, saved = archive.get(uid, (None, {}))
        title = next((d.get("title") for d in found if d.get("uid") == uid),
                     None) or saved.get("title") or uid
        if uid not in archive:
            dashboards.append({"uid": uid, "title": title, "state": "unsaved"})
            continue
        if uid not in uids:
            dashboards.append({"uid": uid, "title": title, "state": "missing"})
            continue
        try:
            live = _get(base + "/api/dashboards/uid/" + uid, token)["dashboard"]
        except Exception as exc:
            return {"state": "unknown", "dashboards": [],
                    "detail": "could not read %s (%s)" % (uid, exc)}
        if _canonical(live) != _canonical(dict(saved)):
            state = "differs"
        elif path.name in uncommitted:
            # The file matches Grafana, so `--save` has nothing left to do;
            # what is missing is the commit. Reported separately because the
            # remedy is a different command.
            state = "uncommitted"
        else:
            state = "ok"
        dashboards.append({"uid": uid, "title": title, "state": state})

    bad = [d for d in dashboards if d["state"] != "ok"]
    if not dashboards:
        return {"state": "unknown", "dashboards": [],
                "detail": "no XAMS dashboards found"}
    if not bad:
        return {"state": "ok", "dashboards": dashboards,
                "detail": "%d dashboard(s) match git" % len(dashboards)}
    return {"state": "drift", "dashboards": dashboards,
            "detail": "%s" % ", ".join(
                "%s (%s)" % (d["title"], d["state"]) for d in bad)}


class DriftWatcher:
    """`check()` with a cache and a lock, for callers on a request path.

    The web UI reloads every 10 s and a page view must not wait on an HTTP
    round trip to Grafana, nor make one per view. The first call after the
    cache expires pays for the refresh; everything else gets the last answer.
    """

    def __init__(self, cache_s: float = CACHE_S):
        self.cache_s = cache_s
        self._lock = threading.Lock()
        self._result: dict | None = None
        self._at = 0.0

    def get(self) -> dict:
        with self._lock:
            fresh = self._result is not None and \
                time.monotonic() - self._at < self.cache_s
            if fresh:
                return self._result
        result = check()
        with self._lock:
            self._result, self._at = result, time.monotonic()
        return result
