"""Pull dashboards out of Grafana and into git. See DESIGN.md §12.

    python tools/save_dashboard.py                 # every XAMS dashboard
    python tools/save_dashboard.py xams-overview   # just one
    python tools/save_dashboard.py --check         # report drift, write nothing

**Run this after editing a dashboard in the Grafana UI.** Grafana keeps
dashboards in its own internal database; without them in git they are lost when
that database is lost or Grafana is reinstalled, taking hours of work with
them. That is the whole reason §12 asks for provisioning.

WHICH DIRECTION WINS, AND WHEN.

`allowUiUpdates: true` in the provisioning config means the UI can save, and
Grafana then treats its stored copy as newer than the file. Two consequences,
and the second one cost twenty minutes on 17 September 2026:

  * Grafana -> file: this tool. Run it after a UI edit, then commit.
  * file -> Grafana: editing `grafana/dashboards/*.json` by hand is NOT picked
    up on its own once the dashboard has been saved from the UI. Restart
    Grafana to make the file win again:

        Restart-Service Grafana

`--check` reports whether the live dashboard and the file have diverged, which
is the question worth asking before a `git pull` or a reinstall.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# NOT the provisioned directory.
#
# Grafana refuses every UI save of a provisioned dashboard, and the setting
# that would allow it (allowUiUpdates) is only read at service start — which
# needs admin on this machine and could not be done. Fighting that cost an
# afternoon, so dashboards are no longer provisioned from a file at all.
#
# Instead: Grafana owns the live dashboard and the UI can edit it freely, and
# this directory is the ARCHIVE that git tracks. `--save` copies Grafana into
# it, `--load` puts it back into a fresh Grafana. The §12 requirement is that
# a dashboard survives losing Grafana's database, and an archive plus a load
# command satisfies that just as well as provisioning did.
DASHBOARD_DIR = ROOT / "grafana" / "dashboards-archive"

# Fields Grafana adds that describe the stored copy rather than the dashboard.
# Keeping them would make every export differ from the last for no real reason.
VOLATILE = ("id", "version", "iteration")


def api(base: str, auth: str, path: str):
    req = urllib.request.Request(base + path,
                                 headers={"Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def path_for(uid: str) -> Path:
    """Where this dashboard's file already lives, or where it should go.

    Matches on the uid INSIDE each file rather than on the filename, so a
    dashboard keeps its existing file whatever that file is called. Naming the
    file after the uid unconditionally would quietly create a second copy
    beside the first, and provisioning would then load both.
    """
    for candidate in sorted(DASHBOARD_DIR.glob("*.json")):
        try:
            if json.loads(candidate.read_text(encoding="utf-8")).get("uid") == uid:
                return candidate
        except Exception:
            continue
    return DASHBOARD_DIR / f"{uid}.json"


def load_into_grafana(args) -> int:
    """Restore the archived dashboards into Grafana.

    This is what makes the install reproducible (§12): after a reinstall,
    Grafana has no dashboards and this puts them all back.
    """
    if not args.password:
        print("Grafana password needed: pass --password or set GRAFANA_PASSWORD.",
              file=sys.stderr)
        return 2
    auth = base64.b64encode(f"{args.user}:{args.password}".encode()).decode()

    files = sorted(DASHBOARD_DIR.glob("*.json"))
    if not files:
        print(f"No dashboards in {DASHBOARD_DIR.relative_to(ROOT)}.")
        return 1

    for path in files:
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        dashboard.pop("id", None)
        dashboard["version"] = 0
        payload = json.dumps({"dashboard": dashboard, "overwrite": True,
                              "message": f"loaded from {path.name}"}).encode()
        req = urllib.request.Request(args.url + "/api/dashboards/db", data=payload,
            headers={"Authorization": "Basic " + auth,
                     "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status = json.loads(r.read()).get("status")
            print(f"  {path.name:24s} loaded ({status})")
        except urllib.error.HTTPError as exc:
            print(f"  {path.name:24s} FAILED {exc.code}: "
                  f"{exc.read()[:120].decode(errors='replace')}")
            return 1
    return 0


def normalise(dashboard: dict) -> dict:
    clean = {k: v for k, v in dashboard.items() if k not in VOLATILE}
    clean["id"] = None
    return clean


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("uid", nargs="*", help="dashboard uid(s); default is all XAMS ones")
    p.add_argument("--url", default="http://127.0.0.1:3000")
    p.add_argument("--user", default=os.environ.get("GRAFANA_USER", "admin"))
    p.add_argument("--password", default=os.environ.get("GRAFANA_PASSWORD"))
    p.add_argument("--check", action="store_true",
                   help="report drift between Grafana and the files; write nothing")
    p.add_argument("--load", action="store_true",
                   help="push the archived files INTO Grafana (use on a fresh install)")
    args = p.parse_args(argv)

    if args.load:
        return load_into_grafana(args)

    if not args.password:
        print("Grafana password needed: pass --password or set GRAFANA_PASSWORD.",
              file=sys.stderr)
        return 2

    auth = base64.b64encode(f"{args.user}:{args.password}".encode()).decode()

    try:
        if args.uid:
            uids = args.uid
        else:
            found = api(args.url, auth, "/api/search?type=dash-db&query=")
            uids = [d["uid"] for d in found
                    if "xams" in (d.get("uid", "") + d.get("title", "")).lower()]
    except urllib.error.HTTPError as exc:
        print(f"Grafana refused the request: {exc.code} {exc.reason}", file=sys.stderr)
        if exc.code == 401:
            print("Check the password.", file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print(f"Cannot reach Grafana at {args.url}: {exc.reason}", file=sys.stderr)
        return 2

    if not uids:
        print("No XAMS dashboards found in Grafana.")
        return 1

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    drifted, written = [], []

    for uid in uids:
        payload = api(args.url, auth, f"/api/dashboards/uid/{uid}")
        dashboard = normalise(payload["dashboard"])
        title = dashboard.get("title", uid)

        path = path_for(uid)
        new = json.dumps(dashboard, indent=2, ensure_ascii=False)

        old = None
        if path.exists():
            try:
                old = json.dumps(normalise(json.loads(path.read_text(encoding="utf-8"))),
                                 indent=2, ensure_ascii=False)
            except Exception:
                old = None

        if old == new:
            print(f"  {uid:20s} unchanged  ({title})")
            continue

        drifted.append(uid)
        if args.check:
            print(f"  {uid:20s} DIFFERS from {path.relative_to(ROOT)}  ({title})")
            continue

        path.write_text(new, encoding="utf-8")
        written.append(path)
        print(f"  {uid:20s} saved to {path.relative_to(ROOT)}  ({title})")

    if args.check:
        if drifted:
            print(f"\n{len(drifted)} dashboard(s) differ from the files in git.")
            print("Run without --check to bring the files up to date.")
            return 1
        print("\nEverything in Grafana matches the files in git.")
        return 0

    if written:
        print(f"\n{len(written)} file(s) updated. Commit them:")
        print("  git add grafana/dashboards && git commit -m 'grafana: update dashboards'")
        print("\nA dashboard that exists only in Grafana's own database is lost")
        print("when that database is.")
    else:
        print("\nNothing to do — the files already match Grafana.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
