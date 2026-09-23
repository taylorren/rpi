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
``S(t)`` is highly autocorrelated - it is a smoothed average with a half-life measured in
days, so consecutive snapshots carry almost the same information. Treating 500 snapshots
as 500 independent observations would understate the uncertainty by well over an order of
magnitude. The effective sample size is roughly ``duration / correlation time``, which for
a 36-hour half-life means about one independent observation every two days, not every
fifteen minutes.

That distinction is the whole point of this tool: it is the difference between "we have
plenty of data" and "we have a handful of observations".

Reported
--------
* ``mean S``        - the recommended value for ``b``
* ``corr time``     - how long the mood stays correlated with itself
* ``n_eff``         - effective independent observations, not snapshot count
* ``SE`` / ``95% CI``- uncertainty of the mean, corrected for autocorrelation
* implied drift with ``b = 0`` and with the recommended ``b``
* a readiness verdict and, if not ready, how much longer is needed

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
from typing import Any, Dict, List, Optional, Sequence
try:
    import numpy as np
except Exception:
    np = None

from . import config as config_mod, paths, schema, storage

# The mean of S must be pinned this tightly before b can be trusted. Since the
# daily drift is about 0.2 * mean_S percent, a standard error of 0.05 works out
# to roughly +-0.01%/day, or about +-3.7%/year.
TARGET_SE = 0.05

# Correlation search bound, in snapshots. Comfortably past a 36h half-life while
# keeping the naive autocorrelation loop cheap.
MAX_LAG_SEARCH = 800


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
            config_version: int) -> Dict[str, Any]:
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
        "ready": se <= TARGET_SE,
        "days_needed": (days * (se / TARGET_SE) ** 2) if se > TARGET_SE else days,
        "config_version": config_version,
    }


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
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    conn = storage.connect(args.db)
    try:
        rows = storage.snapshots(conn, cfg.config_version, args.schema_version)
        values: List[float] = [float(row["s_value"]) for row in rows]
    finally:
        conn.close()

    if not values:
        print("no snapshots; run the pipeline first")
        return 1

    stats = analyse(values, cfg.snapshot_minutes, cfg.config_version)
    if stats.get("reason"):
        print("not enough data: {}".format(stats["reason"]))
        return 1

    mean = stats["mean"]
    current = drift_percent_per_day(mean, cfg)
    annual = ((1.0 + current / 100.0) ** 365 - 1.0) * 100.0

    print("samples        : {} snapshots over {:.1f} days".format(
        stats["n"], stats["days"]))
    print("mean S(t)      : {:+.4f}   <- the recommended b".format(mean))
    print("sd S(t)        : {:.4f}".format(stats["sd"]))
    print()
    print("autocorrelation: {:.0f} snapshots = {:.2f} days".format(
        stats["tau_c_snapshots"], stats["tau_c_days"]))
    print("  The mood stays correlated with itself for days, so consecutive")
    print("  snapshots are near-duplicates. Counting them as independent would")
    print("  understate the uncertainty badly.")
    print("effective n    : {:.1f} independent observations (not {})".format(
        stats["n_effective"], stats["n"]))
    print("standard error : {:.4f}   (95% CI {:+.4f} .. {:+.4f})".format(
        stats["se"], mean - stats["ci_half_width"], mean + stats["ci_half_width"]))
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

    if stats["ready"]:
        print("VERDICT: ready. Standard error {:.4f} <= target {:.2f}".format(
            stats["se"], TARGET_SE))
    else:
        print("VERDICT: NOT ready. Standard error {:.4f} > target {:.2f}".format(
            stats["se"], TARGET_SE))
        print("         roughly {:.0f} days of history needed (have {:.1f}).".format(
            stats["days_needed"], stats["days"]))
        print("         Uncertainty falls as 1/sqrt(time), so it takes 4x the data")
        print("         to halve the error.")
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
        for target in (0.05, 0.10, 0.15, 0.25, 0.40):
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
            print("  {:>10.2f}  {:>7.0f} d  {:>12.4f}%  {:>11.1f}%  {}".format(
                target, need, per_day, per_year, verdict))
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
