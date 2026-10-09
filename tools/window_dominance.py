#!/usr/bin/env python3
"""Explain a rising correlation time by looking inside the decay window.

``python -m rpi.calibrate`` measures ``tau_c`` from S(t), so when the projected
freeze date slides the question is always *why S(t) became more correlated with
itself*. Two very different causes look identical in that one number:

1. **A long-lived dominant story.** One story whose event time broke days ago
   still carries most of the decay weight, so S(t) sits still while that story
   decays. The corpus really is more correlated for as long as it stays in the
   window, and waiting only helps once it ages out.
2. **A trend read as correlation.** The autocorrelation subtracts a single mean
   over the whole sample, so a steady drift inside that sample reads as
   correlation at every lag. The number then moves because the sample is short
   and drifting - the estimator walking - not because any story persisted.

They are separated here by two views, both read-only against the database:

* Section A re-measures ``tau_c`` on trailing windows (each with its own local
  mean removed) and prints the daily mean of S(t) next to it. A trailing tau
  that stays low while the full-sample one rises is trend, not persistence.
* Section B shows who holds the window on each day: the top clusters by decay
  weight, the participation ratio (effective number of stories sharing the
  weight - 1/sum of squared shares), and the weight-weighted mean age. One
  dominant story shows up as a top-1 share of tens of percent across several
  days; a healthy window spreads the weight over dozens of stories.

Each analysed row is already one story: ``storage.analysed_rows`` returns at
most one item per cluster, so an item's share of the decay weight *is* its
story's share - no extra grouping is needed here.

Usage::

    python tools/window_dominance.py
    python tools/window_dominance.py --window-days 4 --detail-days 7
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import calculator, calibrate  # noqa: E402
from rpi import config as config_mod, paths, schema, storage  # noqa: E402

_LN2 = math.log(2.0)

# Titles are single-line summaries but nothing stops a feed from shipping a
# wall of text; truncate so the table keeps its shape.
TITLE_WIDTH = 76


def window_view(items: Sequence[calculator.ScoredItem],
                at: datetime,
                tau_hours: float) -> Tuple[float, List[Tuple[float, float, Any]],
                                            float, float]:
    """Decay-weight picture of the window at ``at``.

    Mirrors ``calculator.decayed_mean`` exactly - same horizon, same
    half-life formula, same rejection of future-stamped items - but returns
    the shares instead of the ratio, so callers can see *who* holds the
    weight rather than only what it averages to.

    Returns ``(total_weight, entries, mean_age_days, effective_stories)``.
    Entries are ``(weight, age_hours, item)``, largest first.
    """
    horizon_hours = tau_hours * calculator.MAX_AGE_FACTOR
    entries: List[Tuple[float, float, calculator.ScoredItem]] = []
    total = 0.0
    weighted_age = 0.0
    sum_sq = 0.0
    for item in items:
        age_hours = (at - item.ts).total_seconds() / 3600.0
        if age_hours < 0.0 or age_hours > horizon_hours:
            continue
        weight = math.exp(-_LN2 * age_hours / tau_hours)
        entries.append((weight, age_hours, item))
        total += weight
        weighted_age += weight * age_hours
        sum_sq += weight * weight
    if total <= 0.0:
        return 0.0, [], 0.0, 0.0
    entries.sort(key=lambda entry: entry[0], reverse=True)
    mean_age_days = weighted_age / total / 24.0
    effective = (total * total) / sum_sq
    return total, entries, mean_age_days, effective


def _day_ends(first: datetime, last: datetime) -> List[datetime]:
    """Exclusive UTC end-of-day stamps from ``first``'s day through ``last``'s."""
    ends: List[datetime] = []
    day = first.astimezone(timezone.utc).date()
    last_day = last.astimezone(timezone.utc).date()
    while day <= last_day:
        ends.append(datetime(day.year, day.month, day.day,
                             tzinfo=timezone.utc) + timedelta(days=1))
        day += timedelta(days=1)
    return ends


def trailing_tau_rows(series: Sequence[Tuple[datetime, float]],
                      window_days: float,
                      snapshot_minutes: int) -> List[Dict[str, Any]]:
    """Trailing correlation time and daily mean S(t), one row per full window.

    A window is only listed once it is completely covered by history, so the
    earliest rows compare like with like. Correlation time is returned in
    snapshots by ``calibrate.correlation_time``; converting with the snapshot
    cadence is the same arithmetic ``analyse`` does.
    """
    if not series:
        return []
    per_day = 1440.0 / max(snapshot_minutes, 1)
    full = int(window_days * per_day * 0.95)
    rows: List[Dict[str, Any]] = []
    for end in _day_ends(series[0][0], series[-1][0]):
        start = end - timedelta(days=window_days)
        if start < series[0][0]:
            continue  # history shorter than the window; not comparable
        values = [value for moment, value in series if start <= moment < end]
        if len(values) < full:
            continue
        tau_snapshots = calibrate.correlation_time(values)
        # A search that ran out of lag is a floor, not a measurement; mark it
        # rather than quoting the cap as if the series had decorrelated there.
        limit = min(calibrate.MAX_LAG_SEARCH, len(values) // 2)
        rows.append({
            "day": (end - timedelta(days=1)).date(),
            "start": start,
            "end": end,
            "tau_days": tau_snapshots * snapshot_minutes / 1440.0,
            "capped": tau_snapshots >= limit,
            "mean_s": statistics.fmean(values),
            "n": len(values),
        })
    return rows


def polarity_mix(items: Sequence[calculator.ScoredItem], start: datetime,
                 end: datetime) -> Tuple[float, float]:
    """Share of stories that are negative and positive in ``[start, end)``.

    Pairs with the drifting mean S(t): if the mean is falling *and* the
    negative share is rising while the average magnitude is flat, the corpus
    itself turned darker. If the mean moves while the mix does not, the move
    is coming from a few stories rather than from the flow.
    """
    negative = positive = total = 0
    for item in items:
        if start <= item.ts < end:
            total += 1
            if item.sentiment == "negative":
                negative += 1
            elif item.sentiment == "positive":
                positive += 1
    if not total:
        return 0.0, 0.0
    return negative / total, positive / total


def level_stats(values: Sequence[float], snapshot_minutes: int) -> Dict[str, Any]:
    """tau, effective n, SE and the span needed to reach TARGET_SE.

    The same arithmetic ``calibrate.analyse`` does, kept here so the raw and
    detrended versions of one series can be compared on identical terms.
    """
    n = len(values)
    if n < 8:
        return {}
    sd = statistics.pstdev(values)
    tau_snapshots = calibrate.correlation_time(values)
    n_eff = max(n / (2.0 * tau_snapshots), 1.0)
    se = sd / math.sqrt(n_eff)
    days = n * snapshot_minutes / 1440.0
    needed = days * (se / calibrate.TARGET_SE) ** 2 if se > calibrate.TARGET_SE else days
    return {
        "sd": sd,
        "tau_days": tau_snapshots * snapshot_minutes / 1440.0,
        "n_eff": n_eff,
        "se": se,
        "days": days,
        "needed": needed,
    }


def detrended(values: Sequence[float],
              snapshot_minutes: int) -> Tuple[List[float], float]:
    """``values`` with a least-squares straight line removed, plus its slope/day.

    A frozen ``b`` can only be the mean of a series that has no trend in it, so
    this is the split that matters: whatever spread and correlation survive here
    is what a fixed baseline has to absorb. The line is what it cannot - it is
    the baseline itself walking away.
    """
    n = len(values)
    if n < 2:
        return list(values), 0.0
    mx = (n - 1) / 2.0
    my = statistics.fmean(values)
    den = sum((i - mx) ** 2 for i in range(n))
    slope = (sum((i - mx) * (v - my) for i, v in enumerate(values)) / den
             if den else 0.0)
    residuals = [v - (my + slope * (i - mx)) for i, v in enumerate(values)]
    return residuals, slope * 1440.0 / max(snapshot_minutes, 1)


def brief(text: Optional[str]) -> str:
    """Single-line, fixed-width-enough title for the detail block."""
    clean = " ".join((text or "?").split())
    if len(clean) > TITLE_WIDTH:
        return clean[:TITLE_WIDTH - 1] + "…"
    return clean


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--schema-version", type=int, default=schema.SCHEMA_VERSION)
    parser.add_argument("--min-items", type=int, default=calibrate.WARMUP_MIN_ITEMS,
                        help="coverage floor for the warm-up cut (0 disables it)")
    parser.add_argument("--window-days", type=float, default=5.0,
                        help="length of the trailing window used to re-measure tau")
    parser.add_argument("--detail-days", type=int, default=5,
                        help="how many recent days get the top-stories detail block")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")  # titles may be any encoding
    except (AttributeError, ValueError):  # pragma: no cover - exotic streams
        pass

    cfg = config_mod.load()
    conn = storage.connect(args.db)
    try:
        rows = storage.snapshots(conn, cfg.config_version, args.schema_version)
        analysed = storage.analysed_rows(conn, args.schema_version)
    finally:
        conn.close()

    if not rows or not analysed:
        print("no snapshots or analyses; run the pipeline first")
        return 1

    stats = calibrate.fit(rows, cfg.snapshot_minutes, cfg.config_version,
                          args.min_items)
    if stats.get("reason"):
        print("not enough data: {}".format(stats["reason"]))
        return 1

    items = calculator.build_items(analysed, cfg)
    # One analysed row per story, so this map is all the detail the shares need.
    meta = {row["id"]: row for row in analysed}

    covered = rows[int(stats["dropped_snapshots"]):]
    series: List[Tuple[datetime, float]] = []
    for row in covered:
        moment = calculator.parse_ts(row["ts"])
        if moment is not None:
            series.append((moment, float(row["s_value"])))
    series.sort(key=lambda pair: pair[0])

    print("series   : {} snapshots ({} covered of {}), {} stories".format(
        len(series), len(covered), len(rows), len(items)))
    print("sample   : tau_c {:.2f} d, sd {:.4f}, n_eff {:.1f} - the number "
          "this tool exists to explain".format(
              float(stats["tau_c_days"]), float(stats["sd"]),
              float(stats["n_effective"])))
    print()

    # --- Section A ---------------------------------------------------------
    print("A. is the recent series really less correlated?")
    print("   tau re-measured on each trailing {:.0f}-day window (local mean"
          " removed; * = lag search hit its cap), with the polarity mix of the"
          " stories published in that window".format(args.window_days))
    header = "{:>10}  {:>8}  {:>9}  {:>5}  {:>6}  {:>6}".format(
        "window to", "tau (d)", "mean S", "n", "%neg", "%pos")
    print("   " + header)
    print("   " + "-" * len(header))
    tau_rows = trailing_tau_rows(series, args.window_days, cfg.snapshot_minutes)
    for row in tau_rows:
        negative, positive = polarity_mix(items, row["start"], row["end"])
        print("   {:>10}  {:>8}  {:+9.4f}  {:>5}  {:>5.0%}  {:>5.0%}{}".format(
            row["day"].isoformat(),
            "{:.2f}{}".format(row["tau_days"], "*" if row["capped"] else ""),
            row["mean_s"], row["n"], negative, positive,
            "" if not row["capped"] else "  <- floor, not a measurement"))
    if not tau_rows:
        print("   (no full window yet - need {:.0f} days of covered history)"
              .format(args.window_days))
    print()

    # The table above answers "did tau move?". This answers the question that
    # decides whether waiting helps: how much of the full-sample tau and spread
    # is a straight line - which no fixed b can ever absorb - rather than the
    # scatter a frozen baseline is supposed to sit in the middle of.
    covered_values = [value for _moment, value in series]
    raw = level_stats(covered_values, cfg.snapshot_minutes)
    residuals, slope_per_day = detrended(covered_values, cfg.snapshot_minutes)
    flat = level_stats(residuals, cfg.snapshot_minutes)
    if raw and flat:
        print("   whole covered sample, and the same series with a straight line")
        print("   removed (the line is what a fixed b cannot absorb):")
        print("     raw        tau {:>5.2f}d  sd {:.4f}  se {:.4f}  -> {:>4.0f}d to target".format(
            raw["tau_days"], raw["sd"], raw["se"], raw["needed"]))
        print("     detrended  tau {:>5.2f}d  sd {:.4f}  se {:.4f}  -> {:>4.0f}d to target{}".format(
            flat["tau_days"], flat["sd"], flat["se"], flat["needed"],
            "   (already at target)" if flat["se"] <= calibrate.TARGET_SE else ""))
        print("     trend      {:+.4f}/day = {:+.3f}/week in S units".format(
            slope_per_day, slope_per_day * 7.0))
        print()

    # --- Section B ---------------------------------------------------------
    print("B. who holds the decay window? (share of total decay weight)")
    header = "{:>10}  {:>7}  {:>7}  {:>9}  {:>6}".format(
        "day", "stories", "eff.n", "mean age", "top-1")
    print("   " + header)
    print("   " + "-" * len(header))
    probes: List[Tuple[datetime, Dict[str, Any]]] = []
    last_moment = series[-1][0]
    for end in _day_ends(series[0][0], last_moment):
        at = end - timedelta(days=1) + timedelta(hours=12)
        if at > last_moment:
            at = last_moment  # mid-day run: do not project into the future
        if at < series[0][0]:
            continue
        total, entries, mean_age, effective = window_view(items, at, cfg.tau_hours)
        if total <= 0.0:
            continue
        view = {
            "at": at,
            "total": total,
            "entries": entries,
            "stories": len(entries),
            "eff_n": effective,
            "mean_age": mean_age,
            "top1": entries[0][0] / total,
        }
        probes.append((at, view))
        print("   {:>10}  {:>7}  {:>7.1f}  {:>8.1f}d  {:>6.1%}".format(
            at.date().isoformat(), view["stories"], view["eff_n"],
            mean_age, view["top1"]))
    print()

    detail = probes[-max(args.detail_days, 1):]
    if detail:
        print("   top 3 stories in the window on each recent day")
        for _at, view in detail:
            print("   {}".format(view["at"].date().isoformat()))
            for weight, age_hours, item in view["entries"][:3]:
                row = meta.get(item.item_id) or {}
                members = int(calculator.row_get(row, "cluster_member_count") or 1)
                print("     {:>5.1%}  age {:>4.1f}d  {:>3} member(s)  {}".format(
                    weight / view["total"], age_hours / 24.0, members,
                    brief(calculator.row_get(row, "title"))))
        print()

    # --- Section C ---------------------------------------------------------
    print("C. what moved")
    if tau_rows:
        print("   trailing tau : {:.2f} d -> {:.2f} d over {} full windows".format(
            tau_rows[0]["tau_days"], tau_rows[-1]["tau_days"], len(tau_rows)))
    if len(tau_rows) >= 2:
        print("   daily mean S : {:+.4f} -> {:+.4f} over the same windows".format(
            tau_rows[0]["mean_s"], tau_rows[-1]["mean_s"]))
    if probes:
        recent = probes[-min(7, len(probes)):]
        worst = max(recent, key=lambda pair: pair[1]["top1"])
        row = meta.get(worst[1]["entries"][0][2].item_id) or {}
        print("   top-1 share  : {:.1%} on {} (latest {:.1%}), worst of the"
              " last {} days".format(
                  worst[1]["top1"], worst[0].date().isoformat(),
                  probes[-1][1]["top1"], len(recent)))
        print("                held by: {}".format(
            brief(calculator.row_get(row, "title"))))
        print("   eff.n        : {:.1f} stories effectively share the weight"
              " (of {} present)".format(
                  probes[-1][1]["eff_n"], probes[-1][1]["stories"]))
    print()
    print("reading: a trailing tau that climbed with the full-sample one means")
    print("the corpus genuinely decorrelated more slowly - cause 1 above; a flat")
    print("trailing tau under a rising full-sample tau means drift is being read")
    print("as correlation (cause 2). A top-1 share in the tens of percent for")
    print("several consecutive days is the one-story signature.")
    print("A falling mean S with a rising %neg is the third case: the flow itself")
    print("turned darker, so the long-run mean b is moving while it is measured.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

