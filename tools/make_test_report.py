"""Run the test suite and write a report of what ran and what it covers.

    python tools/make_test_report.py              test_report.md
    python tools/make_test_report.py --pdf        and test_report.pdf

Generated rather than written by hand, for the reason `gen_doc_pages.py`
gives about the channel tables: a hand-maintained list of what the tests
cover is wrong within a month, and a test report that is wrong is worse than
none, because it is quoted.

The per-file descriptions are the first sentence of each test module's own
docstring. They are therefore maintained in the place somebody is already
looking when they change a test, and a file with nothing to say about itself
shows up in the report as blank rather than as a description of what it used
to do.

Coverage is included when the `coverage` package is importable and left out
when it is not. It is not in `[dev]`; requiring it here would mean the report
cannot be produced on a machine where the tests themselves run perfectly
well.

Both outputs are git-ignored: this is a snapshot of one run on one machine,
not a source file. Re-run it rather than reading an old one.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


# --------------------------------------------------------------- running

def run_suite(junit: Path, coverage_json: Path | None) -> int:
    """Run pytest once, under coverage where that is available."""
    if coverage_json is not None:
        command = [sys.executable, "-m", "coverage", "run",
                   "--source", "src/xams_sc", "-m", "pytest"]
    else:
        command = [sys.executable, "-m", "pytest"]
    # -p no:cacheprovider: a report run must not leave .pytest_cache behind
    # in a tree somebody is about to commit from.
    command += ["-q", "-p", "no:cacheprovider", f"--junitxml={junit}"]

    result = subprocess.run(command, cwd=ROOT)
    if coverage_json is not None:
        subprocess.run([sys.executable, "-m", "coverage", "json",
                        "-o", str(coverage_json), "--quiet"], cwd=ROOT)
    return result.returncode


# --------------------------------------------------------------- reading

def summary_of(module: str) -> str:
    """The first sentence of a test module's docstring.

    The docstrings in this suite open with one line saying what the file
    holds down, which is exactly the column wanted here. Everything after
    that first sentence is the story of the bug it was written for, which
    belongs in the file and not in a table.
    """
    path = TESTS / f"{module}.py"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    match = re.match(r'\s*(?:#[^\n]*\n)*\s*"""(.*?)"""', text, re.S)
    if not match:
        return ""
    first = " ".join(match.group(1).strip().split("\n\n")[0].split())
    # The first sentence only. Everything after it is the story of the bug the
    # file was written for, which belongs in the file, and the pointer to the
    # design document, which would end every row of the table in the same
    # noise.
    #
    # A sentence ends at a full stop NOT preceded by a digit: these docstrings
    # are full of `§4.3` and `§8.2a`, and splitting on a bare full stop cut
    # them in half - "flow integrator.1, §7.5." was in the first report this
    # produced.
    end = re.search(r"(?<!\d)\.(?=\s|$)", first)
    return first[:end.end()] if end else first


def read_results(junit: Path) -> tuple[dict, list[dict]]:
    root = ET.parse(junit).getroot()
    suite = root.find("testsuite")
    cases = []
    for case in suite.iter("testcase"):
        classname = case.get("classname", "")
        parts = classname.split(".")
        # `tests.test_alarms.TestThresholds` and `tests.test_alarms` both
        # carry the module in the same position.
        module = parts[1] if len(parts) > 1 and parts[0] == "tests" else parts[0]
        outcome = "passed"
        reason = ""
        for tag, name in (("failure", "failed"), ("error", "error"),
                          ("skipped", "skipped")):
            found = case.find(tag)
            if found is not None:
                outcome = name
                reason = (found.get("message") or "").strip()
                break
        cases.append({"module": module, "name": case.get("name", ""),
                      "time": float(case.get("time", 0.0)),
                      "outcome": outcome, "reason": reason,
                      "classname": classname})
    return dict(suite.attrib), cases


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


# --------------------------------------------------------------- writing

def table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    out.append("")
    return out


def build(attrs: dict, cases: list[dict], coverage: dict | None) -> str:
    total = int(attrs.get("tests", 0))
    failures = int(attrs.get("failures", 0))
    errors = int(attrs.get("errors", 0))
    skipped = int(attrs.get("skipped", 0))
    passed = total - failures - errors - skipped
    duration = float(attrs.get("time", 0.0))

    lines = [
        "# XAMS Slow Control — test report",
        "",
        f"Generated {datetime.now().strftime('%d %B %Y, %H:%M')} by "
        "`tools/make_test_report.py`. A snapshot of one run on one machine; "
        "re-run it rather than reading an old one.",
        "",
    ]

    lines += table(
        ["", ""],
        [["Commit", f"`{git('rev-parse', '--short', 'HEAD')}` "
                    f"{git('log', '-1', '--format=%s')}"],
         ["Branch", git("rev-parse", "--abbrev-ref", "HEAD")],
         ["Working tree", "clean" if not git("status", "--porcelain")
                          else "**modified since this commit**"],
         ["Python", platform.python_version()],
         ["Platform", f"{platform.system()} {platform.release()} "
                      f"({platform.machine()})"],
         ["Host", attrs.get("hostname", "unknown")],
         ["Node present", "yes" if shutil.which("node") else
                          "no — the browser-behaviour tests were skipped"]])

    lines += ["## Result", ""]
    verdict = ("**All tests passed.**" if failures == 0 and errors == 0
               else f"**{failures + errors} test(s) did not pass.**")
    lines += [verdict, ""]
    lines += table(["", "Count"],
                   [["Tests", total], ["Passed", passed],
                    ["Failed", failures], ["Errors", errors],
                    ["Skipped", skipped],
                    ["Duration", f"{duration:.0f} s"]])

    lines += [
        "## What this suite does not cover", "",
        "**No hardware is involved in any of it.** The instrument drivers are "
        "tested against recorded replies from the real devices, so what is "
        "checked is the parsing, the sign convention, the status decoding and "
        "the refusals — not the cable, the board or the serial port. A green "
        "suite says the software does what it was asked to; it does not say "
        "the cDAQ is plugged in.",
        "",
        "This run was made on the machine named above. If that is not the "
        "lab PC, nothing here was exercised against the plant.",
        "",
    ]

    by_module: dict[str, list[dict]] = {}
    for case in cases:
        by_module.setdefault(case["module"], []).append(case)

    rows = []
    for module in sorted(by_module):
        group = by_module[module]
        bad = sum(1 for c in group if c["outcome"] in ("failed", "error"))
        skip = sum(1 for c in group if c["outcome"] == "skipped")
        if bad:
            result = f"**{bad} failed**"
        elif skip:
            result = f"ok ({skip} skipped)"
        else:
            result = "ok"
        rows.append([f"`{module}`", len(group),
                     f"{sum(c['time'] for c in group):.1f} s",
                     result, summary_of(module)])

    lines += [f"## Results by file ({len(by_module)} files)", ""]
    lines += table(["File", "Tests", "Time", "Result", "What it holds down"],
                   rows)

    bad = [c for c in cases if c["outcome"] in ("failed", "error")]
    if bad:
        lines += ["## Failures", ""]
        lines += table(["Test", "Why"],
                       [[f"`{c['classname']}::{c['name']}`",
                         c["reason"][:160] or "see the run output"]
                        for c in bad])

    skips = [c for c in cases if c["outcome"] == "skipped"]
    if skips:
        lines += ["## Skipped", ""]
        lines += table(["Test", "Reason"],
                       [[f"`{c['classname']}::{c['name']}`",
                         c["reason"][:160]] for c in skips])

    if coverage:
        files = coverage.get("files", {})
        totals = coverage.get("totals", {})
        lines += ["## Coverage", "",
                  "Statement coverage of `src/xams_sc`, from the same run. "
                  "A high number here means the lines ran, not that what they "
                  "did was checked.", ""]
        rows = []
        for name in sorted(files):
            s = files[name]["summary"]
            rows.append([f"`{name}`", s["num_statements"], s["missing_lines"],
                         f"{s['percent_covered']:.0f}%"])
        rows.append(["**total**", totals.get("num_statements", 0),
                     totals.get("missing_lines", 0),
                     f"**{totals.get('percent_covered', 0):.0f}%**"])
        lines += table(["Module", "Statements", "Not run", "Covered"], rows)

    slowest = sorted(cases, key=lambda c: -c["time"])[:10]
    lines += ["## Ten slowest tests", "",
              "Listed because a slow suite stops being run. Most of these "
              "wait on a timeout by design.", ""]
    lines += table(["Test", "Time"],
                   [[f"`{c['module']}::{c['name']}`", f"{c['time']:.1f} s"]
                    for c in slowest])

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- pdf

def to_pdf(md: Path, pdf: Path) -> bool:
    """Render through the dossier's own pipeline (notes/md2pdf.py).

    Same toolchain as `notes/render.sh`, and macOS-only for the same reason:
    Chrome renders and Ghostscript normalises, because without the gs pass
    the output has been seen to print blank after page 1.
    """
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(chrome).exists() or shutil.which("gs") is None:
        print("no Chrome or no Ghostscript; the PDF was not made "
              "(the Markdown is there)", file=sys.stderr)
        return False

    with tempfile.TemporaryDirectory() as tmp:
        html = Path(tmp) / "report.html"
        raw = Path(tmp) / "raw.pdf"
        subprocess.run([sys.executable, str(ROOT / "notes" / "md2pdf.py"),
                        str(md), str(html)], check=True)
        subprocess.run([chrome, "--headless", "--disable-gpu",
                        "--no-pdf-header-footer",
                        f"--print-to-pdf={raw}", f"file://{html}"],
                       check=True, capture_output=True)
        subprocess.run(["gs", "-sDEVICE=pdfwrite",
                        "-dCompatibilityLevel=1.4", "-dPDFSETTINGS=/prepress",
                        "-dEmbedAllFonts=true", "-dNOPAUSE", "-dBATCH",
                        "-dQUIET", f"-sOutputFile={pdf}", str(raw)],
                       check=True)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/make_test_report.py")
    parser.add_argument("--pdf", action="store_true",
                        help="also render test_report.pdf")
    parser.add_argument("--out", default="test_report.md")
    args = parser.parse_args(argv)

    try:
        import coverage  # noqa: F401
        with_coverage = True
    except ImportError:
        with_coverage = False
        print("coverage is not installed; the report will have no coverage "
              "section", file=sys.stderr)

    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "results.xml"
        cov_json = Path(tmp) / "coverage.json" if with_coverage else None

        print("running the suite...", file=sys.stderr)
        run_suite(junit, cov_json)
        if not junit.exists():
            print("pytest produced no results; nothing to report",
                  file=sys.stderr)
            return 1

        attrs, cases = read_results(junit)
        coverage_data = None
        if cov_json is not None and cov_json.exists():
            coverage_data = json.loads(cov_json.read_text(encoding="utf-8"))

    out = ROOT / args.out
    out.write_text(build(attrs, cases, coverage_data), encoding="utf-8")
    print(f"wrote {out}")

    if args.pdf:
        pdf = out.with_suffix(".pdf")
        if to_pdf(out, pdf):
            print(f"wrote {pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
