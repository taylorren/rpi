#!/usr/bin/env python3
"""Export the index as a static JSON document for the UI.

The UI is deliberately dumb: it reads one JSON file and draws a chart. All
bucketing happens here, where it can be tested, rather than in JavaScript.

Raw 15-minute snapshots would be ~2,880 points for a month, which is noisy to
look at and wasteful to ship, so each window is bucketed to a sensible
resolution and the **last** value in each bucket is taken (a close, in index
terms):

===========  ===========  ==============
Window       Resolution   Typical points
===========  ===========  ==============
``today``    raw          96
``1w``       hourly       168
``1m``       daily        30
===========  ===========  ==============

The market's own series (``market``, see :mod:`rpi.market`) is bucketed by the
same rule and published per window beside the index's, so the page can draw the
two together without deciding anything itself. It is a separate section rather
than a field on the index's points because the two are separate readings that
happen to share an axis.

Usage::

    python -m rpi.export
    python -m rpi.export --out data/rpi.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import bisect

from . import calculator, config as config_mod, market, paths, schema, storage

_LN2 = math.log(2.0)

# How many reports a chart point's tooltip names. Three is what fits under a
# cursor without covering the chart, and on the live corpus the top three
# usually hold a fifth of the weight between them - enough to explain the point.
TOOLTIP_CONTRIBUTORS = 3

# Window for the moving average. 24 hours is the conventional "daily trend"
# reading of an index and is what the requirements' note about typical stock
# index indicators is asking for.
MOVING_AVERAGE_HOURS = 24


def trailing_mean(values: Sequence[float], times: Sequence[datetime],
                  window: timedelta) -> List[float]:
    """Trailing mean of ``values`` over ``window``, in a single pass.

    A moving average is the standard way to read an index without reacting to
    every twitch, so the underlying news trend can be seen through the noise.
    """
    seconds = window.total_seconds()
    out: List[float] = []
    total = 0.0
    start = 0
    for index, moment in enumerate(times):
        total += values[index]
        while start < index and (moment - times[start]).total_seconds() > seconds:
            total -= values[start]
            start += 1
        out.append(total / (index - start + 1))
    return out


def bucket(points: List[Tuple[datetime, float, float, int, float]],
           delta: timedelta,
           item_times: Sequence[datetime] = ()) -> List[Dict[str, Any]]:
    """Reduce points to one per bucket, keeping the last value in each.

    ``v`` counts items *published* inside that bucket - volume, as a reader
expects under an index chart. That is a different quantity from ``n``, the
number of items currently influencing the index, which is a rolling figure
and is carried along as context for the decay maths.

    ``item_times`` must be sorted ascending, because the count is taken with
    ``bisect`` rather than by scanning the sequence per bucket. An unsorted
    sequence would silently under-count instead of raising, so callers must
    keep that promise: ``calculator.build_items`` returns items sorted by
    ``ts``, which is what the pipeline passes in.
    """
    if not points:
        return []

    step = max(int(delta.total_seconds()), 1)
    grouped: Dict[int, Dict[str, Any]] = {}
    order: List[int] = []

    for ts, level, s_value, count, ma in points:
        key = int(ts.timestamp()) // step
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {"t": ts, "level": level, "s": s_value,
                            "n": count, "ma": ma}
            order.append(key)
        else:
            entry.update({"t": ts, "level": level, "s": s_value,
                          "n": count, "ma": ma})

    out: List[Dict[str, Any]] = []
    for key in order:
        entry = grouped[key]
        start = datetime.fromtimestamp(key * step, tz=timezone.utc)
        end = start + delta
        lo = bisect.bisect_left(item_times, start)
        hi = bisect.bisect_left(item_times, end)
        volume = hi - lo
        out.append({
            "t": entry["t"].isoformat().replace("+00:00", "Z"),
            "level": round(entry["level"], 4),
            "s": round(entry["s"], 4),
            "ma": round(entry["ma"], 4),
            "n": entry["n"],
            "v": volume,
        })
    return out


def market_points(bars: Sequence[Tuple[datetime, float]]) -> List[Dict[str, Any]]:
    """Market readings as chart points, in the shape the index's points use.

    ``value`` is the market's own level - the number a reader could look up -
    and not the scaled value the page draws, because the scaling is a property
    of the window being viewed and this is the reading itself.
    """
    return [{"t": moment.isoformat().replace("+00:00", "Z"),
             "value": round(value, 4)} for moment, value in bars]


def bucket_market(bars: Sequence[Tuple[datetime, float]],
                  delta: timedelta) -> List[Dict[str, Any]]:
    """Reduce market bars to one per bucket, keeping the last value in each.

    The same rule, and the same epoch-aligned buckets, as :func:`bucket` above:
    a market point and an index point that land in one bucket are then two
    readings of the same interval rather than two points of different
    arithmetic, which is what makes the two lines comparable at all.

    Each point keeps its *own* timestamp rather than the bucket's. A reading is
    drawn where it was true, and a session's close really did happen at 07:00
    UTC - the index's daily point for the same date sits at the end of the UTC
    day because that is when its own last reading was taken. The two are a few
    hours apart on a chart and that gap is a fact, not a rounding error.
    """
    if not bars:
        return []
    step = max(int(delta.total_seconds()), 1)
    grouped: Dict[int, Tuple[datetime, float]] = {}
    order: List[int] = []
    for moment, value in bars:
        key = int(moment.timestamp()) // step
        if key not in grouped:
            order.append(key)
        grouped[key] = (moment, value)
    return market_points([grouped[key] for key in order])


def build_market(conn: Any, now: Optional[datetime] = None,
                 symbol: str = market.SYMBOL) -> Optional[Dict[str, Any]]:
    """The market's readings for the chart's three windows, or ``None``.

    ``None`` rather than an empty section when nothing is stored. The page
    treats a missing key as "say nothing about the market", and that has to
    stay distinguishable from "the market did not move", which is a reading: an
    absent line means the fetch has never succeeded, not that the market was
    quiet. It is the same contract ``meta.calibration`` uses, and for the same
    reason - a section that cannot be missing cannot report a failure.

    Resolutions are chosen per window from whatever is stored, so this works
    whether the last successful fetch was the primary's 5-minute series or the
    fallback's daily one:

    ===========  ===================  ==================
    Window       Market resolution    Mirrors the index
    ===========  ===================  ==================
    ``today``    as fetched           15-minute snapshots
    ``1w``       hourly               hourly buckets
    ``1m``       daily                daily buckets
    ===========  ===================  ==================

    ``today`` is passed through unbucketed because the provider's own cadence is
    already finer than the index's snapshots, and re-bucketing would throw away
    the market's only intraday detail to match a resolution it already beats.
    """
    now = now or datetime.now(timezone.utc)
    rows = storage.market_bars(conn, symbol)
    if not rows:
        return None

    # One reading per moment. When a fallback's daily bar and the primary's
    # 5-minute bar describe the same instant, the finer one is the real reading
    # and the coarse one was only ever standing in for it.
    kept: Dict[str, Tuple[int, Any]] = {}
    for row in rows:
        rank = market.RESOLUTION_RANK.get(row["resolution"], 0)
        current = kept.get(row["ts"])
        if current is None or rank >= current[0]:
            kept[row["ts"]] = (rank, row)
    ordered = sorted((entry[1] for entry in kept.values()),
                     key=lambda row: row["ts"])

    bars: List[Tuple[datetime, float]] = []
    for row in ordered:
        moment = calculator.parse_ts(row["ts"])
        if moment is not None:
            bars.append((moment, float(row["value"])))
    if not bars:
        return None

    # How far apart the provider's own bars are, which is what says whether the
    # market was trading at a given moment. The published windows are bucketed
    # wider than this, and each reports its own cadence separately.
    newest = ordered[-1]
    bar_minutes = market.RESOLUTION_MINUTES.get(newest["resolution"], 5)

    def since(start: datetime) -> List[Tuple[datetime, float]]:
        """Bars from ``start`` on, preceded by the last value known at ``start``.

        The market is shut for most of a day, so a window can begin with no bar
        of its own inside it - and a line that starts halfway across the chart
        reads as missing data rather than as a market that had not opened yet.
        The last bar from before the window is therefore carried in, stamped at
        the window's start, which is what that value *was* at that moment: a
        close is the market's level until the next session opens. It also puts
        the previous close at the left edge of the "today" window, which is what
        today's move is measured against.

        Only when the window's start has no reading of its own nearby. If the
        market was trading at that moment, its first bar inside the window is the
        reading for it, and a carried copy of the previous bar would put a stale
        value at the edge of the chart and then step to the real one seconds
        later.
        """
        earlier = [pair for pair in bars if pair[0] < start]
        inside = [pair for pair in bars if pair[0] >= start]
        shut_at_start = (not inside or
                         (inside[0][0] - start) > timedelta(minutes=bar_minutes))
        if earlier and shut_at_start:
            # Truncated, because every other stamp in the store is: the cutoff
            # comes from the clock, which carries microseconds that no bar ever
            # has, and a fixed-width stamp is what the rest of the pipeline
            # compares and orders by.
            inside = [(start.replace(microsecond=0), earlier[-1][1])] + inside
        return inside

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def window(start: datetime, delta: Optional[timedelta],
               cadence_minutes: int) -> Dict[str, Any]:
        """One window: its readings, how far apart they should be, and its move.

        ``cadence_minutes`` is what tells the page where the market was shut.
        The market trades 09:30-15:00 CST (01:30-07:00 UTC) on working days -
        5.5 hours out of 24 - so most of every chart is a period with no market
        in it, and the line is carried across it because the last price still
        stands. A segment wider than the cadence is therefore a shut market (or
        a missed fetch), and is drawn as such rather than as a market that did
        nothing.

        ``change_pct`` is measured from the window's first published reading to
        its last, which is the same convention the page uses for the index's own
        window change - so the two numbers are like for like whatever window the
        reader picked, even though the two clocks are not. Measuring from the
        raw bars instead would disagree with the line whenever a bucket dropped
        the first of them.
        """
        readings = since(start)
        points = (market_points(readings) if delta is None
                  else bucket_market(readings, delta))
        first = points[0]["value"] if points else None
        last = points[-1]["value"] if points else None
        return {
            "points": points,
            "cadence_minutes": cadence_minutes,
            "change_pct": (None if not first
                           else round((last - first) / first * 100.0, 4)),
        }

    # Each window uses the finest resolution that covers it, and reports the
    # cadence that resolution implies: the today window is the provider's own
    # bars (5 minutes, or a day if the fallback answered), and the wider windows
    # are bucketed to an hour and a day respectively.
    windows = {
        "today": window(midnight, None, bar_minutes),
        "1w": window(now - timedelta(days=7.0), timedelta(hours=1), 60),
        "1m": window(now - timedelta(days=30.0), timedelta(days=1), 24 * 60),
    }

    return {
        "symbol": symbol,
        "name": market.NAME,
        "local_name": market.LOCAL_NAME,
        "source": newest["source"],
        "resolution": newest["resolution"],
        # When the newest bar was taken, which is not when it was fetched: a
        # reading is only as fresh as its own timestamp, and a page that shows a
        # market number has to be able to say how old it is.
        "as_of": newest["ts"],
        "latest": {
            "t": newest["ts"],
            "value": round(float(newest["value"]), 4),
            "change": (None if newest["change"] is None
                       else round(float(newest["change"]), 4)),
            "change_pct": (None if newest["change_pct"] is None
                           else round(float(newest["change_pct"]), 4)),
        },
        "windows": windows,
    }


def attach_contributors(window: List[Dict[str, Any]],
                        items: Sequence[calculator.ScoredItem],
                        labels: Dict[str, Dict[str, str]],
                        cfg: config_mod.RpiConfig,
                        top: int) -> None:
    """Name the reports carrying each point, in place.

    A point is not moved by "the news" in general but by the handful of reports
    the decay and the impact weighting leave holding the weight, and a reader
    asking *why is it 99.9 here* wants those names rather than the aggregate.

    The shares use the same half-life and the same impact weighting as
    ``calculator.decayed_mean``, so they add up to the reading the point already
    shows - which is also the check that this is not a second, drifting
    explanation of the same number. A neutral story carries no weight at all
    under the impact weighting, so it can never appear here.

    One row is reserved for each side. Weight is ``|signed| ** p``, and
    achievement-style news scores higher than disaster, so ranking by weight
    alone returns three positive stories even on a day the index is falling -
    which reads as a contradiction. Reserving a slot for the heaviest negative
    and the heaviest positive shows the balance the level is actually made of.

    Only reports already published at the point count: a past point is explained
    by what was known then, not by what arrived afterwards.
    """
    if top <= 0 or not items:
        return
    horizon_hours = cfg.tau_hours * calculator.MAX_AGE_FACTOR
    for point in window:
        at = calculator.parse_ts(point.get("t"))
        if at is None:
            continue
        weighted: List[Tuple[float, calculator.ScoredItem]] = []
        total = 0.0
        for item in items:
            age_hours = (at - item.ts).total_seconds() / 3600.0
            if age_hours < 0.0 or age_hours > horizon_hours:
                continue
            weight = math.exp(-_LN2 * age_hours / cfg.tau_hours)
            if cfg.weight_power:
                weight *= abs(item.signed) ** cfg.weight_power
            weighted.append((weight, item))
            total += weight
        if total <= 0.0:
            continue
        weighted.sort(key=lambda pair: -pair[0])

        chosen: List[Tuple[float, calculator.ScoredItem]] = []
        taken: set = set()
        for want_positive in (True, False):
            for weight, item in weighted:
                if (item.signed > 0.0) == want_positive and item.signed != 0.0:
                    chosen.append((weight, item))
                    taken.add(item.item_id)
                    break
        for pair in weighted:
            if len(chosen) >= top:
                break
            if pair[1].item_id not in taken:
                chosen.append(pair)
                taken.add(pair[1].item_id)
        chosen.sort(key=lambda pair: -pair[0])

        named: List[Dict[str, Any]] = []
        for weight, item in chosen[:top]:
            label = labels.get(item.item_id) or {}
            named.append({
                "title": (label.get("title") or "")[:90],
                "source": label.get("source") or "",
                "signed": round(item.signed, 2),
                "share": round(weight / total, 4),
            })
        point["top"] = named


def build_news(rows: Iterable[Any], cfg: config_mod.RpiConfig,
               conn: Any = None, limit: int = 400,
               start: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """The audit list: which news items produced the index, with provenance.

    Newest first, capped so the export cannot grow without bound. Each entry
    carries enough detail to explain its own contribution - the raw inputs
    (sentiment, impact, scope, scope weight) and the resulting signed value -
    plus how many duplicate reports were folded into it, so a merged story is
    visibly merged rather than silently dropped.
    """
    news: List[Dict[str, Any]] = []
    for row in rows:
        ts = (calculator.parse_ts(calculator.row_get(row, "cluster_first_published"))
              or calculator.parse_ts(calculator.row_get(row, "published"))
              or calculator.parse_ts(calculator.row_get(row, "fetched_at")))
        if ts is None:
            continue
        if start is not None and ts < start:
            continue
        sentiment = (row["sentiment"] or "").lower()
        scope = row["scope"] or ""
        expected = row["impact_expected"]
        weight = cfg.scope_weight(scope)
        cluster_id = calculator.row_get(row, "cluster_id")
        member_count = int(calculator.row_get(row, "cluster_member_count") or 1)
        sources = []
        if conn is not None and cluster_id is not None and member_count > 1:
            # set() because one outlet can contribute several members, and the
            # total is already conveyed by member_count.
            sources = sorted({
                s for s in storage.cluster_sources(conn, int(cluster_id))
                if s and s != (row["source"] or "")
            })
        news.append({
            "id": row["id"],
            "t": ts.isoformat().replace("+00:00", "Z"),
            "title": row["title"] or "(untitled)",
            "link": row["link"],
            "source": row["source"] or "unknown",
            "sentiment": sentiment or "unknown",
            "impact": round(float(expected), 4) if expected is not None else None,
            "pick": int(row["impact"]) if row["impact"] is not None else None,
            "scope": scope or "unknown",
            "w": round(weight, 4),
            "signed": round(calculator.signed_impact(sentiment, expected, scope, cfg), 4),
            "dupes": member_count,
            "also_in": sources,
        })

    news.sort(key=lambda entry: entry["t"], reverse=True)
    return news[:limit]


def calibration_progress(conn: Any, cfg: config_mod.RpiConfig,
                         schema_version: int,
                         now: datetime) -> Optional[Dict[str, Any]]:
    """Estimate when the baseline can be frozen, for the UI to display.

    The estimate itself is owned by ``rpi.calibrate`` - duplicating the
    autocorrelation and effective-sample-size maths here would create a second
    source of truth for a number a reader will compare against that tool's own
    output. So its ``fit`` is imported and called, not reimplemented, which also
    means the warm-up cut is decided in exactly one place for both callers: the
    opening days carried a handful of stories, and those readings otherwise
    dominate the spread and drag the date with them.

    The date is published even while the estimate is still settling, because "not
    yet" is less useful to a reader than "roughly when" - but it is published as a
    range rather than an appointment. ``spread_days`` carries how far the date
    could be out on the sample's own evidence, and ``settled`` says whether the
    sample is large enough for the date to be read at face value at all. Both are
    for the page to hedge with; the honest alternative, hiding the date, was
    rejected because the projection is the question a reader actually has.

    Two deliberate choices:

    * Imported lazily, inside the function. Nothing on the hourly path should
      gain an import it does not need, and ``calibrate`` is the one module that
      touches NumPy.
    * Any failure returns ``None`` rather than raising. This feeds a single
      informational line on a page; a missing date is a small loss, while an
      exception would take the whole export - and with it the index itself -
      down with it. The page omits the line when the key is absent.

    Returns ``None`` once ``b`` is calibrated: the date is then no longer a
    question, and the bootstrap banner next to it has already disappeared.
    """
    if cfg.calibrated:
        return None

    try:
        from . import calibrate
    except Exception:  # pragma: no cover - import is stdlib-only
        return None

    try:
        rows = storage.snapshots(conn, cfg.config_version, schema_version)
        stats = calibrate.fit(rows, cfg.snapshot_minutes, cfg.config_version,
                              target_se=calibrate.level_target_se(cfg))
    except Exception:  # pragma: no cover - defensive, see docstring
        return None

    if stats.get("reason"):
        return None

    days = float(stats["days"])
    needed = float(stats["days_needed"])
    # ``days_needed`` equals ``days`` once ready, so the clamp keeps the
    # countdown from going negative on the day the target is met.
    remaining = max(needed - days, 0.0)
    expected = now + timedelta(days=remaining)

    return {
        "ready": bool(stats["ready"]),
        # False while the sample is too small for the date to be read at face
        # value. The page hedges with it rather than hiding the date.
        "settled": bool(stats.get("settled", False)),
        "days": round(days, 1),
        "days_needed": round(needed),
        "days_remaining": round(remaining),
        # How far the date could be out on the sample's own evidence. The date is
        # re-fitted whenever the spread or the correlation time moves, so on a
        # young series it largely re-dates itself; this is what says so.
        "spread_days": round(float(stats.get("spread_days", 0.0))),
        "warmup_days": round(float(stats.get("dropped_days", 0.0)), 1),
        "expected_on": expected.date().isoformat(),
        "mean_b": round(float(stats["mean"]), 4),
        "se": round(float(stats["se"]), 4),
        "target_se": float(stats.get("target_se", calibrate.TARGET_SE)),
        # Carried so the page can show what moves the date, and so a reader can
        # reconcile it with ``python -m rpi.calibrate`` line by line.
        "sd": round(float(stats["sd"]), 4),
        "tau_c_days": round(float(stats["tau_c_days"]), 2),
        "n_effective": round(float(stats["n_effective"]), 1),
    }


def build_payload(conn: Any, cfg: config_mod.RpiConfig, schema_version: int,
                  now: Optional[datetime] = None,
                  news_limit: int = 400) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)

    rows = storage.snapshots(conn, cfg.config_version, schema_version)
    stamps: List[datetime] = []
    levels: List[float] = []
    raw: List[Tuple[datetime, float, float, int]] = []
    for row in rows:
        ts = calculator.parse_ts(row["ts"])
        if ts is None:
            continue
        stamps.append(ts)
        levels.append(float(row["level"]))
        raw.append((ts, float(row["level"]), float(row["s_value"]),
                    int(row["item_count"])))

    # One query feeds both the index series and the audit list.
    analysis_rows = storage.analysed_rows(conn, schema_version,
                                          per_source=cfg.items_per_source)
    items = calculator.build_items(analysis_rows, cfg)

    # Computed on the full snapshot series, so the average means the same thing
    # in every window rather than being recomputed per window.
    averages = trailing_mean(levels, stamps,
                             timedelta(hours=MOVING_AVERAGE_HOURS))
    points: List[Tuple[datetime, float, float, int, float]] = [
        (raw[i][0], raw[i][1], raw[i][2], raw[i][3], averages[i])
        for i in range(len(raw))
    ]

    series = [
        calculator.Snapshot(ts=ts, level=level, s_value=s_value, item_count=count)
        for ts, level, s_value, count, _ma in points
    ]
    stats = calculator.summarise(items, series, cfg, now=now)

    # How many reports the index does not read. With items_per_source on, a story
    # counts once per outlet, so what is folded away is the extra reports from an
    # outlet that wrote several about it; off, it is every report but the
    # representative's. Either way it is the same kind of thing - reports that
    # exist in the corpus and carry no weight - so the page keeps one figure.
    read_per_story: Optional[Dict[int, int]] = None
    if cfg.items_per_source:
        read_per_story = {int(cid): int(n) for cid, n in conn.execute(
            "SELECT m.cluster_id, COUNT(DISTINCT i.source)"
            "  FROM cluster_members m JOIN items i ON i.id = m.item_id"
            " GROUP BY m.cluster_id")}
    folded = 0
    counted: set = set()
    for row in analysis_rows:
        cluster_id = calculator.row_get(row, "cluster_id")
        if cluster_id is None:
            continue
        key = int(cluster_id)
        # Once per cluster: every row of a multi-source story carries the same
        # member_count, so counting per row would multiply the loss by the number
        # of outlets that covered it.
        if key in counted:
            continue
        counted.add(key)
        members = int(calculator.row_get(row, "cluster_member_count") or 1)
        kept = 1
        if read_per_story is not None:
            kept = read_per_story.get(key, 1)
        folded += max(members - kept, 0)
    stats["duplicates_removed"] = folded
    # Backlog. A number that keeps climbing is the signature of a scoring
    # service that has stopped consuming work, which every other stage would
    # hide by continuing to succeed.
    stats["pending"] = storage.pending_count(conn, schema_version,
                                            per_source=cfg.items_per_source)
    # Parked failures are a different problem with a different fix, so they are
    # published separately rather than folded into the backlog - folding them in
    # is what kept the backlog permanently non-zero after the 2026-09-24 CUDA
    # fault, which turned the health check into background noise.
    stats["failed"] = storage.failed_count(conn, schema_version,
                                           per_source=cfg.items_per_source)

    item_times = [item.ts for item in items]

    def within(days: float) -> List[Tuple[datetime, float, float, int, float]]:
        cutoff = now - timedelta(days=days)
        return [point for point in points if point[0] >= cutoff]

    # "today" means the current UTC day, matching the fetcher's daily files.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_points = [point for point in points if point[0] >= midnight] or within(1.0)
    news_windows = {
        "today": build_news(analysis_rows, cfg, conn=conn, limit=news_limit,
                            start=midnight),
        "1w": build_news(analysis_rows, cfg, conn=conn, limit=news_limit,
                         start=now - timedelta(days=7.0)),
        "1m": build_news(analysis_rows, cfg, conn=conn, limit=news_limit,
                         start=now - timedelta(days=30.0)),
    }

    windows = {
        "today": bucket(today_points, timedelta(minutes=cfg.snapshot_minutes),
                        item_times),
        "1w": bucket(within(7.0), timedelta(hours=1), item_times),
        "1m": bucket(within(30.0), timedelta(days=1), item_times),
    }
    # What the chart's tooltip names when a reader asks why a point sits where it
    # does. Built here rather than in the page so the shares come from the same
    # arithmetic as the level they are explaining.
    labels = {row["id"]: {"title": row["title"] or "", "source": row["source"] or ""}
              for row in analysis_rows}
    for window in windows.values():
        attach_contributors(window, items, labels, cfg, TOOLTIP_CONTRIBUTORS)

    return {
        "meta": {
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "config_version": cfg.config_version,
            "schema_version": schema_version,
            "calibrated": cfg.calibrated,
            "base_level": cfg.base_level,
            "k": cfg.k,
            "c": cfg.c,
            "tau_hours": cfg.tau_hours,
            "weight_power": cfg.weight_power,
            "baseline_b": cfg.baseline_b,
            "scope_weights": cfg.scope_weights,
            "snapshot_minutes": cfg.snapshot_minutes,
            "ma_hours": MOVING_AVERAGE_HOURS,
            "last_analysis": storage.last_analysis_time(conn, schema_version),
            "model": storage.get_meta(conn, "model"),
            # Absent once b is calibrated, or if the estimate could not be
            # made. The page treats a missing key as "say nothing", never as
            # an error, so the index still publishes either way.
            "calibration": calibration_progress(
                conn, cfg, schema_version, now),
        },
        "summary": stats,
        "windows": windows,
        "news_windows": news_windows,
        "news": build_news(analysis_rows, cfg, conn=conn, limit=news_limit),
        # The market's own readings, for the page to draw beside the index.
        # Absent (not empty) when nothing has been stored, so the page can tell
        # "the market fetch has never worked" from "the market did not move".
        "market": build_market(conn, now=now),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the RPI series as JSON for the UI.")
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--out", type=Path, default=paths.EXPORT_PATH)
    parser.add_argument("--schema-version", type=int, default=schema.SCHEMA_VERSION)
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    conn = storage.connect(args.db)
    try:
        payload = build_payload(conn, cfg, args.schema_version)
    finally:
        conn.close()

    if not payload["windows"]["1m"] and not payload["windows"]["today"]:
        print("no snapshots to export; run rpi.calculator first")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8")

    stats = payload["summary"]
    print("wrote {} ({} bytes)".format(args.out, args.out.stat().st_size))
    print("  level {:.3f}  change {:+.3f} ({:+.3f}%)".format(
        stats["level"], stats["change"], stats["change_pct"]))
    for name, series in payload["windows"].items():
        print("  window {:<6} {} points".format(name, len(series)))
    if not cfg.calibrated:
        print("  NOTE baseline_b is still the bootstrap value (not calibrated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
