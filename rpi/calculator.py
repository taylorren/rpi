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

Those contributions are combined into a decay- and impact-weighted mean - a
*level*, not a flow - where older news fades with half-life ``tau_hours`` and a
story's influence scales with how consequential it is:

.. math::

    S(t) = \\frac{\\sum_i s_i \\cdot |s_i|^{p} \\cdot 2^{-(t-t_i)/\\tau}}
                {\\sum_i |s_i|^{p} \\cdot 2^{-(t-t_i)/\\tau}}

The impact weighting (``weight_power``, :math:`p` above) is what keeps a day's
biggest story visible. A plain mean over hundreds of stories is a crowd index in
which no single event can hold more than a couple of percent of the weight, so
the level has almost nothing to move on: measured on the live corpus the largest
story of a day holds 2.4% of the weight at :math:`p = 0`, 8% at :math:`p = 2` and
17% at :math:`p = 3`. Zero disables it and recovers the plain mean.

The index then reads the *deviation from the baseline* directly:

.. math::

    RPI_t = base \\cdot \\exp\\left(c \\cdot (S(t) - b)\\right)

The level is a thermometer, not an odometer. Three properties follow, and each
is deliberate:

1. **It is a reading, not an accumulation.** Nothing carries over between
   snapshots, so the index revisits a level whenever the news does. An integral
   of a mean over hundreds of stories is too slow to read: it turns a month of
   news into one smooth arc with no days visible in it. A reading shows today.
2. **Silence causes no movement.** With nothing in the decay window the
   weighted mean is 0/0, so :math:`S(t)` falls back to ``b`` and the level
   returns to the base. The index only moves when there is news.
3. **An error in ``b`` is bounded.** It shifts the level by the constant factor
   :math:`\\exp(c \\cdot \\delta)` - about 1.1% for the live baseline's own
   standard error - so a baseline that is slightly wrong puts the whole chart
   slightly high or low instead of sending it away over time. This is why the
   calibration no longer has to be exact, and why the integrator's drift budget
   has no analogue here.

What the thermometer gives up is memory: it cannot show that the world has been
worse for a month, because it only ever reads the present. That is the trade,
and it is the intended one - the persistence of an event is carried by later
reports about it, each scored on its own day.

Neutral items carry :math:`\\varepsilon = 0`, so with the impact weighting on they
contribute nothing at all - which is the right reading once the scoring rule is
"score what this report changed": a report that changed nothing has no business
moving a world index. With the weighting off (:math:`p = 0`) they dilute
:math:`S(t)` toward zero instead, which is a different and weaker claim - that a
flood of unremarkable news is evidence the world is unremarkable.

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

# A window holding fewer stories than this is not a mean, it is one or two
# headlines, so the level it implies is not a reading of anything. The series
# therefore begins at the first snapshot whose window reaches the floor, and the
# thin prefix is simply not published. The calibration discards the same prefix
# for the same reason (``calibrate.WARMUP_MIN_ITEMS``); this is that floor
# applied to the chart, which matters now because a thermometer shows its input
# directly instead of damping it through an integral - on the live corpus the
# opening days swung the level to 85 and 116, which is a fact about coverage and
# not about the world.
MIN_WINDOW_ITEMS = 50


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
                 fallback: float, weight_power: float = 0.0) -> float:
    """Decay-weighted mean signed impact at ``at``.

    ``weight_power`` adds an impact weighting on top of the decay: each story's
    weight is multiplied by ``|signed| ** weight_power``, so a story twice as
    consequential counts 2**p times as much and a neutral story (signed exactly
    zero) contributes nothing at all. Zero disables it and recovers the plain
    mean. The sign still comes from the story, never from the weight.

    Returns ``fallback`` (the baseline) when nothing in range carries any
    weight, so silence produces no movement.
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
        if weight_power:
            weight *= abs(item.signed) ** weight_power
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
    series: List[Snapshot] = []

    while moment <= end:
        s_value = decayed_mean(items, times, moment, cfg.tau_hours, baseline,
                               cfg.weight_power)

        horizon = timedelta(hours=cfg.tau_hours * MAX_AGE_FACTOR)
        low = bisect.bisect_left(times, moment - horizon)
        high = bisect.bisect_right(times, moment)
        count = max(high - low, 0)

        # The thin opening days are not published: see MIN_WINDOW_ITEMS.
        if count >= MIN_WINDOW_ITEMS:
            # The thermometer. The level is a *reading* of the mood against
            # normal, not an accumulation of it: nothing carries over from the
            # previous snapshot, so the index revisits a level whenever the news
            # does, and a sustained deviation moves it once rather than
            # compounding every tick. That is what makes an individual day's news
            # visible at all - an integral of a mean over hundreds of stories
            # moves too slowly to read.
            series.append(Snapshot(
                ts=moment,
                level=cfg.base_level * math.exp(cfg.c * (s_value - baseline)),
                s_value=s_value,
                item_count=count,
            ))
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
