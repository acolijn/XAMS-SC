"""Build the manual into the package, where the web UI serves it from.

    python tools/build_docs.py

Python rather than a shell script on purpose: this runs on the Windows lab PC
as well as on a laptop, and one command that works in both places is worth
more than two that each work in one. `install_services.ps1` calls it, so the
manual exists before the API starts.

Nothing here knows the current directory: the repository root is derived from
this file, so it can be run from anywhere and from a service account whose
working directory is not what you expect.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    try:
        import mkdocs  # noqa: F401
    except ImportError:
        print(
            "MkDocs is not installed in this environment.\n"
            "\n"
            "    python -m pip install -e \".[docs]\"\n"
            "\n"
            "On the lab PC use the virtual environment's interpreter "
            "explicitly:\n"
            "\n"
            "    .\\.venv\\Scripts\\python.exe -m pip install -e \".[docs]\"\n",
            file=sys.stderr,
        )
        return 1

    # `python -m mkdocs`, not the `mkdocs` shim: the shim lives in
    # .venv/Scripts, which is on PATH only inside an activated environment,
    # and services do not activate anything.
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict"],
        cwd=ROOT,
    )
    if result.returncode == 0:
        # Path comes from mkdocs.yml (site_dir); printed so a failure to find
        # it later is diagnosable from the build log alone.
        print(f"\nmanual built into {ROOT / 'src/xams_sc/api/site'}")
        print("served by the web UI at http://127.0.0.1:8000/manual")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
