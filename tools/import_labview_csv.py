"""Import LabVIEW slow-control logs into the XAMS archive. See DESIGN.md §9.6.

    python tools/import_labview_csv.py --days 7
    python tools/import_labview_csv.py --days 7 --to-postgres
    python tools/import_labview_csv.py --from 2026-09-01 --to 2026-09-10 --dry-run

Imported records are tagged `src="labview"` so they stay distinguishable from
data this system took itself.

------------------------------------------------------------------------------
THE HEADER FILES CANNOT BE TRUSTED. READ THIS BEFORE CHANGING THE LAYOUT.
------------------------------------------------------------------------------

§9.6 warned that the LabVIEW front panel has a "Re-save headers" button, so the
header file is mutable. The reality on this machine, measured 17 September 2026,
is worse than that:

  * 304 header files contain **90 distinct layouts**, from 1 to 2269 columns.
    Several are plainly corrupt — a header appended to instead of overwritten.
  * The data files themselves come in **9 distinct column counts** (7, 39, 45,
    46, 47, 54, 55, 60, 62), and their date ranges INTERLEAVE rather than
    forming clean eras. A 46-column file and a 54-column file can share a week.
  * The current header (`SC_LOG_HEADERS/16-9-2026`) describes **62 columns**
    while the data it supposedly describes has **47**. It under-counts the
    temperatures by one AND lists sixteen high-voltage columns that are not in
    the data at all.

So the header is not merely stale: for the current data it is structurally
wrong. Importing by header would shift columns and produce values that look
entirely plausible and are attached to the wrong sensors.

**This importer therefore ignores the header files completely.** It recognises
layouts by column count against a table established by comparing the logged
values with live readings from the same instruments, and it REFUSES any file
whose column count it does not recognise. It never guesses.

------------------------------------------------------------------------------
FORMAT NOTES
------------------------------------------------------------------------------

  * Tab separated, no header row inside the data file.
  * **Decimal comma** — "19,649" is 19.649. European locale.
  * Timestamps "D-M-YYYY HH:MM:SS" in **local time**, converted to UTC here.
  * LabVIEW logs **scaled** values, not raw. `raw` is therefore null for every
    imported record, which is a real loss: history imported this way cannot be
    rescaled if a multiplier turns out to be wrong (§9.4).
  * Sample interval is 2 s. One day is roughly 43000 rows and 15 MB.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xams_sc.config import load  # noqa: E402
from xams_sc.model import Measurement, Quality, iso  # noqa: E402

LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
SRC = "labview"

# Platinum RTD validity, same bound the cdaq driver uses. An imported value
# outside it is an open circuit and is stored as quality=error with no value —
# importing +1326 C as a temperature would carry LabVIEW's own blind spot into
# the new archive, which is the opposite of the point.
RTD_VALID_C = (-200.0, 850.0)

# ---------------------------------------------------------------------------
# Column layouts, keyed by field count. None = a column we deliberately drop.
#
# Established 17 September 2026 by reading the last line of 17-9-2026 and
# matching each value against what our own cDAQ read minutes later. Every
# mapping below was confirmed that way, not taken from a header file.
# ---------------------------------------------------------------------------

_T_9216_1 = ["tt301", "tt302", "tt103", "tt104", "ttamb", "tt303", "tt304", None]
_T_9216_2 = [None] * 8                      # module entirely unconnected
_T_9226 = ["tt201", "tt202", "tt203", "tt204", "tt205", "tt206", "tt207", None]
_V_9207 = ["p101", "p102", "p103", "p104", None, "pmain", None, "fm101"]
_I_9207 = [None] * 8                        # current inputs, unused (§7.1)
# The Lake Shore sensor inputs were renamed ls_sensor_a/b -> tt401/tt402 on
# 17 Sep 2026, bringing them into the plant tag scheme. Historic data must be
# imported under the CURRENT names or the history is orphaned under a channel
# that no longer exists - which is exactly why section 3 says names are
# permanent. Any already-imported rows were renamed in place at the same time.
_LAKESHORE = ["tt401", "tt402", "ls_heater_1", None]

LAYOUTS = {
    # Time + 24 temperatures + 8 voltages + 8 currents + 4 Lake Shore + 2 time
    47: ["_time"] + _T_9216_1 + _T_9216_2 + _T_9226 + _V_9207 + _I_9207
        + _LAKESHORE + [None, None],
}


def parse_number(text: str) -> float | None:
    """Decimal comma to float. Empty or unparseable becomes None."""
    text = text.strip()
    if not text:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def parse_timestamp(text: str) -> datetime | None:
    """'17-9-2026 08:46:16' in local time, returned as aware UTC."""
    try:
        naive = datetime.strptime(text.strip(), "%d-%m-%Y %H:%M:%S")
    except ValueError:
        return None
    # fold=0 picks the first occurrence of an ambiguous local time at the DST
    # fallback. One hour a year is imported an hour early rather than rejected.
    return naive.replace(tzinfo=LOCAL_TZ, fold=0).astimezone(ZoneInfo("UTC"))


def file_date(name: str) -> datetime | None:
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", name)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


class Rejected(Exception):
    """A file this importer will not guess at."""


def rows_from(path: Path, channels: dict, stats: dict):
    """Yield Measurements from one daily log file.

    Raises Rejected if the column count is not a layout we have confirmed.
    """
    with io.open(path, encoding="latin-1", errors="replace") as fh:
        first = fh.readline().rstrip("\n")
        if not first:
            return
        width = len(first.split("\t"))
        layout = LAYOUTS.get(width)
        if layout is None:
            raise Rejected(
                f"{width} columns — not a confirmed layout "
                f"(known: {sorted(LAYOUTS)}). Refusing to guess; see the note "
                f"at the top of this file."
            )
        fh.seek(0)

        for lineno, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != width:
                # A short line is a truncated write, usually the last line of a
                # file that was open when the process died. Skip it and count.
                stats["ragged"] += 1
                continue

            t = parse_timestamp(fields[0])
            if t is None:
                stats["bad_timestamp"] += 1
                continue

            for idx, name in enumerate(layout):
                if name in (None, "_time"):
                    continue
                ch = channels.get(name)
                if ch is None:
                    continue
                value = parse_number(fields[idx])
                if value is None:
                    stats["unparseable"] += 1
                    continue

                quality = Quality.OK
                if ch.kind == "rtd" and not (
                    RTD_VALID_C[0] <= value <= RTD_VALID_C[1]
                ):
                    quality = Quality.ERROR
                    stats["open_circuit"] += 1
                    value = None

                stats["rows"] += 1
                yield Measurement(t=t, channel=name, value=value, unit=ch.unit,
                                  raw=None, quality=quality)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--source", default=r"C:\Users\localadmin\Desktop\SC_DATA\SC_LOG")
    p.add_argument("--days", type=int, default=7,
                   help="import the most recent N days (default 7)")
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD, overrides --days")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    p.add_argument("--out", default="data/imported",
                   help="JSONL output directory")
    p.add_argument("--to-postgres", action="store_true",
                   help="also insert into the meas table with src='labview'")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be imported; write nothing")
    args = p.parse_args(argv)

    source = Path(args.source)
    if not source.is_dir():
        print(f"FATAL: {source} is not a directory", file=sys.stderr)
        return 2

    config = load()
    channels = config.channels

    dated = sorted((d, f) for f in os.listdir(source) if (d := file_date(f)))
    if not dated:
        print(f"FATAL: no dated log files in {source}", file=sys.stderr)
        return 2

    if args.date_from:
        lo = datetime.strptime(args.date_from, "%Y-%m-%d")
        hi = (datetime.strptime(args.date_to, "%Y-%m-%d") if args.date_to
              else dated[-1][0])
    else:
        hi = dated[-1][0]
        lo = hi - timedelta(days=args.days - 1)

    selected = [(d, f) for d, f in dated if lo <= d <= hi]
    print(f"source     {source}")
    print(f"range      {lo:%Y-%m-%d} .. {hi:%Y-%m-%d}")
    print(f"files      {len(selected)} of {len(dated)} available")
    if args.dry_run:
        print("mode       DRY RUN — nothing will be written")
    print()

    out_dir = Path(args.out)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    pg = None
    if args.to_postgres and not args.dry_run:
        pg = _connect_postgres(config)
        if pg is None:
            return 2
        # Re-running an import must not double the rows. Only labview-tagged
        # rows in this date range are removed; nothing this system recorded
        # itself is ever touched.
        lo_utc = lo.replace(tzinfo=LOCAL_TZ)
        hi_utc = (hi + timedelta(days=1)).replace(tzinfo=LOCAL_TZ)
        with pg.cursor() as cur:
            cur.execute("DELETE FROM meas WHERE src = %s AND t >= %s AND t < %s",
                        (SRC, lo_utc, hi_utc))
            if cur.rowcount > 0:
                print(f"  removed {cur.rowcount:,} previously imported rows "
                      f"in this range\n")

    totals = {"rows": 0, "open_circuit": 0, "ragged": 0,
              "bad_timestamp": 0, "unparseable": 0}
    rejected = []

    for d, fname in selected:
        stats = {k: 0 for k in totals}
        path = source / fname
        try:
            batch = list(rows_from(path, channels, stats))
        except Rejected as exc:
            rejected.append((fname, str(exc)))
            print(f"  {fname:14s} REFUSED — {exc}")
            continue

        if not args.dry_run:
            _write_jsonl(out_dir / f"{d:%Y-%m-%d}.jsonl", batch, config)
            if pg is not None:
                _write_postgres(pg, batch)

        for k in totals:
            totals[k] += stats[k]
        note = f"  ({stats['open_circuit']} open-circuit)" if stats["open_circuit"] else ""
        print(f"  {fname:14s} {stats['rows']:7d} readings{note}")

    print()
    print(f"  total readings     {totals['rows']:,}")
    print(f"  open-circuit       {totals['open_circuit']:,} stored as quality=error")
    if totals["ragged"]:
        print(f"  short lines        {totals['ragged']:,} skipped (truncated writes)")
    if totals["bad_timestamp"]:
        print(f"  bad timestamps     {totals['bad_timestamp']:,} skipped")
    if totals["unparseable"]:
        print(f"  unparseable values {totals['unparseable']:,} skipped")
    if rejected:
        print(f"\n  {len(rejected)} FILE(S) REFUSED — unrecognised column count:")
        for f, why in rejected:
            print(f"    {f}: {why}")
        print("  These are not imported. Confirm their layout against live")
        print("  readings before adding it to LAYOUTS; do not guess.")

    if pg is not None:
        pg.commit()
        pg.close()
    return 0


def _write_jsonl(path: Path, batch, config) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "t": iso(batch[0].t) if batch else None,
            "meta": {"config": config.config_hash, "src": SRC,
                     "note": "imported from LabVIEW SC_LOG"},
        }, separators=(",", ":")) + "\n")
        for m in batch:
            d = m.to_payload()
            d["src"] = SRC
            fh.write(json.dumps(d, separators=(",", ":")) + "\n")


def _connect_postgres(config):
    import yaml
    from xams_sc.config import CONFIG_DIR
    secrets_path = CONFIG_DIR / "secrets.yaml"
    if not secrets_path.exists():
        print("FATAL: config/secrets.yaml not found", file=sys.stderr)
        return None
    pg = (yaml.safe_load(secrets_path.read_text(encoding="utf-8")) or {}).get("postgres")
    if not pg:
        print("FATAL: no postgres block in secrets.yaml", file=sys.stderr)
        return None
    import psycopg
    return psycopg.connect(
        host=pg.get("host", "127.0.0.1"), port=pg.get("port", 5432),
        dbname=pg["database"], user=pg.get("user", "xams"),
        password=pg.get("password", ""))


def _write_postgres(conn, batch) -> None:
    """COPY, not executemany.

    A day is roughly a million readings. executemany round-trips per row and
    takes minutes; COPY streams and takes seconds.
    """
    with conn.cursor() as cur:
        with cur.copy(
            "COPY meas (t, channel, value, raw, unit, quality, src) FROM STDIN"
        ) as copy:
            for m in batch:
                copy.write_row((m.t, m.channel, m.value, None,
                                m.unit, m.quality.value, SRC))


if __name__ == "__main__":
    raise SystemExit(main())
