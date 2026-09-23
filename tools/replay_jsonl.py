"""Replay the JSONL archive into a PostgreSQL database. See DESIGN.md §9.2, §12.

    python tools/replay_jsonl.py --since 2026-09-20                  # -> the VM
    python tools/replay_jsonl.py --since 2026-09-01 --until 2026-09-10
    python tools/replay_jsonl.py --data-dir data/imported --since 2026-01-01
    python tools/replay_jsonl.py --target postgres --since 2026-09-22  # local
    python tools/replay_jsonl.py --since 2026-09-20 --dry-run

The archive is the truth and every database is an index over it (§9.3). This
is the tool that makes that sentence true in practice. It is how:

  * a new Nikhef VM gets its history on the first day;
  * a gap on the VM is closed after an outage longer than the remote writer's
    backlog — the sinks log names the `--since` to use;
  * the LabVIEW history reaches the VM: `import_labview_csv.py` writes
    `data/imported/*.jsonl`, and this replays that directory. The importer's
    own `--to-postgres` cannot be used there, because it DELETEs before it
    inserts and the VM's writer role has no DELETE.

IT IS SAFE TO RUN TWICE. Rows go in with ON CONFLICT DO NOTHING against the
unique index on (t, channel, src), so a replay adds only what is missing and
reports how many that was. That is also why it REFUSES a database without the
index: there, a replay would double every row it touched.

It needs INSERT on `meas` and nothing else — COPY into a temporary table, then
one INSERT ... SELECT. So it runs as the VM's `xams_writer`, the same role the
sinks use, and no stronger credential ever has to sit on the lab PC.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xams_sc.model import Measurement  # noqa: E402
from xams_sc.sinks.__main__ import dsn_from_secrets  # noqa: E402
from xams_sc.sinks.pg_writer import HAS_UNIQUE_INDEX  # noqa: E402

# A temporary table, not a real one: it needs no privilege beyond TEMP (which
# PUBLIC has by default), and it is gone when the transaction ends even if
# this process is killed halfway.
CREATE_STAGE = """
CREATE TEMP TABLE replay_stage (
    t timestamptz, channel text, value double precision, raw double precision,
    unit text, quality text, src text
) ON COMMIT DROP
"""

COPY_STAGE = "COPY replay_stage (t, channel, value, raw, unit, quality, src) FROM STDIN"

MERGE_STAGE = """
INSERT INTO meas (t, channel, value, raw, unit, quality, src)
SELECT t, channel, value, raw, unit, quality, src FROM replay_stage
ON CONFLICT (t, channel, src) DO NOTHING
"""


def select_files(directory: Path, since: date, until: date | None) -> list[Path]:
    """The archive's day files in [since, until], oldest first.

    Files are named by UTC date (jsonl_writer). Anything else in the directory
    is ignored rather than guessed at.
    """
    out = []
    for path in directory.glob("*.jsonl"):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= since and (until is None or day <= until):
            out.append((day, path))
    return [p for _, p in sorted(out)]


def rows_from(path: Path, stats: dict):
    """The meas rows in one archive file.

    The first line of every file is a `meta` header (§9.3), and a line cut off
    by a power loss is expected at the end of a day that ended badly. Both are
    skipped and counted; neither stops the replay.
    """
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                if "meta" in d:
                    continue
                m = Measurement.from_payload(d)
            except Exception:
                stats["unparseable"] += 1
                continue
            stats["read"] += 1
            yield (m.t, m.channel, m.value, m.raw, m.unit, m.quality.value, m.src)


def replay_file(conn, path: Path, stats: dict) -> int:
    """Put one day into the database. Returns the rows that were NEW.

    One transaction per day: a day is either in or not, and a failure halfway
    through the archive leaves every earlier day committed.
    """
    with conn.cursor() as cur:
        cur.execute(CREATE_STAGE)
        with cur.copy(COPY_STAGE) as copy:
            for row in rows_from(path, stats):
                copy.write_row(row)
        cur.execute(MERGE_STAGE)
        added = cur.rowcount
    conn.commit()
    return added


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="replay_jsonl.py", description=__doc__.split("\n")[0])
    p.add_argument("--since", required=True, help="first UTC day, YYYY-MM-DD")
    p.add_argument("--until", help="last UTC day, YYYY-MM-DD (default: all)")
    p.add_argument("--data-dir", default=str(ROOT / "data" / "raw"))
    p.add_argument("--target", default="postgres_remote",
                   help="block of config/secrets.yaml to write to "
                        "(default postgres_remote, the Nikhef VM)")
    p.add_argument("--dry-run", action="store_true",
                   help="read and count; connect to nothing")
    args = p.parse_args(argv)

    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until) if args.until else None
    files = select_files(Path(args.data_dir), since, until)
    if not files:
        print(f"no archive files in {args.data_dir} from {since}", file=sys.stderr)
        return 2

    conn = None
    if not args.dry_run:
        dsn = dsn_from_secrets(args.target)
        if not dsn:
            print(f"FATAL: config/secrets.yaml has no `{args.target}` block "
                  "with a database", file=sys.stderr)
            return 2
        import psycopg
        conn = psycopg.connect(dsn, connect_timeout=10)
        with conn.cursor() as cur:
            cur.execute(HAS_UNIQUE_INDEX)
            if cur.fetchone() is None:
                print("FATAL: the unique index meas_unique_reading is missing "
                      "on the target, so a replay would duplicate rows. Apply "
                      "sql/schema.sql as the table owner first.", file=sys.stderr)
                conn.close()
                return 2
        conn.commit()

    print(f"target     {'(dry run)' if args.dry_run else args.target}")
    print(f"files      {len(files)}  ({files[0].stem} .. {files[-1].stem})\n")

    total_read = total_added = 0
    for path in files:
        stats = {"read": 0, "unparseable": 0}
        if conn is None:
            for _ in rows_from(path, stats):
                pass
            added = 0
        else:
            added = replay_file(conn, path, stats)
        total_read += stats["read"]
        total_added += added
        note = f"  ({stats['unparseable']} unparseable)" if stats["unparseable"] else ""
        print(f"  {path.stem}  {stats['read']:9,d} read  {added:9,d} new{note}")

    print(f"\n  total  {total_read:,} read, {total_added:,} new")
    if conn is not None:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
