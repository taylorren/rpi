#!/usr/bin/env python3
"""Audit what the duplicate clustering did to the index's view of an event.

Why
---
The index consumes **one item per cluster**, preferring the cluster's
representative (``storage.analysed_rows``). So a cluster's contribution is not
"what the reports said" but "what one report said, stamped at the cluster's
first sighting". When the clustering is right that is the intent: a story enters
the series when it broke, scored by its fullest telling.

It is wrong when the cluster is coarser than the event. The live corpus has a
worked example - a report of Navi Pillay winning the Nobel Peace Prize scored
-6.66 - and two separate mistakes produced it:

* the representative was NYT's report, whose RSS summary is a hook about a
  *different* fact (a UN commission's genocide finding, not the prize), so the
  model scored the text faithfully and the text was about Gaza; and
* a second cluster had glued the award to the previous day's "Trump says he
  deserves the Nobel Peace Prize", where the award's own report (+5.93) was
  discarded, because only the representative counts.

So a cluster can span several *different* events, and one event can sit in
several clusters. Neither is visible from the index.

The blind spot this exists to close
-----------------------------------
The instrument cannot see this for itself. A duplicate member is never
analysed, so 3,124 of the live corpus's 3,131 scored clusters have exactly one
scored member: the representative's score *is* the cluster's score, by
construction. The two clusters where scored members disagree are the ones that
happened to be scored as representatives before a later rebuild merged them, so
the observed rate is a lower bound, not an estimate.

This closes that gap the only way it can be closed - by scoring a sample of
non-representative members and comparing them with their representatives. It
writes nothing: no analysis is stored, no cluster is touched. It cannot change
the index it is measuring.

Reading the output
------------------
* Section A is the shape of the clustering: how many clusters hold more than
  one member, how long they span, and how far the representative trails the
  cluster's first sighting.
* Section B lists the worst offenders by drift, plus every cluster where two
  *already scored* members disagree in sign. Those cost nothing - the scores
  are already stored.
* Section C is the estimate: one non-representative member per sampled cluster,
  scored and compared with its representative, plus the reference it has to be
  read against - how often two arbitrary *same-day* stories agree in sign. Both
  come from the same corpus, so the gap between them is what the clustering
  buys, and the gap to 100% is what one score per cluster costs.
* ``agree`` is the share of sampled members whose sign matches the
  representative's. ``delta`` is member minus representative in signed-impact
  units, so a positive delta means the index would have read the story as
  *better* had that member been elected.

What it measured on the live corpus (2026-10-09, 60 calls)
----------------------------------------------------------
``agree`` is **73%** against a same-day reference of **49%**: the clustering is
carrying real signal, and members of a cluster really are more alike than two
stories drawn from the same day. But the rate is **flat across the drift bands**
(67 / 80 / 73 / 73%), which falsifies the guess that span is what drives the
loss. The price of one score per cluster is paid everywhere, not only by the
long clusters: a cluster holds a spread of framings of the same event, and the
index takes one draw from that spread.

An earlier 24-call run showed a clean 100 / 100 / 83 / 67 gradient. It was
sampling noise - the trap ``DESIGN-HISTORY.md`` already records as "sample at
100, not 25".

Usage::

    python tools/cluster_audit.py --dry-run
    python tools/cluster_audit.py --per-band 6 --bands 4
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import analyse, api, calculator, config as config_mod  # noqa: E402
from rpi import paths, schema, storage  # noqa: E402

# Titles are single-line but nothing stops a feed from shipping a wall of text;
# truncate so the tables keep their shape.
TITLE_WIDTH = 64


def brief(text: Optional[str], width: int = TITLE_WIDTH) -> str:
    """Single-line, fixed-width-enough title for a detail row."""
    clean = " ".join((text or "?").split())
    if len(clean) > width:
        return clean[:width - 1] + "…"
    return clean


def hours_between(start: Optional[datetime],
                  end: Optional[datetime]) -> Optional[float]:
    """Hours from ``start`` to ``end``, or None if either is missing."""
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 3600.0


def signed(sentiment: Optional[str], impact: Optional[float],
           scope: Optional[str], cfg: Any) -> Optional[float]:
    """Scope-weighted signed impact - the quantity the index consumes.

    ``calculator.signed_impact`` returns 0.0 for a missing impact, which is
    indistinguishable from a neutral story. Here the difference matters, so an
    unscored member is None and never counts as agreement.
    """
    if sentiment is None or impact is None:
        return None
    if str(sentiment).strip().lower() not in calculator.POLARITY:
        return None
    return calculator.signed_impact(sentiment, impact, scope, cfg)


def sign_of(value: Optional[float]) -> int:
    """-1, 0 or +1 for a signed impact; a neutral story is exactly 0.0."""
    if value is None:
        return 0
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def load_clusters(conn: Any, schema_version: int) -> List[Dict[str, Any]]:
    """Every cluster with its members and the representative's stored score.

    One query, grouped in Python: a cluster is a handful of rows, so a second
    pass per cluster would only add round trips. Members come back in publish
    order, which is the order they are worth reading in.
    """
    rows = conn.execute(
        "SELECT c.cluster_id, c.representative, c.first_published,"
        "       m.item_id, m.is_representative, i.source, i.title, i.summary,"
        "       i.published, i.fetched_at,"
        "       a.sentiment, a.impact_expected, a.scope, a.analyzed_at"
        "  FROM clusters c"
        "  JOIN cluster_members m ON m.cluster_id = c.cluster_id"
        "  JOIN items i ON i.id = m.item_id"
        "  LEFT JOIN analyses a ON a.item_id = i.id AND a.schema_version = ?"
        " ORDER BY c.cluster_id, i.published",
        (schema_version,)).fetchall()

    clusters: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        cluster_id = int(row["cluster_id"])
        entry = clusters.get(cluster_id)
        if entry is None:
            entry = {
                "cluster_id": cluster_id,
                "representative": row["representative"],
                "first_published": calculator.parse_ts(row["first_published"]),
                "members": [],
            }
            clusters[cluster_id] = entry
        event = (calculator.parse_ts(row["published"])
                 or calculator.parse_ts(row["fetched_at"]))
        entry["members"].append({
            "id": row["item_id"],
            "source": row["source"],
            "title": row["title"],
            "summary": row["summary"] or "",
            "event": event,
            "is_rep": bool(row["is_representative"]),
            "sentiment": row["sentiment"],
            "impact": row["impact_expected"],
            "scope": row["scope"],
            "analyzed_at": row["analyzed_at"],
            "signed": None,
        })
    return list(clusters.values())


def finalise(clusters: Sequence[Dict[str, Any]], cfg: Any) -> None:
    """Fill in per-cluster timings, the representative, and its signed score.

    ``first_published`` is the event time the index uses, so ``drift`` - how far
    the representative is published after it - is the age gap between the story
    the index stamps and the report it is actually scored by.
    """
    for entry in clusters:
        members = entry["members"]
        rep = next((m for m in members if m["is_rep"]), None)
        if rep is None:  # a rebuild may have dropped the flag but kept the id
            rep = next((m for m in members
                        if m["id"] == entry["representative"]), None)
        entry["rep"] = rep
        for member in members:
            member["signed"] = signed(member["sentiment"], member["impact"],
                                      member["scope"], cfg)

        moments = [m["event"] for m in members if m["event"] is not None]
        entry["first_seen"] = min(moments) if moments else None
        entry["last_seen"] = max(moments) if moments else None
        entry["span_hours"] = hours_between(entry["first_seen"],
                                            entry["last_seen"])
        anchor = entry["first_published"] or entry["first_seen"]
        entry["drift_hours"] = hours_between(anchor,
                                             rep["event"] if rep else None)
        entry["rep_signed"] = rep["signed"] if rep else None


def scored_members(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Members that already carry a stored score - free to compare."""
    return [m for m in entry["members"] if m["signed"] is not None]


def sign_clashes(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Scored members whose sign differs from the representative's.

    Only ever non-empty for clusters whose members were scored as
    representatives before a later rebuild merged them, which is why the count
    is a floor and not a rate.
    """
    rep = entry.get("rep")
    if rep is None or entry.get("rep_signed") is None:
        return []
    want = sign_of(entry["rep_signed"])
    return [m for m in scored_members(entry)
            if m is not rep and sign_of(m["signed"]) != want]


def sample_population(clusters: Sequence[Dict[str, Any]], min_summary: int
                      ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(cluster, member) pairs worth one scoring call each.

    A member qualifies when the cluster has a stored representative score to
    compare against, the member has no stored score of its own, and its text is
    long enough to be the same kind of measurement the representative got.
    """
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for entry in clusters:
        rep = entry.get("rep")
        if rep is None or entry.get("rep_signed") is None:
            continue
        if len(entry["members"]) < 2:
            continue
        for member in entry["members"]:
            if member is rep or member["signed"] is not None:
                continue
            if len(member["summary"]) < min_summary:
                continue
            pairs.append((entry, member))
    return pairs


def stratify(pairs: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
             bands: int, per_band: int
             ) -> List[Tuple[int, List[Tuple[Dict[str, Any], Dict[str, Any]]]]]:
    """Evenly spaced picks from equal-count drift bands - deterministic, no RNG.

    Even spacing rather than random sampling so two runs on the same database
    are comparable, which matters when the answer is a share of a small sample.
    """
    ordered = sorted(pairs, key=lambda pair: pair[0].get("drift_hours") or 0.0)
    out: List[Tuple[int, List[Tuple[Dict[str, Any], Dict[str, Any]]]]] = []
    size = len(ordered) / float(max(bands, 1))
    for band in range(bands):
        lo = int(round(band * size))
        hi = int(round((band + 1) * size))
        chunk = list(ordered[lo:hi])
        if not chunk:
            continue
        step = max(len(chunk) // max(per_band, 1), 1)
        out.append((band, chunk[::step][:per_band]))
    return out


def band_label(band: int, chunk: Sequence[Tuple[Dict[str, Any], Any]]) -> str:
    """Human name for a drift band, with the drift range it covers."""
    drifts = [pair[0].get("drift_hours") or 0.0 for pair in chunk]
    return "band {} ({:.0f}-{:.0f}h drift)".format(
        band + 1, min(drifts), max(drifts))


def null_agreement(clusters: Sequence[Dict[str, Any]]) -> Tuple[float, int]:
    """Sign agreement between two arbitrary *same-day* stories.

    The reference the cluster figure has to be read against, and the reason
    ``agree`` cannot be read against 100%. Two stories drawn from one day are
    the natural null: they share the day's mood and the day's feed mix, but not
    the event. A cluster figure well above this is the clustering working; the
    remaining gap is what summarising a cluster with one score costs.

    Stored scores only, paired in time order, so it costs nothing and is
    deterministic.
    """
    by_day: Dict[str, List[float]] = {}
    for entry in clusters:
        for member in entry["members"]:
            if member["signed"] is None or member["event"] is None:
                continue
            by_day.setdefault(member["event"].date().isoformat(),
                              []).append(member["signed"])
    pairs = 0
    agree = 0
    for values in by_day.values():
        for index in range(0, len(values) - 1, 2):
            pairs += 1
            if sign_of(values[index]) == sign_of(values[index + 1]):
                agree += 1
    if not pairs:
        return float("nan"), 0
    return agree / float(pairs), pairs


def stamp(moment: Optional[datetime]) -> str:
    """Short UTC stamp for a table cell."""
    return moment.strftime("%m-%d %H:%M") if moment else "-"


def fmt_signed(value: Optional[float], spec: str = "{:+.2f}") -> str:
    """A signed number, or a dash when the story was never scored."""
    return "-" if value is None else spec.format(value)


def describe(values: Sequence[float], unit: str = "") -> str:
    """median/p90 and max, in the house style, or a dash when empty."""
    if not values:
        return "-"
    ordered = sorted(values)
    median = statistics.median(ordered)
    p90 = ordered[min(int(round(0.9 * (len(ordered) - 1))), len(ordered) - 1)]
    return "{:.1f}/{:.1f}{} (median/p90), max {:.1f}{}".format(
        median, p90, unit, ordered[-1], unit)


def histogram(values: Sequence[float], edges: Sequence[float]) -> str:
    """Counts either side of fixed edges, so the shape is visible at a glance."""
    counts = [0] * (len(edges) + 1)
    for value in values:
        placed = False
        for index, edge in enumerate(edges):
            if value < edge:
                counts[index] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    labels = ["<{}".format(edges[0])]
    for lo, hi in zip(edges, edges[1:]):
        labels.append("{}-{}".format(lo, hi))
    labels.append(">{}".format(edges[-1]))
    return " | ".join("{} {}".format(label, count)
                      for label, count in zip(labels, counts))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--schema-version", type=int, default=schema.SCHEMA_VERSION)
    parser.add_argument("--span-hours", type=float, default=24.0,
                        help="flag clusters whose members span longer than this")
    parser.add_argument("--top", type=int, default=8,
                        help="clusters listed in the worst-offenders block")
    parser.add_argument("--members", type=int, default=3,
                        help="extra members shown per listed cluster")
    parser.add_argument("--bands", type=int, default=4,
                        help="drift bands to split the sample into")
    parser.add_argument("--per-band", type=int, default=6,
                        help="members to score from each band (default %(default)s)")
    parser.add_argument("--min-summary", type=int, default=100,
                        help="shortest usable summary, in characters")
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="show the sample without calling the analyser")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")  # titles may be any encoding
    except (AttributeError, ValueError):  # pragma: no cover - exotic streams
        pass

    cfg = config_mod.load()
    endpoint = args.endpoint or cfg.endpoint

    conn = storage.connect(args.db)
    try:
        clusters = load_clusters(conn, args.schema_version)
    finally:
        conn.close()

    if not clusters:
        print("no clusters; run the pipeline first")
        return 1
    finalise(clusters, cfg)

    multi = [c for c in clusters if len(c["members"]) > 1]
    members_total = sum(len(c["members"]) for c in clusters)
    drifts = [c["drift_hours"] for c in multi if c["drift_hours"] is not None]
    spans = [c["span_hours"] for c in multi if c["span_hours"] is not None]

    # --- Section A ---------------------------------------------------------
    print("A. the shape of the clustering")
    print("   clusters      : {} total, {} with more than one member, {} members".format(
        len(clusters), len(multi), members_total))
    print("   cluster size  : {}".format(
        describe([float(len(c["members"])) for c in multi])))
    print("   member span   : {}  (first to last sighting, h)".format(
        describe(spans, "h")))
    print("   rep drift     : {}  (first sighting to representative, h)".format(
        describe(drifts, "h")))
    print("   drift bands   : {}".format(
        histogram(drifts, (6.0, 24.0, 72.0))))
    over_span = [c for c in multi
                 if (c["span_hours"] or 0.0) > args.span_hours]
    over_drift = [c for c in multi
                  if (c["drift_hours"] or 0.0) > args.span_hours]
    print("   over {:.0f}h span : {} of {} multi-member clusters".format(
        args.span_hours, len(over_span), len(multi)))
    print("   over {:.0f}h drift: {} of {} (the score is that much newer than "
          "the story it is stamped on)".format(
              args.span_hours, len(over_drift), len(multi)))
    print()

    # --- Section B ---------------------------------------------------------
    print("B. worst offenders by drift")
    print("   the index stamps the representative's score at the cluster's first"
          " sighting, so drift is how much newer the score is than the story")
    header = "{:>8}  {:>7}  {:>8}  {:>8}  {:<14} {:>7}  {}".format(
        "cluster", "members", "span", "drift", "rep", "score", "first sighting")
    print("   " + header)
    print("   " + "-" * len(header))
    listed = sorted(multi, key=lambda c: -(c["drift_hours"] or 0.0))
    for entry in listed[:max(args.top, 0)]:
        rep = entry["rep"]
        anchor = entry["first_published"] or entry["first_seen"]
        print("   {:>8}  {:>7}  {:>7.1f}h  {:>7.1f}h  {:<14} {:>7}  {}".format(
            entry["cluster_id"], len(entry["members"]),
            entry["span_hours"] or 0.0, entry["drift_hours"] or 0.0,
            brief(rep["source"] if rep else "?", 14),
            fmt_signed(entry["rep_signed"]), stamp(anchor)))
        others = [m for m in entry["members"] if m is not rep]
        for member in others[:max(args.members, 0)]:
            print("       + {:<14} {}  {:>7}  {}".format(
                brief(member["source"], 14), stamp(member["event"]),
                fmt_signed(member["signed"]), brief(member["title"], 54)))
    print()

    clashes = [c for c in multi if sign_clashes(c)]
    print("   clusters whose already-scored members disagree in sign: {}".format(
        len(clashes)))
    for entry in clashes:
        rep = entry["rep"]
        print("     cluster {} ({} members, drift {}h): rep {} {} vs".format(
            entry["cluster_id"], len(entry["members"]),
            fmt_signed(entry["drift_hours"], "{:.1f}"),
            brief(rep["source"] if rep else "?", 14),
            fmt_signed(entry["rep_signed"])))
        for member in sign_clashes(entry):
            print("       {} {} by {}".format(
                brief(member["source"], 14), fmt_signed(member["signed"]),
                brief(member["title"], 54)))
    print("   (these cost nothing to find - both scores were already stored - but")
    print("    they are a floor, not a rate: a duplicate member is never analysed)")
    print()

    # --- Section C ---------------------------------------------------------
    print("C. the blind spot: scoring non-representative members")
    pairs = sample_population(clusters, args.min_summary)
    bands = stratify(pairs, args.bands, args.per_band)
    planned = sum(len(chunk) for _band, chunk in bands)
    print("   population    : {} unscored member(s) of clusters whose"
          " representative is scored".format(len(pairs)))
    print("   sample        : {} member(s) in {} drift band(s) = {} call(s)".format(
        planned, len(bands), planned))
    reference, ref_pairs = null_agreement(clusters)
    print("   reference     : two arbitrary same-day stories agree {} of the time"
          " ({} stored pairs)".format(
              "-" if reference != reference else "{:.0%}".format(reference),
              ref_pairs))
    print()

    if not planned:
        print("   nothing to sample: every member of a scored cluster is already"
              " scored, or its text is too short to be the same measurement")
        return 0

    if args.dry_run:
        for band, chunk in bands:
            print("   {}: {} member(s)".format(band_label(band, chunk), len(chunk)))
            for entry, member in chunk:
                print("     cluster {:<7} drift {:>6.1f}h  rep {:>6}  {:<14} {}".format(
                    entry["cluster_id"], entry["drift_hours"] or 0.0,
                    fmt_signed(entry["rep_signed"]), brief(member["source"], 14),
                    brief(member["title"], 54)))
        print()
        print("   (dry run - nothing scored, nothing stored)")
        return 0

    try:
        health = api.health(endpoint)
    except api.ApiError as exc:
        print("FATAL scoring service not reachable at {}: {}".format(endpoint, exc))
        return 2
    print("   model         : {}  ({})".format(health.get("model"), endpoint))
    print()

    header = "{:<24} {:>3} {:>7} {:>10} {:>10} {:>9} {:>9} {:>6}".format(
        "band", "n", "agree", "rep mean", "mem mean", "delta", "|delta|", "flips")
    print("   " + header)
    print("   " + "-" * len(header))

    def summarise(rows: Sequence[Tuple[int, Dict[str, Any], Dict[str, Any], float]]
                  ) -> Dict[str, float]:
        reps: List[float] = []
        mems: List[float] = []
        deltas: List[float] = []
        agree = flips = 0
        for _band, entry, _member, fresh in rows:
            rep_signed = float(entry["rep_signed"])
            reps.append(rep_signed)
            mems.append(fresh)
            deltas.append(fresh - rep_signed)
            if sign_of(fresh) == sign_of(rep_signed):
                agree += 1
            else:
                flips += 1
        count = len(rows)
        return {
            "n": float(count),
            "agree": (agree / count) if count else float("nan"),
            "rep": statistics.fmean(reps) if reps else float("nan"),
            "mem": statistics.fmean(mems) if mems else float("nan"),
            "delta": statistics.fmean(deltas) if deltas else float("nan"),
            "spread": (statistics.fmean([abs(d) for d in deltas]) if deltas
                       else float("nan")),
            "flips": float(flips),
        }

    def row(label: str, stats: Dict[str, float]) -> str:
        share = ("-" if stats["agree"] != stats["agree"]
                 else "{:.0%}".format(stats["agree"]))
        spread = ("-" if stats["spread"] != stats["spread"]
                  else "{:.2f}".format(stats["spread"]))
        return "   {:<24} {:>3.0f} {:>7} {:>10} {:>10} {:>9} {:>9} {:>6.0f}".format(
            label, stats["n"], share, fmt_signed(stats["rep"]),
            fmt_signed(stats["mem"]), fmt_signed(stats["delta"]), spread,
            stats["flips"])

    results: List[Tuple[int, Dict[str, Any], Dict[str, Any], float]] = []
    failures = 0
    for band, chunk in bands:
        for entry, member in chunk:
            context = analyse.build_context({
                "title": member["title"],
                "summary": member["summary"],
                "published": (member["event"].isoformat().replace("+00:00", "Z")
                              if member["event"] else None),
            })
            try:
                result = api.score(context, schema.ANALYSIS_SCHEMA,
                                   schema.SCORE_FIELDS, endpoint=endpoint)
            except api.ApiError as exc:
                failures += 1
                print("   FAILED cluster {} {}: {}".format(
                    entry["cluster_id"], brief(member["source"], 14), exc))
                continue
            parsed = analyse.parse_response(result)
            fresh = signed(parsed.get("sentiment"), parsed.get("impact_expected"),
                           parsed.get("scope"), cfg)
            if fresh is None:
                failures += 1
                continue
            results.append((band, entry, member, fresh))

    if not results:
        print("   every call failed; nothing to report")
        return 2

    for band, chunk in bands:
        rows = [r for r in results if r[0] == band]
        if rows:
            print(row(band_label(band, chunk), summarise(rows)))
    print("   " + "-" * len(header))
    print(row("ALL", summarise(results)))
    if failures:
        print("   {} call(s) failed and are excluded".format(failures))
    print()
    print("agree  = share of sampled members whose sign matches the representative's.")
    print("delta  = member minus representative in signed-impact units: positive means")
    print("         the index would have read the story as better had that member been")
    print("         elected; |delta| is the size of the misread, ignoring direction.")
    print("flips  = members that would have reversed the story's sign.")
    print()
    print("Read agree against the reference, not against 100%: that is how often two")
    print("arbitrary same-day stories agree, so the gap between them is what the")
    print("clustering buys, and the gap to 100% is what one score per cluster costs.")
    print("A rate that falls with drift points at clusters spanning several events,")
    print("and the fix belongs in rpi.dedupe, which asks the model for 'same")
    print("real-world event' and is answered generously. A flat rate says the cost is")
    print("intrinsic to summarising a cluster with one draw, not a matter of span.")
    print("Nothing here is written to the database: these scores exist only for this")
    print("report.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
