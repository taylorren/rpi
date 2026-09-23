#!/usr/bin/env python3
"""RPI calculation: turning scored news into an index.

The index
---------
Each item contributes a signed impact:

.. math::

    s_i = \\varepsilon_i \\cdot m_i \\cdot w_{scope(i)}

where :math:`\\varepsilon` is polarity (+1 / 0 / -1), :math:`m_i` is the
``expected_score`` of the impact question (0-10) and :math:`w` is the scope
weight from the config.

Those contributions are combined into a decay-weighted mean - a *level*, not a
flow - where older news fades with half-life ``tau_hours``:

.. math::

    S(t) = \\frac{\\sum_i s_i 2^{-(t-t_i)/\\tau}}{\\sum_i 2^{-(t-t_i)/\\tau}}

The index then integrates the *deviation from the baseline*, using the exact
integrator for a piecewise-constant input:

.. math::

    RPI_t = RPI_{t-1} \\cdot \\exp\\left(k \\cdot \\frac{\\Delta t}{1\\,day}
            \\cdot \\frac{S(t) - b}{10}\\right)

Three properties follow, and each is deliberate:

1. **The step is scaled by the timestep.** Applying a *level* term once per
   snapshot without this would make the index depend on the cron interval -
   the same news would produce a different chart at 15-minute versus hourly
   sampling, and changing cadence would silently rewrite history. Scaling by
   :math:`\\Delta t` makes the trajectory invariant to sampling rate.
2. **Silence causes no drift.** With no items in the decay window the weighted
   mean is 0/0, so :math:`S(t)` falls back to ``b`` and the exponent is zero.
   The index only moves when there is news.
3. **Sustained sentiment moves the index; single headlines do not.** Because
   the level is integrated, one large story ramps the index for as long as
   ``tau`` keeps it in the window, rather than causing a spike. This is
   correct index behaviour, not a bug.

Neutral items are *included* with :math:`\\varepsilon = 0`: they dilute
:math:`S(t)` toward zero, which is the right reading - a flood of unremarkable
news is evidence the world is unremarkable.

Everything here is a pure function of the stored analyses plus the config, so
the whole history can be recomputed in seconds after retuning ``k``, ``tau`` or
``b``.
"""

from __future__ import annotations

import argparse
import bisect
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config as config_mod
from . import paths, schema, storage
from .config import RpiConfig

POLARITY: Dict[str, int] = {"positive": 1, "neutral": 0, "negative": -1}

_LN2 = math.log(2.0)

# Truncation horizon, in half-lives. At 9 half-lives a contribution is down to
# 2^-9 = 0.2%, small enough to drop. Expressed in half-lives rather than in tau,
# because that is the natural unit for trading cost against accuracy here.
MAX_AGE_FACTOR = 9.0


@dataclass(frozen=True)
class ScoredItem:
    """One analysed news item, reduced to what the index needs."""
    item_id: str
    ts: datetime
    sentiment: str
    scope: str
    impact: float
    signed: float


@dataclass(frozen=True)
class Snapshot:
    ts: datetime
    level: float
    s_value: float
    item_count: int


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an RFC 3339 timestamp from the store, tolerating a trailing Z."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def signed_impact(sentiment: Optional[str], impact_expected: Optional[float],
                  scope: Optional[str], cfg: RpiConfig) -> float:
    """Signed, scope-weighted contribution of one item."""
    if impact_expected is None:
        return 0.0
    return float(POLARITY.get((sentiment or "").lower(), 0)) * float(impact_expected) * cfg.scope_weight(scope)


def row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read a column that may be absent; sqlite3.Row raises on missing keys."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def build_items(rows: Iterable[Any], cfg: RpiConfig) -> List[ScoredItem]:
    """Convert analysed rows into scored items, oldest first.

    Event time prefers the cluster's ``first_published``, so a story enters the
    time series when it broke rather than when the slowest outlet covered it.
    Falls back to the item's own ``published``, then ``fetched_at``.
    """
    items: List[ScoredItem] = []
    for row in rows:
        ts = (parse_ts(row_get(row, "cluster_first_published"))
              or parse_ts(row_get(row, "published"))
              or parse_ts(row_get(row, "fetched_at")))
        if ts is None:
            continue
        sentiment = (row["sentiment"] or "").lower()
        scope = row["scope"] or ""
        impact = row["impact_expected"]
        items.append(ScoredItem(
            item_id=row["id"],
            ts=ts,
            sentiment=sentiment,
            scope=scope,
            impact=float(impact) if impact is not None else 0.0,
            signed=signed_impact(sentiment, impact, scope, cfg),
        ))
    items.sort(key=lambda item: item.ts)
    return items


# --------------------------------------------------------------------------- #
# Core maths
# --------------------------------------------------------------------------- #

def decayed_mean(items: Sequence[ScoredItem], times: Sequence[datetime],
                 at: datetime, tau_hours: float,
                 fallback: float) -> float:
    """Decay-weighted mean signed impact at ``at``.

    Returns ``fallback`` (the baseline) when nothing is in range, so silence
    produces no drift.
    """
    horizon = timedelta(hours=tau_hours * MAX_AGE_FACTOR)
    lo = bisect.bisect_left(times, at - horizon)
    hi = bisect.bisect_right(times, at)

    numerator = 0.0
    denominator = 0.0
    for item in items[lo:hi]:
        age_hours = (at - item.ts).total_seconds() / 3600.0
        if age_hours < 0:
            continue
        # True half-life: a story `tau_hours` old contributes half as much as a
        # fresh one. Plain exp(-age/tau) instead halves at tau*ln2, so a 36h
        # setting would behave like a 25h half-life - which is what this code
        # used to do, contradicting the documented meaning of tau.
        weight = math.exp(-_LN2 * age_hours / tau_hours)
        numerator += item.signed * weight
        denominator += weight

    if denominator <= 0.0:
        return fallback
    return numerator / denominator


def _floor_to(moment: datetime, step: timedelta) -> datetime:
    """Align a timestamp down to the snapshot grid."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    seconds = int((moment - epoch).total_seconds())
    return epoch + timedelta(seconds=(seconds // int(step.total_seconds())) * int(step.total_seconds()))


def build_series(items: Sequence[ScoredItem], cfg: RpiConfig, start: datetime,
                 end: datetime) -> List[Snapshot]:
    """Build the index series over ``[start, end]`` on the snapshot grid."""
    if not items:
        return []

    step = timedelta(minutes=max(cfg.snapshot_minutes, 1))
    times = [item.ts for item in items]
    baseline = cfg.baseline_b

    moment = _floor_to(start, step)
    level = cfg.base_level
    previous: Optional[datetime] = None
    series: List[Snapshot] = []

    while moment <= end:
        s_value = decayed_mean(items, times, moment, cfg.tau_hours, baseline)

        if previous is not None:
            dt_days = (moment - previous).total_seconds() / 86400.0
            level *= math.exp(cfg.k * dt_days * (s_value - baseline) / 10.0)

        horizon = timedelta(hours=cfg.tau_hours * MAX_AGE_FACTOR)
        low = bisect.bisect_left(times, moment - horizon)
        high = bisect.bisect_right(times, moment)

        series.append(Snapshot(
            ts=moment,
            level=level,
            s_value=s_value,
            item_count=max(high - low, 0),
        ))
        previous = moment
        moment += step

    return series


def recalculate(conn: Any, cfg: RpiConfig, schema_version: int,
                now: Optional[datetime] = None,
                persist: bool = True) -> List[Snapshot]:
    """Recompute the whole series from stored analyses and optionally persist it.

    Snapshots are a pure function of the analyses, so replacing them wholesale
    is both correct and idempotent.
    """
    now = now or datetime.now(timezone.utc)
    rows = storage.analysed_rows(conn, schema_version)
    items = build_items(rows, cfg)
    if not items:
        return []

    start = items[0].ts
    series = build_series(items, cfg, start, now)

    if persist:
        storage.clear_snapshots(conn, cfg.config_version, schema_version)
        for snap in series:
            storage.save_snapshot(
                conn,
                ts=snap.ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                config_version=cfg.config_version,
                schema_version=schema_version,
                level=snap.level,
                s_value=snap.s_value,
                item_count=snap.item_count,
            )
        conn.commit()

    return series


def summarise(items: Sequence[ScoredItem], series: Sequence[Snapshot],
              cfg: RpiConfig,
              now: Optional[datetime] = None) -> Dict[str, Any]:
    """Headline numbers for the UI."""
    now = now or datetime.now(timezone.utc)
    if not series:
        return {"level": cfg.base_level, "change": 0.0, "change_pct": 0.0,
                "s_value": cfg.baseline_b, "baseline": cfg.baseline_b,
                "volume_24h": 0, "volume_total": 0,
                "breadth_positive": 0.0, "breadth_negative": 0.0,
                "first_ts": None, "last_ts": None}

    latest = series[-1]
    # Compare against the snapshot one day earlier, so "change" reads as the
    # day's move rather than the last tick's.
    day_ago = latest.ts - timedelta(days=1)
    reference = series[0]
    for snap in series:
        if snap.ts <= day_ago:
            reference = snap
        else:
            break

    change = latest.level - reference.level
    change_pct = (change / reference.level * 100.0) if reference.level else 0.0

    recent = [item for item in items
              if now - item.ts <= timedelta(hours=24)]
    positives = sum(1 for item in recent if item.signed > 0)
    negatives = sum(1 for item in recent if item.signed < 0)
    counted = positives + negatives

    return {
        "level": latest.level,
        "change": change,
        "change_pct": change_pct,
        "s_value": latest.s_value,
        "baseline": cfg.baseline_b,
        "volume_24h": len(recent),
        "volume_total": len(items),
        "breadth_positive": (positives / counted) if counted else 0.0,
        "breadth_negative": (negatives / counted) if counted else 0.0,
        "first_ts": series[0].ts.isoformat().replace("+00:00", "Z"),
        "last_ts": latest.ts.isoformat().replace("+00:00", "Z"),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recompute RPI snapshots from stored analyses.")
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--schema-version", type=int, default=schema.SCHEMA_VERSION)
    parser.add_argument("--no-persist", action="store_true",
                        help="compute and print without writing snapshots")
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    conn = storage.connect(args.db)
    try:
        rows = storage.analysed_rows(conn, args.schema_version)
        items = build_items(rows, cfg)
        if not items:
            print("no analyses at schema_version={}; run rpi.analyse first".format(
                args.schema_version))
            return 1

        now = datetime.now(timezone.utc)
        series = recalculate(conn, cfg, args.schema_version, now=now,
                             persist=not args.no_persist)
        stats = summarise(items, series, cfg, now=now)
    finally:
        conn.close()

    print("config_version={} schema_version={} calibrated={}".format(
        cfg.config_version, args.schema_version, cfg.calibrated))
    print("k={} tau={}h baseline_b={} base={}".format(
        cfg.k, cfg.tau_hours, cfg.baseline_b, cfg.base_level))
    print()
    print("items      : {}".format(stats["volume_total"]))
    print("snapshots  : {}".format(len(series)))
    print("period     : {} -> {}".format(stats["first_ts"], stats["last_ts"]))
    print("level      : {:.3f}".format(stats["level"]))
    print("change     : {:+.3f} ({:+.3f}%)".format(
        stats["change"], stats["change_pct"]))
    print("S(t)       : {:+.4f}  (baseline {:+.4f})".format(
        stats["s_value"], stats["baseline"]))
    print("volume 24h : {}".format(stats["volume_24h"]))
    print("breadth    : {:.0%} positive / {:.0%} negative".format(
        stats["breadth_positive"], stats["breadth_negative"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
