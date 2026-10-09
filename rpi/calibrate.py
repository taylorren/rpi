#!/usr/bin/env python3
"""Estimate the baseline ``b``, and say honestly whether there is enough data yet.

What ``b`` is for
-----------------
News is structurally negative, so the decay-weighted mood ``S(t)`` sits persistently
below zero. With ``b = 0`` the index therefore drifts down at a roughly constant rate
that has nothing to do with the world. ``b`` is meant to be set to the long-run mean of
``S(t)``, so the index responds to news being *unusual* rather than to news being *news*.

Why this is not just "average the snapshots"
--------------------------------------------
``S(t)`` is highly autocorrelated - consecutive snapshots carry almost the same
information. Treating 500 snapshots as 500 independent observations would understate the
uncertainty by two orders of magnitude. The effective sample size is roughly
``duration / correlation time``.

The correlation time is **measured, not assumed**. It is tempting to derive it from
``tau_hours`` - a 36-hour half-life suggests a correlation time near 52 hours, so one
independent observation every two days - but the live series decorrelates far faster than
that, because the weighted mean is dominated by which stories happen to be in the window
rather than by the decay kernel alone. Measured on the live series the correlation time is
51-62 snapshots (0.5-0.65 days), so an independent observation arrives roughly every 15
hours. Deriving it from ``tau_hours`` instead would have understated the uncertainty by a
factor of three and pulled the projected freeze date months early.

That distinction is the whole point of this tool: it is the difference between "we have
plenty of data" and "we have a handful of observations".

Which days are in the sample
----------------------------
The opening days of the series are excluded on purpose. Coverage builds up: on this corpus
the first five days carried 1-15 scored stories a day, against 100-200 a day once the
fetcher and the scoring service were both running. With a handful of stories in the decay
horizon, ``S(t)`` is one or two headlines rather than an average, and those readings swing
to +/-4 - which then dominate the sample's variance for weeks afterwards, dragging ``sd``,
``se``, the drift estimate and the projected freeze date with them. On the live series the
first five days accounted for most of the sample spread (``sd`` 0.64 against 0.22 once they
are dropped).

So the sample begins at the first snapshot whose horizon holds at least
``WARMUP_MIN_ITEMS`` stories, and the dropped prefix is *reported* rather than silently
ignored. The cut is a prefix, never a scattered filter: the series has to stay contiguous
or the autocorrelation lags stop meaning anything. A trimmed sample must also span at least
``MIN_SPAN_DAYS`` before readiness can be claimed at all, because the eight-snapshot floor
``analyse`` keeps is only two hours of history.

Reported
--------
* ``mean S``        - the recommended value for ``b``
* ``corr time``     - how long the mood stays correlated with itself
* ``n_eff``         - effective independent observations, not snapshot count
* ``SE`` / ``95% CI``- uncertainty of the mean, corrected for autocorrelation
* implied drift with ``b = 0`` and with the recommended ``b``
* a readiness verdict and, if not ready, how much longer is needed
* what the target SE is worth as a drift budget, and what loosening it buys
* whether the estimate has settled, and how far the projected date could be out

Usage::

    python -m rpi.calibrate
    python -m rpi.calibrate --apply      # write b into rpi.config.json

Dependencies
------------
Stdlib only, with one optional extra. When NumPy is importable the autocorrelation is
computed with an FFT; otherwise the naive ``O(n * lag)`` loop is used. The two are
numerically identical on every series checked - AR(1), white noise, a linear trend, a
constant, the short-series guard, and the live 615-snapshot series - so NumPy changes
only the running time, never the reported numbers. Installing it is not required, and
this is the only module in the project that reads it.

Measured at a 15-minute snapshot cadence, worst case for the naive path (its search
running the full ``MAX_LAG_SEARCH``):

    history     snapshots    naive      NumPy
    6.4 days          615    3.5 ms     2.0 ms
    1 quarter       8,760    0.9 s      2.1 ms
    1 year         35,040    3.0 s      7.7 ms

Neither column is slow enough for anyone to notice, on a command that is only ever run
by hand - this module is deliberately absent from the hourly pipeline, so the choice of
path cannot affect production.

One limitation worth knowing, since it is not about speed: ``MAX_LAG_SEARCH`` also caps
the *reported* correlation time at 800 snapshots, which is 8.3 days at this cadence. A
genuinely longer correlation time would be reported as 8.3 days, and because ``n_eff``
falls as the correlation time rises, the uncertainty would come out too small. The
current value is 61 snapshots (0.64 days), well inside the cap.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
try:
    import numpy as np
except Exception:
    np = None

from . import config as config_mod, paths, schema, storage

# The mean of S must be pinned this tightly before b can be trusted. This is a
# drift budget, not a statistical convention: with k = 0.02 an error of ``d`` in
# the mean mood drifts the index by about 0.2 * d percent per day, or 73 * d
# percent per year. 0.10 therefore leaves at most +-0.02%/day, about +-7.6%/year,
# against the ~30%/year the correction removes on the live series: the "worth
# doing" band this module's own verdict table already accepts, and roughly a
# fifth of a typical day's move (median 1-day move ~0.1%), so it stays invisible
# on the chart. It was 0.05, which bought +-3.7%/year for about four times the
# wait - and since S(t) is not stationary (it drifted ~-0.2/week on the live
# series), the extra scatter a tighter target chases is not the dominant
# uncertainty anyway. tools/window_dominance.py splits that out.
TARGET_SE = 0.10

# Correlation search bound, in snapshots. Comfortably past a 36h half-life while
# keeping the naive autocorrelation loop cheap.
MAX_LAG_SEARCH = 800

# Coverage floor for the sample, in stories present in the decay horizon. The
# opening days measure nothing: with a few stories in the window S(t) is one or
# two headlines, and those readings then dominate the sample's variance for weeks
# afterwards - which is what moved the projected freeze date from July 2027 to
# April 2027 in four days on the live series. Full coverage on this corpus is
# 100-200 stories a day, so 50 excludes the warm-up without touching a day that
# means anything. Never raise this to make a projection look better; it is a
# statement about coverage, not about the answer.
WARMUP_MIN_ITEMS = 50

# Below this many effective observations the estimate of ``se`` is itself
# uncertain by tens of percent, so the projected date - which goes as ``se**2`` -
# is an indication rather than an appointment. The date is still reported; it is
# flagged, and hedged with its own spread, instead of being quoted to the day.
SETTLED_OBSERVATIONS = 20

# Least real time a trimmed sample may span and still be quotable. ``analyse``
# already refuses fewer than eight snapshots, but eight snapshots is two hours:
# right at the coverage step a trim can leave a handful of rows whose spread is
# tiny only because almost no time has passed. Two days is the smallest span on
# which the correlation time is even measurable, so below it the projection is
# still reported but readiness is withheld.
MIN_SPAN_DAYS = 2.0


def correlation_time(values: Sequence[float]) -> float:
    """Lag at which autocorrelation first falls below 1/e, in snapshots.

    Uses a fast FFT‑based autocorrelation when NumPy is available; otherwise
    falls back to the original naive O(n·lag) implementation.
    """
    n = len(values)
    if n < 4:
        return 0.5

    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    if variance <= 0:
        return 0.5

    # Fast path: NumPy FFT autocorrelation
    if np is not None:
        arr = np.asarray(values, dtype=float)
        arr -= arr.mean()
        # Zero‑pad to at least 2*n for clean circular convolution
        size = 2 * n
        fft = np.fft.rfft(arr, n=size)
        ac = np.fft.irfft(fft * np.conjugate(fft))[:n]
        # Normalise to correlation coefficient (lag 0 = 1)
        ac = ac / (variance * np.arange(n, 0, -1))
        threshold = math.exp(-1.0)
        # Search up to the configured limit and half the series length
        max_lag = min(MAX_LAG_SEARCH, n // 2)
        for lag in range(1, max_lag):
            if ac[lag] < threshold:
                return float(lag)
        return float(max_lag)

    # Naive fallback (original algorithm)
    limit = min(MAX_LAG_SEARCH, n // 2)
    threshold = math.exp(-1.0)
    for lag in range(1, limit):
        covariance = sum((values[i] - mean) * (values[i + lag] - mean)
                         for i in range(n - lag)) / (n - lag)
        if covariance / variance < threshold:
            return float(lag)
    return float(limit)


def analyse(values: Sequence[float], snapshot_minutes: int,
            config_version: int,
            target_se: float = TARGET_SE) -> Dict[str, Any]:
    n = len(values)
    if n < 8:
        return {"n": n, "ready": False, "reason": "not enough snapshots yet"}

    mean = statistics.fmean(values)
    sd = statistics.pstdev(values)
    tau_c = correlation_time(values)

    # For a process with correlation time tau_c, the variance of the sample mean
    # is inflated by roughly 2*tau_c. This is the correction that matters.
    n_effective = max(n / (2.0 * tau_c), 1.0)
    se = sd / math.sqrt(n_effective)
    half_width = 1.96 * se

    span_minutes = n * snapshot_minutes
    days = span_minutes / 1440.0

    # The span at which ``se`` reaches ``TARGET_SE``. ``se`` falls as
    # 1/sqrt(time), so the span scales as ``(se / target) ** 2`` - the "4x the
    # data to halve the error" rule, taken once. Note that the current ``days``
    # cancels out of that product: ``days_needed`` moves only when ``sd`` or the
    # correlation time moves, which is why the projected date is an estimate that
    # gets re-fitted rather than a countdown that ticks down.
    days_needed = (days * (se / target_se) ** 2) if se > target_se else days

    # How far the projected date could be out on this sample's own evidence.
    # ``se`` is proportional to the sample's ``sd``, whose relative error is about
    # ``1/sqrt(2 * (n_eff - 1))``, and the date goes as ``se**2``, so that error
    # doubles. Reported so a rough projection reads as a range.
    spread_days = 0.0
    if se > target_se and n_effective > 2.0:
        spread_days = days_needed * 2.0 / math.sqrt(2.0 * (n_effective - 1.0))

    return {
        "n": n,
        "days": days,
        "mean": mean,
        "sd": sd,
        "tau_c_snapshots": tau_c,
        "tau_c_days": tau_c * snapshot_minutes / 1440.0,
        "n_effective": n_effective,
        "se": se,
        "ci_half_width": half_width,
        "ready": se <= target_se,
        "days_needed": days_needed,
        "spread_days": spread_days,
        "settled": n_effective >= SETTLED_OBSERVATIONS,
        "config_version": config_version,
    }


def covered_start(rows: Sequence[Any],
                  min_items: int = WARMUP_MIN_ITEMS) -> Optional[int]:
    """Index of the first snapshot whose coverage is representative, or None.

    Walks the warm-up off the front of the series rather than filtering rows out
    of the middle of it, so the sample stays contiguous and the autocorrelation
    lags keep their meaning. ``None`` means nothing reached the floor - the whole
    series is warm-up - which is a coverage problem to report rather than a
    reason to print nothing: a caller still gets its series, and flags the
    estimate instead of losing it.
    """
    if min_items <= 0:
        return 0
    for index, row in enumerate(rows):
        try:
            count = int(row["item_count"])
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if count >= min_items:
            return index
    return None


def fit(rows: Sequence[Any], snapshot_minutes: int, config_version: int,
        min_items: int = WARMUP_MIN_ITEMS,
        target_se: float = TARGET_SE) -> Dict[str, Any]:
    """Analyse the covered part of a snapshot series.

    The single entry point for both this tool and the export. Both must agree on
    which days are in the sample and what ``b`` would be, because the site quotes
    a date next to a figure a reader can re-derive here.

    Adds the warm-up bookkeeping to the statistics: ``dropped_snapshots``,
    ``dropped_days``, the ``min_items`` floor that produced them, and
    ``coverage_met``.

    Readiness needs a sample that can support the claim, so it is withheld unless
    coverage reached the floor *and* the retained span is at least
    ``MIN_SPAN_DAYS``. ``--apply`` and the site's freeze announcement both key off
    ``ready``, and a trim can otherwise leave a handful of rows whose spread looks
    wonderful because barely any time has passed. The projection itself is still
    reported in that case - a rough date beats no date - and ``quotable`` says
    whether it can be taken at face value.
    """
    first = covered_start(rows, min_items)
    dropped = first if first is not None else 0
    values = [float(row["s_value"]) for row in rows[dropped:]]
    stats = analyse(values, snapshot_minutes, config_version, target_se)
    stats["dropped_snapshots"] = dropped
    stats["dropped_days"] = dropped * snapshot_minutes / 1440.0
    stats["min_items"] = min_items
    stats["coverage_met"] = first is not None
    # Carried in the stats so callers (the export, the replay tools) quote the
    # budget the verdict was actually judged against, not the module default.
    stats["target_se"] = target_se

    quotable = (bool(stats["coverage_met"])
                and float(stats.get("days", 0.0)) >= MIN_SPAN_DAYS)
    stats["quotable"] = quotable
    if not quotable:
        stats["ready"] = False
        stats["settled"] = False
    return stats


def drift_percent_per_day(mean_s: float, cfg: config_mod.RpiConfig) -> float:
    """Daily drift implied by a given mean mood, as a percentage."""
    return (math.exp(cfg.k * mean_s / 10.0) - 1.0) * 100.0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Estimate the baseline b and judge whether the data supports it.")
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--schema-version", type=int, default=schema.SCHEMA_VERSION)
    parser.add_argument("--apply", action="store_true",
                        help="write the estimate into rpi.config.json")
    parser.add_argument("--min-items", type=int, default=WARMUP_MIN_ITEMS,
                        help="coverage floor, in stories per snapshot, for the "
                             "warm-up cut (0 keeps every snapshot)")
    parser.add_argument("--target-se", type=float, default=TARGET_SE,
                        help="drift budget, in S units: the standard error b must "
                             "reach before freezing is worth it (default %(default)s)")
    parser.add_argument("--since", type=str, default=None, metavar="YYYY-MM-DD",
                        help="restrict the sample to snapshots from this day on - use it "
                             "when an input changed (a source added or dropped), so b is "
                             "measured on one homogeneous era instead of a blend of two")
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

    if args.since:
        # Snapshot stamps are fixed-width ISO-8601 UTC, so a date prefix compares
        # correctly as a string and no parsing is needed. Filtered here rather
        # than in the query so storage.snapshots keeps its single signature.
        kept = [row for row in rows if str(row["ts"]) >= args.since]
        if not kept:
            print("no snapshots at or after {} (earliest is {})".format(
                args.since, rows[0]["ts"]))
            return 1
        rows = kept

    stats = fit(rows, cfg.snapshot_minutes, cfg.config_version, args.min_items,
                args.target_se)
    if stats.get("reason"):
        print("not enough data: {}".format(stats["reason"]))
        return 1

    mean = stats["mean"]
    current = drift_percent_per_day(mean, cfg)
    annual = ((1.0 + current / 100.0) ** 365 - 1.0) * 100.0

    print("samples        : {} snapshots over {:.1f} days".format(
        stats["n"], stats["days"]))
    if args.since:
        print("sample era     : from {} only (--since)".format(args.since))
    if stats["dropped_snapshots"]:
        print("warm-up cut    : first {} snapshots ({:.1f} days) dropped - coverage".format(
            stats["dropped_snapshots"], stats["dropped_days"]))
        print("                 was below {} stories in the decay horizon".format(
            stats["min_items"]))
    print("mean S(t)      : {:+.4f}   <- the recommended b".format(mean))
    print("sd S(t)        : {:.4f}".format(stats["sd"]))
    print()
    print("autocorrelation: {:.0f} snapshots = {:.2f} days".format(
        stats["tau_c_snapshots"], stats["tau_c_days"]))
    print("  Consecutive snapshots carry almost the same information, so counting")
    print("  them as independent observations would understate the uncertainty")
    print("  badly. This correlation time is measured from the series, not derived")
    print("  from tau: the window's contents turn over faster than the decay does.")
    print("effective n    : {:.1f} independent observations (not {})".format(
        stats["n_effective"], stats["n"]))
    print("standard error : {:.4f}   (95% CI {:+.4f} .. {:+.4f})".format(
        stats["se"], mean - stats["ci_half_width"], mean + stats["ci_half_width"]))
    budget_year = abs(((1.0 + drift_percent_per_day(stats["target_se"], cfg) / 100.0)
                       ** 365 - 1.0) * 100.0)
    print("drift budget   : SE <= {:.2f}, i.e. residual drift up to {:.1f}%/year".format(
        stats["target_se"], budget_year))
    print()
    print("drift if b = 0         : {:+.4f}%/day  ({:+.1f}%/year)".format(current, annual))
    print("drift after calibration: {:+.4f}%/day  (by construction, if b is exact)"
          .format(drift_percent_per_day(mean - mean, cfg)))
    print()
    print("worst-case residual drift from the uncertainty in b:")
    print("  {:+.4f}%/day, {:+.1f}%/year".format(
        abs(drift_percent_per_day(stats["se"], cfg)),
        abs(((1.0 + drift_percent_per_day(stats["se"], cfg) / 100.0) ** 365 - 1.0) * 100.0)))
    print()

    # Once b is frozen the useful question changes: not "how tightly is it
    # pinned" but "is the frozen value still the right one". That is a recurring
    # check rather than a one-off, because S(t) has no obligation to hold still -
    # on this corpus it drifted about -0.2/week, which is what re-dated the
    # projection for weeks while the estimate itself barely moved. These three
    # numbers are what a weekly reading of this output is for.
    if cfg.calibrated:
        latest_s = float(rows[-1]["s_value"])
        flow_day = drift_percent_per_day(latest_s - cfg.baseline_b, cfg)
        flow_year = ((1.0 + flow_day / 100.0) ** 365 - 1.0) * 100.0
        print("frozen b       : {:+.4f}   (rpi.config.json)".format(cfg.baseline_b))
        print("estimate moved : {:+.4f}   (current mean S(t) minus the frozen b)".format(
            mean - cfg.baseline_b))
        print("drift the frozen b leaves at the current flow, S(t) = {:+.4f}:".format(
            latest_s))
        print("  {:+.4f}%/day, {:+.1f}%/year".format(flow_day, flow_year))
        print("  Re-run --apply once that is no longer clearly smaller than the")
        print("  {:+.1f}%/year it removes.".format(abs(annual)))
        print()

    if stats["ready"]:
        print("VERDICT: ready. Standard error {:.4f} <= target {:.2f}".format(
            stats["se"], stats["target_se"]))
    else:
        print("VERDICT: NOT ready. Standard error {:.4f} > target {:.2f}".format(
            stats["se"], stats["target_se"]))
        print("         roughly {:.0f} days of covered history needed (have {:.1f}).".format(
            stats["days_needed"], stats["days"]))
        print("         Uncertainty falls as 1/sqrt(time), so it takes 4x the data")
        print("         to halve the error.")
        if stats["spread_days"]:
            print("         Read the projection as a range: the sample's own uncertainty")
            print("         puts the date about +-{:.0f} days around it.".format(
                stats["spread_days"]))
        print()

        # The point of the table: a quick calibration is not a cheap calibration.
        # If the residual uncertainty left in b produces more drift than the bias
        # being corrected, the exercise has made things worse, not better.
        # Both sides must be annualised before comparing - mixing daily and annual
        # units makes every option look bad.
        bias_per_year = abs(annual)
        print("Tightening b costs time, and a loose estimate may be worse than none.")
        print("The bias being corrected is {:.1f}%/year:".format(bias_per_year))
        print()
        print("  {:>10}  {:>9}  {:>13}  {:>12}  {}".format(
            "target SE", "days", "residual/day", "residual/yr", "verdict"))
        # The chosen budget is always shown, so the trade-off the verdict is
        # judged on is visible rather than implied by a hardcoded ladder.
        ladder = sorted({0.05, 0.10, 0.15, 0.25, 0.40, float(stats["target_se"])})
        for target in ladder:
            n_eff = (stats["sd"] / target) ** 2
            need = 2.0 * stats["tau_c_days"] * n_eff
            per_day = abs(drift_percent_per_day(target, cfg))
            per_year = abs(((1.0 + per_day / 100.0) ** 365 - 1.0) * 100.0)
            if per_year < bias_per_year * 0.5:
                verdict = "worth doing"
            elif per_year <= bias_per_year * 1.5:
                verdict = "no real gain"
            else:
                verdict = "worse than nothing"
            chosen = ("  <- target"
                      if abs(target - float(stats["target_se"])) < 1e-9 else "")
            print("  {:>10.2f}  {:>7.0f} d  {:>12.4f}%  {:>11.1f}%  {}{}".format(
                target, need, per_day, per_year, verdict, chosen))
        print()
        print("So a calibration is only worth applying once the residual drift it")
        print("leaves behind is clearly smaller than the drift it removes.")

    if args.apply:
        if not stats["ready"]:
            print()
            print("REFUSING to apply: the estimate is not yet stable enough.")
            print("Use --apply only once the verdict is 'ready'.")
            return 1
        cfg.baseline_b = round(mean, 4)
        cfg.calibrated = True
        written = config_mod.save(cfg)
        print()
        print("wrote b = {:+.4f} (calibrated=True) to {}".format(cfg.baseline_b, written))
        print("Re-run the pipeline to rebuild the index with the new baseline:")
        print("  python -m rpi.calculator && python -m rpi.export")
        print("NOTE: this config change makes existing snapshots stale; the next")
        print("      calculation run rewrites them all.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
