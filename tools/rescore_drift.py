#!/usr/bin/env python3
"""Separate a worsening world from a harsher instrument, by re-scoring old news.

Why
---
The index cannot tell the two apart, by construction. ``b`` is a frozen
constant, so any change in how negative the news *sounds* lands on the index as
a change in how the world *is*. Every argument about whether the world is really
getting worse therefore bottoms out in a question the stored data cannot answer
on its own:

    if the same story were scored today, would it still score the same way?

This asks it directly: take items that were already scored, re-score their
stored text, and compare. The text is fixed, so the difference carries no news.
It is the instrument, measured.

The stratifying trick
---------------------
A single mean difference would be uninterpretable - a model could be harsher on
average and the average would not say *when* it changed. So the sample is split
by when each item was scored. If the instrument drifted, items scored early
(under the milder model) shift further than items scored recently. A difference
that is flat across the strata is not instrument drift; one that grows with time
is.

Sampling
--------
Restricted by default to the era in which all six sources were present
(2026-09-23 on the live corpus, found by first appearance per source). Earlier
items come from a different feed mix, and because the outlets differ sharply in
what they publish (mean signed impact +0.76 for CGTN against -1.11 for NYT),
mixing eras would confound "the model changed" with "the corpus changed" - which
is the whole thing being tested. Title-only items are excluded too: a missing
summary is a different measurement.

Reading the output
------------------
* ``repeat`` is the noise floor: the same text scored R times with nothing
  changed in between. Anything inside it is sampling, not drift.
* ``delta`` is new minus stored in signed-impact units. Negative means the
  instrument now reads the same text as *worse*.
* ``stored`` is a single observation, so its own sampling error is unknown; the
  repeat column is the best available estimate of that error.
* The model name recorded when the stored scores were produced is compared with
  the name the service reports now. A changed name explains everything else.

Usage::

    python tools/rescore_drift.py --per-stratum 8 --repeat 2
    python tools/rescore_drift.py --since 2026-09-23 --per-stratum 15 --dry-run
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import analyse, api, calculator, config as config_mod  # noqa: E402
from rpi import paths, schema, storage  # noqa: E402


def eligible(conn: Any, schema_version: int,
             min_summary: int) -> List[Dict[str, Any]]:
    """Scored items with real text, oldest scoring first."""
    rows = conn.execute(
        "SELECT i.id, i.title, i.summary, i.published, i.fetched_at, i.source,"
        "       a.sentiment, a.impact_expected, a.analyzed_at"
        "  FROM items i"
        "  JOIN analyses a ON a.item_id = i.id AND a.schema_version = ?"
        " WHERE LENGTH(COALESCE(i.summary, '')) >= ?",
        (schema_version, min_summary)).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        event = calculator.parse_ts(row["published"] or row["fetched_at"])
        scored = calculator.parse_ts(row["analyzed_at"])
        if event is None or scored is None:
            continue
        out.append({
            "id": row["id"],
            "title": row["title"],
            "summary": row["summary"],
            "published": row["published"],
            "source": row["source"],
            "event": event,
            "scored": scored,
            "sentiment": row["sentiment"],
            "impact_expected": row["impact_expected"],
        })
    out.sort(key=lambda item: item["scored"])
    return out


def signed_impact(sentiment: Optional[str],
                  impact: Optional[float]) -> Optional[float]:
    """The quantity the index consumes, before the scope weight."""
    if sentiment is None or impact is None:
        return None
    sign = calculator.POLARITY.get(str(sentiment).strip().lower())
    if sign is None:
        return None
    return sign * float(impact)


def stratify(items: Sequence[Dict[str, Any]], strata: int,
             per_stratum: int) -> List[Tuple[int, List[Dict[str, Any]]]]:
    """Evenly spaced picks from equal-count time bands - deterministic, no RNG.

    Even spacing rather than random sampling so two runs on the same database
    are comparable, which matters when the answer is a difference of small
    means.
    """
    bands: List[Tuple[int, List[Dict[str, Any]]]] = []
    size = len(items) / float(max(strata, 1))
    for band in range(strata):
        lo = int(round(band * size))
        hi = int(round((band + 1) * size))
        chunk = list(items[lo:hi])
        if not chunk:
            continue
        step = max(len(chunk) // max(per_stratum, 1), 1)
        bands.append((band, chunk[::step][:per_stratum]))
    return bands


def band_label(band: int, total: int, chunk: Sequence[Dict[str, Any]]) -> str:
    """Human name for a time band, with the scoring dates it covers."""
    when = "{} .. {}".format(chunk[0]["scored"].date(), chunk[-1]["scored"].date())
    if total == 2:
        name = "early" if band == 0 else "late"
    else:
        name = "band {}".format(band + 1)
    return "{} ({})".format(name, when)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--schema-version", type=int, default=schema.SCHEMA_VERSION)
    parser.add_argument("--since", type=str, default="2026-09-23", metavar="YYYY-MM-DD",
                        help="first day the source set was complete (default %(default)s)")
    parser.add_argument("--strata", type=int, default=2,
                        help="time bands to split the era into (2 = early vs late)")
    parser.add_argument("--per-stratum", type=int, default=8,
                        help="items to re-score from each band (default %(default)s)")
    parser.add_argument("--repeat", type=int, default=2,
                        help="re-scores per item, for the sampling noise floor "
                             "(default %(default)s)")
    parser.add_argument("--min-summary", type=int, default=100,
                        help="shortest usable summary, in characters")
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="show the sample without calling the analyser")
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    endpoint = args.endpoint or cfg.endpoint

    conn = storage.connect(args.db)
    try:
        stored_model = storage.get_meta(conn, "model")
        items = eligible(conn, args.schema_version, args.min_summary)
    finally:
        conn.close()

    if not items:
        print("no scored items with text at schema_version={}".format(
            args.schema_version))
        return 1

    era = [item for item in items if str(item["event"].date()) >= args.since]
    if not era:
        print("nothing scored at or after {} (earliest event is {})".format(
            args.since, items[0]["event"].date()))
        return 1

    bands = stratify(era, args.strata, args.per_stratum)
    total = sum(len(chunk) for _band, chunk in bands)
    repeats = max(args.repeat, 1)

    print("era          : {} .. {}, {} scored item(s) with >= {} char summaries".format(
        args.since, era[-1]["event"].date(), len(era), args.min_summary))
    print("scored       : {} .. {} (the instrument's own timeline)".format(
        era[0]["scored"].date(), era[-1]["scored"].date()))
    print("sample       : {} item(s) in {} band(s), {} re-score(s) each = {} call(s)".format(
        total, len(bands), repeats, total * repeats))
    print("model stored : {}".format(stored_model))
    print()

    if args.dry_run:
        for band, chunk in bands:
            print("{}: {} item(s)".format(
                band_label(band, len(bands), chunk), len(chunk)))
            for item in chunk:
                print("   {:<62} {:>9}  {}".format(
                    " ".join((item["title"] or "?").split())[:62],
                    item["sentiment"] or "?", item["source"]))
        print()
        print("(dry run - nothing scored)")
        return 0

    try:
        health = api.health(endpoint)
    except api.ApiError as exc:
        print("FATAL scoring service not reachable at {}: {}".format(endpoint, exc))
        return 2
    print("model now    : {}  ({})".format(health.get("model"), endpoint))
    print()

    results: List[Dict[str, Any]] = []
    failures = 0
    for band, chunk in bands:
        for item in chunk:
            context = analyse.build_context({
                "title": item["title"],
                "summary": item["summary"],
                "published": item["published"],
            })
            runs: List[Dict[str, Any]] = []
            try:
                for _ in range(repeats):
                    result = api.score(context, schema.ANALYSIS_SCHEMA,
                                       schema.SCORE_FIELDS, endpoint=endpoint)
                    runs.append(analyse.parse_response(result))
            except api.ApiError as exc:
                failures += 1
                print("  FAILED {}: {}".format(str(item["id"])[:12], exc))
                continue
            results.append({"band": band, "item": item, "runs": runs})

    if not results:
        print("every call failed; nothing to report")
        return 2

    print("=" * 100)
    header = "{:<32} {:>4} {:>10} {:>10} {:>9} {:>7} {:>9}".format(
        "band", "n", "stored", "re-scored", "delta", "flips", "repeat")
    print(header)
    print("-" * len(header))

    def fmt(value: float, spec: str = "{:+.4f}") -> str:
        return "-" if value != value else spec.format(value)  # NaN != NaN

    def summarise(rows: Sequence[Dict[str, Any]]) -> Dict[str, float]:
        stored: List[float] = []
        fresh: List[float] = []
        deltas: List[float] = []
        noise: List[float] = []
        flips = 0
        for entry in rows:
            item = entry["item"]
            s_stored = signed_impact(item["sentiment"], item["impact_expected"])
            s_runs = [signed_impact(r["sentiment"], r["impact_expected"])
                      for r in entry["runs"]]
            s_runs = [v for v in s_runs if v is not None]
            if not s_runs:
                continue
            fresh.append(statistics.fmean(s_runs))
            if s_stored is not None:
                stored.append(s_stored)
                deltas.append(statistics.fmean(s_runs) - s_stored)
            if any(str(r["sentiment"]) != str(item["sentiment"])
                   for r in entry["runs"]):
                flips += 1
            if len(s_runs) > 1:
                noise.append(max(s_runs) - min(s_runs))
        return {
            "n": float(len(fresh)),
            "stored": statistics.fmean(stored) if stored else float("nan"),
            "fresh": statistics.fmean(fresh) if fresh else float("nan"),
            "delta": statistics.fmean(deltas) if deltas else float("nan"),
            "flips": float(flips),
            "noise": statistics.fmean(noise) if noise else float("nan"),
        }

    def row(label: str, stats: Dict[str, float]) -> str:
        return "{:<32} {:>4.0f} {:>10} {:>10} {:>9} {:>3.0f}/{:<3.0f} {:>9}".format(
            label, stats["n"], fmt(stats["stored"]), fmt(stats["fresh"]),
            fmt(stats["delta"]), stats["flips"], stats["n"],
            fmt(stats["noise"], "{:.4f}"))

    for band, chunk in bands:
        rows = [r for r in results if r["band"] == band]
        if rows:
            print(row(band_label(band, len(bands), chunk), summarise(rows)))
    print("-" * len(header))
    print(row("ALL", summarise(results)))
    if failures:
        print("{} call(s) failed and are excluded".format(failures))
    print()
    print("delta = new minus stored, in signed-impact units: negative means the same")
    print("text now scores as worse. Read it against the repeat floor - inside that,")
    print("nothing moved. A delta clearly negative in the early band and near zero in")
    print("the late band means the instrument hardened over time, so part of the")
    print("index's drift is the instrument rather than the world. A delta flat across")
    print("both bands is not instrument drift: then the movement is in the news.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())



