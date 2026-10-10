#!/usr/bin/env python3
"""Measure the index at several impact weightings (p), and re-derive b and c.

Why
---
``weight_power`` is the one lever that decides whether a single day's big story
can move the level: each report is weighted by ``|signed| ** p`` on top of the
decay, so a story twice as consequential counts ``2 ** p`` times as much. It is
also the lever that changes the *units* of S - the mood's mean and spread both
grow with p - so ``b`` (the baseline) and ``c`` (the sensitivity) have to be
re-measured with it. Adopting a new p without re-measuring both leaves the level
mis-centred and mis-scaled.

What it reports, per p
----------------------
* ``mean S``      - the value ``b`` must be set to on these units
* ``sd S``        - the spread the sensitivity is judged against
* ``c = ln(1.05) / sd`` - the sensitivity that puts a one-sd mood about 5% either
  side of 100, which is the rule the live ``c`` (0.0243) was derived from
* ``largest/dy`` - the biggest story's share of one day's weight: the answer to
  "can an event move the line at all?"
* the published series' level span by window, and the median daily move, at the
  candidate ``b`` and ``c``

Nothing here writes to the database or the config. Adopting a column means
editing ``weight_power``, ``baseline_b`` and ``c`` in ``rpi.config.json`` and
bumping ``CONFIG_VERSION`` - a separate, deliberate step.

Usage::

    python tools/weight_power_ab.py
    python tools/weight_power_ab.py --powers 2 3 4
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import calculator, config as config_mod, paths, schema, storage  # noqa: E402
import scheme_ab  # noqa: E402  (same directory; reused so the arithmetic has one home)

# The amplitude target the live c was derived from: a one-sd mood is worth about
# 5% of level. Kept here as a named constant so a different target is a visible
# edit rather than a magic number.
LEVEL_PER_SD = 0.05


def candidate_c(sd: float) -> float:
    """The sensitivity that makes a one-sd mood worth ``LEVEL_PER_SD``."""
    if sd <= 0.0:
        return float("nan")
    return math.log1p(LEVEL_PER_SD) / sd


def rescaled(series: Sequence[calculator.Snapshot], cfg: Any,
             b: float, c: float) -> List[calculator.Snapshot]:
    """Re-level a series at a candidate ``b`` and ``c`` without touching storage."""
    return [calculator.Snapshot(
        ts=snap.ts,
        level=cfg.base_level * math.exp(c * (snap.s_value - b)),
        s_value=snap.s_value,
        item_count=snap.item_count,
    ) for snap in series]


def largest_share(items: Sequence[calculator.ScoredItem], at: datetime,
                  cfg: Any) -> float:
    """Biggest story's share of the weight live at ``at``."""
    total, weighted = scheme_ab.weights_of(items, at, cfg)
    if total <= 0.0 or not weighted:
        return float("nan")
    return weighted[0][0] / total


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--powers", type=float, nargs="+", default=[2.0, 3.0])
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    conn = storage.connect(args.db)
    try:
        rows = storage.analysed_rows(conn, schema.SCHEMA_VERSION,
                                     per_source=cfg.items_per_source)
        items = calculator.build_items(rows, cfg)
    finally:
        conn.close()

    if not items:
        print("no analyses at schema_version={}".format(schema.SCHEMA_VERSION))
        return 1

    now = datetime.now(timezone.utc)
    start = items[0].ts
    print("corpus   : {} item(s)  {} .. {}".format(
        len(items), start.date(), items[-1].ts.date()))
    print("current  : p={} b={:+.4f} c={} (config v{})".format(
        cfg.weight_power, cfg.baseline_b, cfg.c, cfg.config_version))
    print()

    header = ("{:<4} {:>10} {:>9} {:>9} {:>10} {:>11} {:>11} {:>11}".format(
        "p", "mean S", "sd S", "corr d", "largest/dy", "c@5%/sd",
        "1w span", "1m span"))
    print(header)
    print("-" * len(header))

    results: List[Dict[str, Any]] = []
    for p in args.powers:
        cfg_p = dataclasses.replace(cfg, weight_power=p)
        series = calculator.build_series(items, cfg_p, start, now)
        cal = scheme_ab.calibration(series, cfg_p)
        b = cal["b"]
        c = candidate_c(cal["sd"])
        levels = rescaled(series, cfg_p, b, c)

        def span(days: float) -> float:
            cutoff = now - timedelta(days=days)
            vals = [s.level for s in levels if s.ts >= cutoff]
            return ((max(vals) - min(vals)) / cfg_p.base_level * 100.0
                    if vals else float("nan"))

        share = largest_share(items, now, cfg_p)
        move = scheme_ab.median_daily_move(levels)
        results.append({"p": p, "b": b, "c": c, "sd": cal["sd"],
                        "span1w": span(7.0), "span1m": span(30.0),
                        "move": move})
        print("{:<4} {:>10.4f} {:>9.4f} {:>9.2f} {:>10.2%} {:>11.5f} "
              "{:>10.3f}% {:>10.3f}%".format(
                  p, b, cal["sd"], cal.get("tau_c_days", float("nan")), share,
                  c, span(7.0), span(30.0)))

    print()
    print("corr d      = measured correlation time of S, in days (calibrate's own)")
    print("largest/dy  = biggest story's share of the weight live now")
    print("c@5%/sd     = ln(1.05)/sd(S), the rule the live c was derived from")
    print("span        = min..max of the level over the window, % of base")
    print()
    print("median daily move of the level:")
    for r in results:
        print("  p={:<4} {:>6.3f}%".format(r["p"], r["move"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
