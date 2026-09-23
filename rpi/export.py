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

Usage::

    python -m rpi.export
    python -m rpi.export --out data/rpi.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import calculator, config as config_mod, paths, schema, storage

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
        volume = sum(1 for moment in item_times if start <= moment < end)
        out.append({
            "t": entry["t"].isoformat().replace("+00:00", "Z"),
            "level": round(entry["level"], 4),
            "s": round(entry["s"], 4),
            "ma": round(entry["ma"], 4),
            "n": entry["n"],
            "v": volume,
        })
    return out


def build_news(rows: Iterable[Any], cfg: config_mod.RpiConfig,
               conn: Any = None, limit: int = 400) -> List[Dict[str, Any]]:
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
    analysis_rows = storage.analysed_rows(conn, schema_version)
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

    # A story covered by six outlets is one item in the index, not six.
    stats["duplicates_removed"] = sum(
        max(int(calculator.row_get(row, "cluster_member_count") or 1) - 1, 0)
        for row in analysis_rows)
    # Backlog. A number that keeps climbing is the signature of a scoring
    # service that has stopped consuming work, which every other stage would
    # hide by continuing to succeed.
    stats["pending"] = storage.pending_count(conn, schema_version)

    item_times = [item.ts for item in items]

    def within(days: float) -> List[Tuple[datetime, float, float, int, float]]:
        cutoff = now - timedelta(days=days)
        return [point for point in points if point[0] >= cutoff]

    # "today" means the current UTC day, matching the fetcher's daily files.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_points = [point for point in points if point[0] >= midnight] or within(1.0)

    return {
        "meta": {
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "config_version": cfg.config_version,
            "schema_version": schema_version,
            "calibrated": cfg.calibrated,
            "base_level": cfg.base_level,
            "k": cfg.k,
            "tau_hours": cfg.tau_hours,
            "baseline_b": cfg.baseline_b,
            "scope_weights": cfg.scope_weights,
            "snapshot_minutes": cfg.snapshot_minutes,
            "ma_hours": MOVING_AVERAGE_HOURS,
            "last_analysis": storage.last_analysis_time(conn, schema_version),
            "model": storage.get_meta(conn, "model"),
        },
        "summary": stats,
        "windows": {
            "today": bucket(today_points, timedelta(minutes=cfg.snapshot_minutes),
                            item_times),
            "1w": bucket(within(7.0), timedelta(hours=1), item_times),
            "1m": bucket(within(30.0), timedelta(days=1), item_times),
        },
        "news": build_news(analysis_rows, cfg, conn=conn, limit=news_limit),
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
