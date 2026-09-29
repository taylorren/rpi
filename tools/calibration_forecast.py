#!/usr/bin/env python3
"""Replay how the projected b-freeze date has moved as the series grew.

The projected date is not a countdown. Algebraically it is
``sd**2 * 2 * tau_c / (per_day * TARGET_SE**2)`` added to the first day of the
sample - the elapsed time cancels - so the date moves only when the sample's
spread or its correlation time moves, and it is re-fitted on every run. On a
young series those estimates move a lot, so the date slides by months while
``se`` barely falls: on the live series it went from July 2027 to April 2027 in
four days.

Replaying the pipeline's own history makes that visible instead of surprising,
and gives a way to tell whether a change to the estimator actually steadied it.
Each row is one prefix of the stored series, trimmed of its warm-up exactly the
way the live path trims it, so the numbers here are the ones the site published
at that point. A date that is still racing earlier as the sample grows means the
sample - not the world - is what is moving.

Usage::

    python tools/calibration_forecast.py
    python tools/calibration_forecast.py --step-days 0.5
    python tools/calibration_forecast.py --min-items 0     # no warm-up cut
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import calculator, calibrate  # noqa: E402
from rpi import config as config_mod, paths, schema, storage  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--schema-version", type=int, default=schema.SCHEMA_VERSION)
    parser.add_argument("--min-items", type=int, default=calibrate.WARMUP_MIN_ITEMS,
                        help="coverage floor for the warm-up cut (0 disables it)")
    parser.add_argument("--step-days", type=float, default=1.0,
                        help="how much history to add to each row")
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    conn = storage.connect(args.db)
    try:
        rows = storage.snapshots(conn, cfg.config_version, args.schema_version)
    finally:
        conn.close()

    if not rows:
        print("no snapshots; run the pipeline first")
        return 1

    per_day = 1440.0 / max(cfg.snapshot_minutes, 1)
    step = max(int(round(args.step_days * per_day)), 1)
    # The live series is always included, so the last row is the number the site
    # is showing right now even when the step does not land on it.
    ends = list(range(step, len(rows) + 1, step))
    if ends[-1] != len(rows):
        ends.append(len(rows))

    print("series   : {} snapshots, {} -> {}".format(
        len(rows), rows[0]["ts"], rows[-1]["ts"]))
    print("warm-up  : dropped while coverage is below {} stories in the horizon"
          .format(args.min_items))
    print("target SE: {}, so b is quotable to about +-{:.2f}%/day"
          .format(calibrate.TARGET_SE,
                  abs(calibrate.drift_percent_per_day(calibrate.TARGET_SE, cfg))))
    print()
    header = "{:>8} {:>7} {:>8} {:>8} {:>8} {:>7} {:>9}  {}".format(
        "run day", "snaps", "dropped", "sd", "tau (d)", "n_eff", "needed",
        "projected freeze")
    print(header)
    print("-" * len(header))

    for end in ends:
        window = rows[:end]
        stats = calibrate.fit(window, cfg.snapshot_minutes, cfg.config_version,
                              args.min_items)
        if stats.get("reason"):
            continue
        # ``now`` for that run is the end of the series as it stood then, which is
        # how ``rpi.export`` reads it too.
        now = calculator.parse_ts(window[-1]["ts"])
        if now is None:
            continue
        remaining = max(float(stats["days_needed"]) - float(stats["days"]), 0.0)
        expected = (now + timedelta(days=remaining)).date().isoformat()
        spread = float(stats.get("spread_days", 0.0))
        # A date from a sample too small to support one is still listed, marked,
        # because the point of this listing is to show the projection being
        # re-fitted - including while it is not yet worth reading.
        mark = "" if stats.get("quotable") else "  *"
        print("{:>8.1f} {:>7} {:>8} {:>8.4f} {:>8.2f} {:>7.1f} {:>8.0f}d  {}{}{}".format(
            end / per_day, stats["n"], stats["dropped_snapshots"], stats["sd"],
            stats["tau_c_days"], stats["n_effective"], stats["days_needed"],
            expected, "  +-{:.0f}d".format(spread) if spread else "", mark))

    print()
    print("* coverage never reached the floor, or the covered span is under")
    print("  {:.0f} days - too little to estimate from, so the date is a placeholder."
          .format(calibrate.MIN_SPAN_DAYS))
    print("The date is anchored to the first covered day and re-fitted on every")
    print("run: only sd and the correlation time move it. A row whose date jumps")
    print("while se barely falls is the estimate re-fitting, not progress.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
