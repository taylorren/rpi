#!/usr/bin/env python3
"""Score the same stored stories under two impact rubrics, and compare.

Why
---
The scale is compressed: 96.8% of live scores sit in 2-5, the model's entropy
over the eleven levels is 1.40 bits (about 2.6 effective levels), and nothing has
ever reached 10. A scale that cannot reach its own top cannot show a big event,
and no index formula can repair that - the compression is upstream of the
arithmetic.

The anchors are the cheapest lever: they are ours, they travel with the request,
and the served model needs no retraining. ``rpi.schema`` now carries a second set
(``IMPACT_ANCHORS_V2``) built on a single axis - depth of consequence, expressed
as duration and reversibility - with reach explicitly handed back to the
``scope`` field.

How
---
Each sampled story keeps its stored text, so there is no news in the comparison:
any difference is the rubric. The model is deterministic (one forward pass over
the candidate answer tokens, verified by tools/rescore_drift.py), so one call per
rubric per story is enough - no repeats and no sampling noise to average out.

Reading
---
* ``levels`` counts how many of the eleven levels the rubric actually reaches.
* ``>=7`` and the top histogram bins are the point: if the top stays empty under
  v2, the limit is the model's beliefs rather than the wording, and the next
  levers are fewer levels or a retrained adapter.
* Every story is printed, so a wrong move can be seen and not merely counted.
* Nothing here writes to the database. Adopting a rubric is a separate, deliberate
  step, because it bumps ``SCHEMA_VERSION`` and makes every stored score
  incomparable with the new ones.

Usage::

    python tools/anchor_ab.py --limit 30
    python tools/anchor_ab.py --limit 12 --since 2026-09-23
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import analyse, api, calculator, config as config_mod  # noqa: E402
from rpi import paths, schema, storage  # noqa: E402


def eligible(conn: Any, schema_version: int,
             min_summary: int) -> List[Dict[str, Any]]:
    """Stored stories with real text, oldest first - the fixed input for both runs."""
    rows = conn.execute(
        "SELECT i.id, i.title, i.summary, i.published, i.fetched_at, i.source,"
        "       a.sentiment, a.impact_expected, a.scope"
        "  FROM items i"
        "  JOIN analyses a ON a.item_id = i.id AND a.schema_version = ?"
        " WHERE LENGTH(COALESCE(i.summary, '')) >= ?"
        " ORDER BY COALESCE(i.published, i.fetched_at) ASC",
        (schema_version, min_summary)).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        moment = calculator.parse_ts(row["published"] or row["fetched_at"])
        if moment is None:
            continue
        out.append({
            "id": row["id"],
            "title": row["title"],
            "summary": row["summary"],
            "published": row["published"],
            "source": row["source"],
            "event": moment,
            "sentiment": row["sentiment"],
            "impact_expected": row["impact_expected"],
            "scope": row["scope"],
        })
    return out


def evenly(items: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Evenly spaced picks across the whole era - deterministic, no RNG."""
    if limit >= len(items):
        return list(items)
    step = len(items) / float(limit)
    return [items[int(i * step)] for i in range(limit)]


def spread(scores: Sequence[float]) -> Dict[str, float]:
    """The numbers that decide whether a rubric can show a big event."""
    if not scores:
        return {"n": 0.0}
    levels = {int(round(value)) for value in scores}
    return {
        "n": float(len(scores)),
        "mean": statistics.fmean(scores),
        "sd": statistics.pstdev(scores),
        "max": max(scores),
        "share7": sum(1 for value in scores if value >= 7) / len(scores),
        "levels": float(len(levels)),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--schema-version", type=int, default=schema.SCHEMA_VERSION,
                        help="stored scores to compare against (default %(default)s)")
    parser.add_argument("--since", type=str, default="2026-09-23", metavar="YYYY-MM-DD",
                        help="first day the source set was complete (default %(default)s)")
    parser.add_argument("--limit", type=int, default=30,
                        help="stories to score under both rubrics (default %(default)s)")
    parser.add_argument("--by-impact", action="store_true",
                        help="sample the highest stored-impact stories instead of "
                             "evenly across the era: tests whether the top of the "
                             "scale is reachable at all, which an unbiased sample "
                             "cannot show because extreme events are rare")
    parser.add_argument("--quiet", action="store_true",
                        help="omit the per-story table and print only the summary")
    parser.add_argument("--min-summary", type=int, default=100,
                        help="shortest usable summary, in characters")
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="print both rubrics and the sample, then stop")
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    endpoint = args.endpoint or cfg.endpoint

    conn = storage.connect(args.db)
    try:
        items = eligible(conn, args.schema_version, args.min_summary)
    finally:
        conn.close()

    era = [item for item in items if str(item["event"].date()) >= args.since]
    if not era:
        print("no stored stories at or after {} (earliest is {})".format(
            args.since, items[0]["event"].date() if items else "n/a"))
        return 1
    if args.by_impact:
        ranked = [item for item in era if item["impact_expected"] is not None]
        ranked.sort(key=lambda item: -float(item["impact_expected"]))
        sample = ranked[:args.limit]
    else:
        sample = evenly(era, args.limit)
    stored = [item["impact_expected"] for item in sample
              if item["impact_expected"] is not None]

    print("era    : {} .. {}, {} stored stories with >= {} char summaries".format(
        args.since, era[-1]["event"].date(), len(era), args.min_summary))
    print("sample : {} stories, {}".format(
        len(sample),
        "the highest stored impact" if args.by_impact
        else "evenly spaced across the era"))
    if stored:
        print("stored : schema_version={}, mean impact {:.2f}, max {:.2f}".format(
            args.schema_version, statistics.fmean(stored), max(stored)))
    print()

    in_use = schema.ANALYSIS_SCHEMA["impact"]
    if args.dry_run:
        for name, field in (("v1 (in use)", in_use),
                            ("v2 (candidate)", schema.impact_schema()["impact"])):
            print("{}: {}".format(name, field["description"]))
            for level in sorted(field["choice_descriptions"], key=int):
                print("  {:>2}  {}".format(level, field["choice_descriptions"][level]))
            print()
        for item in sample:
            print("  {:<64} {}".format(
                " ".join((item["title"] or "?").split())[:64], item["source"]))
        print()
        print("(dry run - nothing scored)")
        return 0

    new_schema = schema.impact_schema()
    try:
        health = api.health(endpoint)
    except api.ApiError as exc:
        print("FATAL scoring service not reachable at {}: {}".format(endpoint, exc))
        return 2
    print("model  : {}  ({})".format(health.get("model"), endpoint))
    print()

    if not args.quiet:
        header = "{:<58} {:>7} {:>7} {:>8}".format("story", "v1", "v2", "v2-v1")
        print(header)
        print("-" * len(header))

    pairs: List[Dict[str, Any]] = []
    failures = 0
    for item in sample:
        context = analyse.build_context(item)
        try:
            before = analyse.parse_response(
                api.score(context, schema.ANALYSIS_SCHEMA, schema.SCORE_FIELDS,
                          endpoint=endpoint))
            after = analyse.parse_response(
                api.score(context, new_schema, schema.SCORE_FIELDS,
                          endpoint=endpoint))
        except api.ApiError as exc:
            failures += 1
            print("  FAILED {}: {}".format(str(item["id"])[:12], exc))
            continue
        v1, v2 = before["impact_expected"], after["impact_expected"]
        if v1 is None or v2 is None:
            failures += 1
            continue
        pairs.append({"item": item, "before": before, "after": after})
        if not args.quiet:
            print("{:<58} {:>7.2f} {:>7.2f} {:>+8.2f}".format(
                " ".join((item["title"] or "?").split())[:58], v1, v2, v2 - v1))

    print()
    if not pairs:
        print("no successful pairs; nothing to compare")
        return 2

    old = [p["before"]["impact_expected"] for p in pairs]
    new = [p["after"]["impact_expected"] for p in pairs]
    same_sentiment = sum(1 for p in pairs
                         if p["before"]["sentiment"] == p["after"]["sentiment"])
    same_scope = sum(1 for p in pairs if p["before"]["scope"] == p["after"]["scope"])

    print("=" * 78)
    print("only the impact rubric changed, so these two should be identical:")
    print("  sentiment agrees on {}/{}    scope agrees on {}/{}".format(
        same_sentiment, len(pairs), same_scope, len(pairs)))
    print()

    labels = ["0-1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]
    bins = [(0, 2), (2, 3), (3, 4), (4, 5), (5, 6),
            (6, 7), (7, 8), (8, 9), (9, 10), (10, 11)]
    for name, values in (("v1 (in use)", old), ("v2 (candidate)", new)):
        counts = [sum(1 for v in values if low <= v < high) for low, high in bins]
        print("{:<14}".format(name) + " ".join(
            "{}:{:<3}".format(label, count) for label, count in zip(labels, counts)))
    print()

    header = "{:<15} {:>6} {:>6} {:>6} {:>6} {:>7} {:>8}".format(
        "rubric", "mean", "sd", "max", "levels", "share>=7", "min")
    print(header)
    print("-" * len(header))
    for name, values in (("stored", stored), ("v1 (re-run)", old),
                         ("v2 (candidate)", new)):
        if not values:
            continue
        stats = spread(values)
        print("{:<15} {:>6.2f} {:>6.2f} {:>6.2f} {:>6.0f} {:>6.1%} {:>8.2f}".format(
            name, stats["mean"], stats["sd"], stats["max"], stats["levels"],
            stats["share7"], min(values)))
    if failures:
        print("{} call(s) failed and are excluded".format(failures))

    movers = sorted(pairs, key=lambda p: -abs(
        p["after"]["impact_expected"] - p["before"]["impact_expected"]))
    print()
    print("signed impact - polarity x impact x scope weight, what the index eats:")
    for name, key in (("v1 (re-run)", "before"), ("v2 (candidate)", "after")):
        values = [calculator.POLARITY.get(str(p[key]["sentiment"]).lower(), 0)
                  * float(p[key]["impact_expected"])
                  * cfg.scope_weight(p[key]["scope"]) for p in pairs]
        print("  {:<15} mean {:+.3f}  sd {:.3f}  min {:+.2f}  max {:+.2f}  "
              "|signed| >= 5: {:.0%}".format(
                  name, statistics.fmean(values), statistics.pstdev(values),
                  min(values), max(values),
                  sum(1 for v in values if abs(v) >= 5) / len(values)))
    print()
    print("biggest moves (v2 minus v1):")
    for entry in movers[:5]:
        print("  {:+.2f}  {}".format(
            entry["after"]["impact_expected"] - entry["before"]["impact_expected"],
            " ".join((entry["item"]["title"] or "?").split())[:70]))
    print()
    print("reading: if the top bins stay empty under v2, the wording is not the")
    print("binding constraint - the model's beliefs are - and the next levers are")
    print("fewer levels or a retrained adapter. If they fill, the rubric was the")
    print("constraint, and adopting it means bumping SCHEMA_VERSION and re-scoring")
    print("the corpus, because scores from two rubrics are not comparable.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


