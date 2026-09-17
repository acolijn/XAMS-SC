"""Compare this system's readings against the imported LabVIEW record.

    python tools/compare_to_labview.py
    python tools/compare_to_labview.py --window 15 --tolerance 2.0

This is the validation half of milestone 4 (§15): "scaled values agree with the
LabVIEW record for the same sensors within expected tolerance."

HOW THE COMPARISON IS MADE, AND WHAT IT CANNOT SHOW.

The two systems cannot run at once — every device admits one process — so there
is no overlapping period to compare directly. Instead this takes the mean of
LabVIEW's last `window` minutes and the mean of our first `window` minutes and
compares them across the changeover gap.

That is sound for channels whose physics moves in minutes (temperatures,
steady-state pressures) and weak for anything that moves in seconds. The gap
itself is reported so the reader can judge.

**What agreement here does and does not prove.** It confirms that our DAQmx
configuration, excitation currents, RTD types and scaling reproduce what
LabVIEW produced from the same sensors. It does NOT confirm that a tag is
attached to the sensor its name claims: an error inherited from LabVIEW would
appear as perfect agreement. That is milestone 3's empirical check, and it is
separate (§16).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xams_sc.config import CONFIG_DIR, load  # noqa: E402

QUERY = """
WITH handover AS (
    SELECT (SELECT max(t) FROM meas WHERE src = 'labview') AS lv_end,
           (SELECT min(t) FROM meas WHERE src = 'xams')    AS xa_start
),
lv AS (
    SELECT channel, avg(value) AS v, count(*) AS n
    FROM meas, handover
    WHERE src = 'labview' AND quality = 'ok'
      AND t > handover.lv_end - make_interval(mins => %(window)s)
    GROUP BY channel
),
xa AS (
    SELECT channel, avg(value) AS v, count(*) AS n
    FROM meas, handover
    WHERE src = 'xams' AND quality = 'ok'
      AND t < handover.xa_start + make_interval(mins => %(window)s)
    GROUP BY channel
)
SELECT lv.channel, lv.v, lv.n, xa.v, xa.n,
       (SELECT EXTRACT(EPOCH FROM (xa_start - lv_end)) FROM handover)
FROM lv JOIN xa USING (channel)
ORDER BY lv.channel
"""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--window", type=int, default=15,
                   help="minutes either side of the changeover (default 15)")
    p.add_argument("--tolerance", type=float, default=1.0,
                   help="percent difference treated as agreement (default 1.0)")
    p.add_argument("--abs-tolerance", type=float, default=0.5,
                   help="absolute difference always treated as agreement, in "
                        "the channel's own units (default 0.5)")
    args = p.parse_args(argv)

    config = load()
    secrets_path = CONFIG_DIR / "secrets.yaml"
    if not secrets_path.exists():
        print("FATAL: config/secrets.yaml not found", file=sys.stderr)
        return 2
    pg = (yaml.safe_load(secrets_path.read_text(encoding="utf-8")) or {}).get("postgres")

    import psycopg
    conn = psycopg.connect(
        host=pg.get("host", "127.0.0.1"), port=pg.get("port", 5432),
        dbname=pg["database"], user=pg.get("user", "xams"),
        password=pg.get("password", ""))

    with conn.cursor() as cur:
        cur.execute(QUERY, {"window": args.window})
        rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No channel appears in both sources. Has the history been imported?")
        return 1

    gap = rows[0][5]
    print(f"window        {args.window} min either side of the changeover")
    print(f"changeover    {gap/60:.1f} min gap between the last LabVIEW reading "
          f"and the first of ours")
    print(f"agreement     within {args.tolerance}% or {args.abs_tolerance} "
          f"absolute\n")

    print(f"  {'channel':16s} {'unit':7s} {'LabVIEW':>11s} {'XAMS':>11s} "
          f"{'diff':>9s} {'diff %':>8s}")
    print("  " + "-" * 68)

    agree, disagree = [], []
    for channel, lv_v, lv_n, xa_v, xa_n, _ in rows:
        ch = config.channels.get(channel)
        unit = ch.unit if ch else "?"
        diff = float(xa_v) - float(lv_v)
        pct = (100.0 * diff / float(lv_v)) if abs(float(lv_v)) > 1e-9 else float("nan")
        ok = abs(diff) <= args.abs_tolerance or abs(pct) <= args.tolerance
        (agree if ok else disagree).append(channel)
        flag = "" if ok else "   <-- CHECK"
        pct_s = f"{pct:8.2f}" if pct == pct else "       -"
        print(f"  {channel:16s} {unit:7s} {float(lv_v):11.3f} {float(xa_v):11.3f} "
              f"{diff:9.3f} {pct_s}{flag}")

    print()
    print(f"  {len(agree)} of {len(rows)} channels agree")
    if disagree:
        print(f"  {len(disagree)} outside tolerance: {', '.join(disagree)}")
        print()
        print("  A difference here is not automatically an error. Check whether")
        print("  the channel actually moves on the timescale of the changeover")
        print("  gap before concluding the scaling is wrong.")
    return 0 if not disagree else 1


if __name__ == "__main__":
    raise SystemExit(main())
