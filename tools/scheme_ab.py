#!/usr/bin/env python3
"""Compare "one score per story" with "one score per story per outlet".

Why
---
``storage.analysed_rows`` consumes one item per cluster, preferring the
representative. Section 11 of ``DESIGN-HISTORY.md`` measured what that costs:
27% of sampled members would flip their story's sign, and the story's single
score is one draw from a spread of framings. It also deletes the follow-on
reports - which is the one thing section 5 says carries a story's persistence.

The proposed scheme keeps the clustering and drops the collapse. A story still
counts once *per outlet that covered it*, so a story four outlets covered
contributes four reports and one outlet covering it four times still contributes
one. Multiplicity then needs no knob: being widely reported is being heavily
weighted, and a feed that writes more does not thereby weigh more.

How
---
Both schemes are built from the same stored scores, so there is no news in the
comparison - only the aggregation. Scheme A is the production index, taken from
``calculator.build_items`` unchanged. Scheme B re-picks one item per
(cluster, source) pair, using the same election rule as A one level down, and
lets each report enter at *its own* published time, so a story's weight builds
as coverage arrives.

A caveat that decides how to read this: **B changes the units of S.** Its mean
and spread are different, so the two level series are not comparable in
absolute terms, and B's level below is centred on its own mean, exactly as a
re-calibration would do. What is comparable is the shape, the S statistics, and
who holds the weight.

Reading
-------
* ``sd(S)`` and ``mean |daily change|`` are the sensitivity of the index: the
  question section 8 asked when the chart was too smooth to read.
* the per-source weight share is the risk. The corpus is six feeds and it is not
  source-invariant - dropping Guardian once moved the mood 26% - so a scheme
  that hands any one feed a much larger share needs to be seen doing it.
* the last block shows the story that prompted this, under both schemes.
* Nothing here writes to the database.

Usage::

    python tools/scheme_ab.py
    python tools/scheme_ab.py --window-days 30 --top 8
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import calculator, calibrate, config as config_mod  # noqa: E402
from rpi import paths, schema, storage  # noqa: E402

_LN2 = math.log(2.0)

# One item per (cluster, source): the cluster's representative when that source
# owns it, otherwise that source's richest text. Same election rule as
# ``rpi.dedupe._reelect_representatives``, one level down - and the same rule
# tools/score_members.py used to pick what to score, so every pair below has a
# score already.
PAIR_SQL = (
    "WITH pair_pick AS ("
    "  SELECT m.cluster_id, i.source, i.id, i.title, i.published, i.fetched_at,"
    "         a.sentiment, a.impact_expected, a.scope,"
    "         ROW_NUMBER() OVER ("
    "           PARTITION BY m.cluster_id, i.source"
    "           ORDER BY CASE WHEN i.id = c.representative THEN 0 ELSE 1 END,"
    "                    LENGTH(COALESCE(i.summary, '')) DESC,"
    "                    i.id ASC"
    "         ) AS rank"
    "  FROM cluster_members m"
    "  JOIN items i ON i.id = m.item_id"
    "  JOIN clusters c ON c.cluster_id = m.cluster_id"
    "  LEFT JOIN analyses a ON a.item_id = i.id AND a.schema_version = ?"
    ")"
    " SELECT cluster_id, source, id, title, published, fetched_at,"
    "        sentiment, impact_expected, scope"
    "   FROM pair_pick WHERE rank = 1"
    "  ORDER BY published ASC, id ASC"
)


def build_pair_items(conn: Any, cfg: Any, schema_version: int
                     ) -> List[calculator.ScoredItem]:
    """Scheme B's items: one per (cluster, source), at the report's own time.

    Pairs whose member carries no score are skipped, and counted separately by
    :func:`unscored_pairs` - a silent drop is exactly the sort of thing this
    comparison exists to catch.
    """
    items: List[calculator.ScoredItem] = []
    for row in conn.execute(PAIR_SQL, (schema_version,)):
        ts = (calculator.parse_ts(row["published"])
              or calculator.parse_ts(row["fetched_at"]))
        if ts is None or row["sentiment"] is None:
            continue
        items.append(calculator.ScoredItem(
            item_id=row["id"],
            ts=ts,
            sentiment=(row["sentiment"] or "").lower(),
            scope=row["scope"] or "",
            impact=float(row["impact_expected"] or 0.0),
            signed=calculator.signed_impact(row["sentiment"],
                                            row["impact_expected"],
                                            row["scope"], cfg),
        ))
    items.sort(key=lambda item: item.ts)
    return items


def unscored_pairs(conn: Any, schema_version: int) -> int:
    """Pairs whose member has no score yet - run tools/score_members.py first."""
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT m.cluster_id, i.source,"
        "         MAX(CASE WHEN a.item_id IS NOT NULL THEN 1 ELSE 0 END) AS scored"
        "    FROM cluster_members m"
        "    JOIN items i ON i.id = m.item_id"
        "    LEFT JOIN analyses a ON a.item_id = i.id AND a.schema_version = ?"
        "   GROUP BY m.cluster_id, i.source)"
        " WHERE scored = 0", (schema_version,)).fetchone()["n"])


def weights_of(items: Sequence[calculator.ScoredItem], at: datetime, cfg: Any
               ) -> Tuple[float, List[Tuple[float, calculator.ScoredItem]]]:
    """Decay-and-impact weight of every item live at ``at``.

    Mirrors ``calculator.decayed_mean`` exactly - same half-life, same
    ``MAX_AGE_FACTOR`` horizon, same rejection of future-stamped items, same
    impact weighting - but returns the individual weights so the caller can see
    *who* holds the index rather than only what it averages to.
    """
    horizon_hours = cfg.tau_hours * calculator.MAX_AGE_FACTOR
    out: List[Tuple[float, calculator.ScoredItem]] = []
    total = 0.0
    for item in items:
        age_hours = (at - item.ts).total_seconds() / 3600.0
        if age_hours < 0.0 or age_hours > horizon_hours:
            continue
        weight = math.exp(-_LN2 * age_hours / cfg.tau_hours)
        if cfg.weight_power:
            weight *= abs(item.signed) ** cfg.weight_power
        out.append((weight, item))
        total += weight
    out.sort(key=lambda pair: pair[0], reverse=True)
    return total, out


def source_shares(items: Sequence[calculator.ScoredItem], at: datetime,
                  cfg: Any, label_of: Dict[str, str]) -> Dict[str, float]:
    """Share of the window's weight held by each outlet."""
    total, weighted = weights_of(items, at, cfg)
    shares: Dict[str, float] = {}
    if total <= 0.0:
        return shares
    for weight, item in weighted:
        name = label_of.get(item.item_id, "?")
        shares[name] = shares.get(name, 0.0) + weight / total
    return shares


def day_change(values: Sequence[Tuple[datetime, float]]) -> float:
    """Mean absolute one-day change of a series, in the series' own units."""
    by_day: Dict[str, float] = {}
    for moment, value in values:
        by_day[moment.date().isoformat()] = value
    days = sorted(by_day)
    if len(days) < 2:
        return 0.0
    return statistics.fmean(abs(by_day[b] - by_day[a])
                            for a, b in zip(days, days[1:]))


def daily(series: Sequence[calculator.Snapshot]) -> List[Tuple[datetime, float]]:
    """Last level of each UTC day - a comparable, evenly spaced summary."""
    out: List[Tuple[datetime, float]] = []
    seen: Dict[str, Tuple[datetime, float]] = {}
    for snap in series:
        seen[snap.ts.date().isoformat()] = (snap.ts, snap.level)
    for key in sorted(seen):
        out.append(seen[key])
    return out


def calibration(series: Sequence[calculator.Snapshot], cfg: Any) -> Dict[str, Any]:
    """Re-measure ``b`` and the readiness verdict on a series' own units.

    Uses ``calibrate`` itself rather than re-deriving the arithmetic: the warm-up
    cut, the correlation time, the effective sample size and the budget the
    verdict is judged against each have exactly one definition in this project,
    and a second one here would drift from it silently.
    """
    rows = [{"ts": snap.ts.isoformat(), "item_count": snap.item_count,
             "s_value": snap.s_value} for snap in series]
    stats = calibrate.fit(rows, cfg.snapshot_minutes, cfg.config_version,
                          calibrate.WARMUP_MIN_ITEMS,
                          calibrate.level_target_se(cfg))
    first = calibrate.covered_start(rows, calibrate.WARMUP_MIN_ITEMS)
    covered = rows[first:] if first is not None else []
    stats["b"] = (statistics.fmean(row["s_value"] for row in covered)
                  if covered else float("nan"))
    return stats


def window_row(series: Sequence[calculator.Snapshot], days: float,
               now: datetime, cfg: Any) -> str:
    """``min .. max  span`` for the last ``days``, span relative to base level."""
    cutoff = now - timedelta(days=days)
    levels = [snap.level for snap in series if snap.ts >= cutoff]
    if not levels:
        return "-"
    span = (max(levels) - min(levels)) / cfg.base_level * 100.0
    return "{:.4f} .. {:.4f}   {:>6.3f}%".format(min(levels), max(levels), span)


def median_daily_move(series: Sequence[calculator.Snapshot]) -> float:
    """Median absolute one-day move of the level, in percent."""
    by_day: Dict[str, float] = {}
    for snap in series:
        by_day[snap.ts.date().isoformat()] = snap.level
    days = sorted(by_day)
    if len(days) < 2:
        return float("nan")
    moves = [abs(by_day[b] - by_day[a]) / by_day[a] * 100.0
             for a, b in zip(days, days[1:])]
    return statistics.median(moves)


def brief(text: Optional[str], width: int = 56) -> str:
    """Single-line, fixed-width-enough title for a detail row."""
    clean = " ".join((text or "?").split())
    if len(clean) > width:
        return clean[:width - 1] + "…"
    return clean


def case_study(conn: Any, pattern: str,
               schema_version: int) -> Optional[Dict[str, Any]]:
    """The cluster whose title matches ``pattern``, with every member."""
    row = conn.execute(
        "SELECT m.cluster_id FROM cluster_members m"
        "  JOIN items i ON i.id = m.item_id"
        " WHERE i.title LIKE ? LIMIT 1", ("%" + pattern + "%",)).fetchone()
    if row is None:
        return None
    cluster_id = int(row["cluster_id"])
    members = list(conn.execute(
        "SELECT i.id, i.source, i.title, i.published, i.fetched_at,"
        "       a.sentiment, a.impact_expected, a.scope, m.is_representative,"
        "       (SELECT COUNT(*) FROM cluster_members m2"
        "          JOIN items i2 ON i2.id = m2.item_id"
        "         WHERE m2.cluster_id = m.cluster_id AND i2.source = i.source)"
        "         AS same_source"
        "  FROM cluster_members m"
        "  JOIN items i ON i.id = m.item_id"
        "  LEFT JOIN analyses a ON a.item_id = i.id AND a.schema_version = ?"
        " WHERE m.cluster_id = ?"
        " ORDER BY i.published", (schema_version, cluster_id)))
    return {"cluster_id": cluster_id, "members": members}


def cluster_effect(ids: Sequence[str],
                   weighted: Dict[str, Tuple[float, calculator.ScoredItem]],
                   total: float) -> Tuple[float, float]:
    """(share of the window's weight, contribution to S) for one cluster."""
    if total <= 0.0:
        return 0.0, 0.0
    weight = sum(weighted[i][0] for i in ids if i in weighted)
    signed = sum(weighted[i][0] * weighted[i][1].signed
                 for i in ids if i in weighted)
    return weight / total, signed / total


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--schema-version", type=int, default=schema.SCHEMA_VERSION)
    parser.add_argument("--window-days", type=float, default=30.0,
                        help="days of series to compare (default %(default)s)")
    parser.add_argument("--top", type=int, default=8,
                        help="how many rows the divergence and added blocks show")
    parser.add_argument("--case", type=str, default="Pillay",
                        help="title substring for the worked example")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")  # titles may be any encoding
    except (AttributeError, ValueError):  # pragma: no cover - exotic streams
        pass

    cfg = config_mod.load()
    conn = storage.connect(args.db)
    try:
        rows = storage.analysed_rows(conn, args.schema_version)
        items_a = calculator.build_items(rows, cfg)
        items_b = build_pair_items(conn, cfg, args.schema_version)
        missing = unscored_pairs(conn, args.schema_version)
        labels = {r["id"]: r["source"] for r in
                  conn.execute("SELECT id, source FROM items")}
        titles = {r["id"]: r["title"] for r in
                  conn.execute("SELECT id, title FROM items")}
        case = case_study(conn, args.case, args.schema_version)
    finally:
        conn.close()

    if not items_a or not items_b:
        print("need both schemes' items; run rpi.analyse and tools/score_members.py")
        return 1

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.window_days)


    # B changes the units of S, so its level is centred on its own mean - what a
    # re-calibration would do. Shape comparisons survive that; absolute levels do
    # not, and are not made.
    provisional = calculator.build_series(items_b, cfg, items_b[0].ts, now)
    mean_s_b = statistics.fmean(s.s_value for s in provisional)
    cfg_b = dataclasses.replace(cfg, baseline_b=mean_s_b)
    series_a = calculator.build_series(items_a, cfg, items_a[0].ts, now)
    series_b = calculator.build_series(items_b, cfg_b, items_b[0].ts, now)
    keep_a = [s for s in series_a if s.ts >= cutoff]
    keep_b = [s for s in series_b if s.ts >= cutoff]

    total_a, weighted_a = weights_of(items_a, now, cfg)
    total_b, weighted_b = weights_of(items_b, now, cfg)

    # --- Section A ---------------------------------------------------------
    print("A. what each scheme feeds the index")
    print("   A: one score per story - the representative, stamped at the cluster's")
    print("      first sighting")
    print("   B: one score per story per outlet, each stamped when it was published")
    print()
    print("   {:<34} {:>10} {:>10}".format("", "A", "B"))
    print("   " + "-" * 56)
    print("   {:<34} {:>10} {:>10}".format("scored items", len(items_a), len(items_b)))
    print("   {:<34} {:>10} {:>10}".format("live in the window", len(weighted_a),
                                           len(weighted_b)))
    print("   {:<34} {:>10} {:>10}".format("(story, source) pairs still unscored",
                                           missing, missing))
    if missing:
        print("   unscored pairs are absent from B; run tools/score_members.py")

    # The weights have to be the ones the index actually uses, or everything
    # below is arithmetic about a different index.
    times_a = [item.ts for item in items_a]
    check = (sum(w * i.signed for w, i in weighted_a) / total_a) if total_a else 0.0
    expect = calculator.decayed_mean(items_a, times_a, now, cfg.tau_hours,
                                     cfg.baseline_b, cfg.weight_power)
    print()
    print("   self-check: weights reproduce calculator.decayed_mean -> {}".format(
        "yes" if abs(check - expect) < 1e-9
        else "NO ({:.9f} vs {:.9f})".format(check, expect)))

    shares_a = source_shares(items_a, now, cfg, labels)
    shares_b = source_shares(items_b, now, cfg, labels)
    print()
    print("   {:<16} {:>10} {:>10}   {:>10}".format("outlet", "A weight", "B weight",
                                                    "shift"))
    print("   " + "-" * 52)
    for name in sorted(set(shares_a) | set(shares_b),
                       key=lambda n: -shares_b.get(n, 0.0)):
        a, b = shares_a.get(name, 0.0), shares_b.get(name, 0.0)
        print("   {:<16} {:>9.1%} {:>9.1%}   {:>+9.1%}".format(name, a, b, b - a))
    print()


    # --- Section B ---------------------------------------------------------
    print("B. does the reading change?")
    sa = [s.s_value for s in keep_a]
    sb = [s.s_value for s in keep_b]
    print("   {:<36} {:>10} {:>10}".format("", "A", "B"))
    print("   " + "-" * 58)
    print("   {:<36} {:>10.4f} {:>10.4f}".format(
        "mean S", statistics.fmean(sa), statistics.fmean(sb)))
    print("   {:<36} {:>10.4f} {:>10.4f}".format(
        "sd(S)", statistics.pstdev(sa), statistics.pstdev(sb)))

    da, db = daily(keep_a), daily(keep_b)
    la = {moment.date().isoformat(): value for moment, value in da}
    lb = {moment.date().isoformat(): value for moment, value in db}
    keys = sorted(set(la) & set(lb))
    if len(keys) < 3:
        print("   too few overlapping days to compare the series")
        return 1
    mean_la = statistics.fmean(la[k] for k in keys)
    mean_lb = statistics.fmean(lb[k] for k in keys)
    print("   {:<36} {:>9.2f}% {:>9.2f}%".format(
        "mean |1-day move| (of mean level)",
        day_change(da) / mean_la * 100.0, day_change(db) / mean_lb * 100.0))
    print("   {:<36} {:>21.3f}".format(
        "correlation of the daily series",
        statistics.correlation([la[k] for k in keys], [lb[k] for k in keys])))
    print("   {} overlapping day(s): {} .. {}".format(len(keys), keys[0], keys[-1]))

    moves = []
    for previous, current in zip(keys, keys[1:]):
        move_a = (la[current] - la[previous]) / la[previous] * 100.0
        move_b = (lb[current] - lb[previous]) / lb[previous] * 100.0
        moves.append((current, move_a, move_b, move_b - move_a))
    moves.sort(key=lambda row: -abs(row[3]))
    print()
    print("   biggest disagreements, by the day's move")
    for current, move_a, move_b, apart in moves[:max(args.top, 0)]:
        print("     {}   A {:>+6.2f}%   B {:>+6.2f}%   apart {:>5.2f} pt".format(
            current, move_a, move_b, apart))

    ids_a = {item.item_id for item in items_a}
    added = [(weight, item) for weight, item in weighted_b
             if item.item_id not in ids_a]
    if added:
        print()
        print("   reports B counts that A drops entirely (top by weight now)")
        for weight, item in added[:max(args.top, 0)]:
            print("     {:>5.1%}  {:>+6.2f}  {:<14} {}".format(
                weight / total_b if total_b else 0.0, item.signed,
                labels.get(item.item_id, "?"), brief(titles.get(item.item_id))))
    print()


    # --- Section C ---------------------------------------------------------
    print("C. the calibration, re-measured on B's own units")
    print("   b is the mean S over the covered window, so it has to be re-measured")
    print("   whenever the units change. c is a readability choice and is kept as it")
    print("   is - see section 8 of DESIGN-HISTORY.md - so B's wider S shows up as a")
    print("   larger swing in the level, which is the direction that section wanted.")
    print()
    cal_a = calibration(series_a, cfg)
    cal_b = calibration(series_b, cfg)
    print("   {:<36} {:>12} {:>12}".format("", "A", "B"))
    print("   " + "-" * 62)
    print("   {:<36} {:>12.4f} {:>12.4f}".format(
        "b (A: config, B: measured)", cfg.baseline_b, cal_b["b"]))
    print("   {:<36} {:>12.4f} {:>12.4f}".format(
        "mean S of the covered window", cal_a["b"], cal_b["b"]))
    print("   {:<36} {:>12.4f} {:>12.4f}".format(
        "sd(S)", cal_a["sd"], cal_b["sd"]))
    print("   {:<36} {:>12.2f} {:>12.2f}".format(
        "tau_c (days)", cal_a["tau_c_days"], cal_b["tau_c_days"]))
    print("   {:<36} {:>12.4f} {:>12.4f}".format(
        "se", cal_a["se"], cal_b["se"]))
    print("   {:<36} {:>12.4f} {:>12.4f}".format(
        "target se (level budget / c)", cal_a["target_se"], cal_b["target_se"]))
    print("   {:<36} {:>12} {:>12}".format(
        "ready", str(cal_a["ready"]), str(cal_b["ready"])))
    print("   {:<36} {:>12} {:>12}".format(
        "dropped warm-up",
        "{} snap / {:.1f} d".format(int(cal_a["dropped_snapshots"]),
                                    cal_a["dropped_days"]),
        "{} snap / {:.1f} d".format(int(cal_b["dropped_snapshots"]),
                                    cal_b["dropped_days"])))
    print("   {:<36} {:>12.1f} {:>12.1f}".format(
        "covered span (days)", cal_a["days"], cal_b["days"]))
    print()
    print("   the published series by window - section 8's table, for both schemes")
    print("   {:<10} {:<34} {:<34}".format("window", "A", "B"))
    for label, days in (("today", 1.0), ("1 week", 7.0), ("1 month", 30.0)):
        print("   {:<10} {:<34} {:<34}".format(
            label, window_row(series_a, days, now, cfg),
            window_row(series_b, days, now, cfg)))
    print("   {:<10} {:>33.3f}% {:>33.3f}%".format(
        "median/day", median_daily_move(keep_a), median_daily_move(keep_b)))
    print()

    # --- Section D ---------------------------------------------------------
    if case is None:
        print("D. worked example: no story matched --case {!r}".format(args.case))
        return 0

    ids = [member["id"] for member in case["members"]]
    by_id_a = {item.item_id: (weight, item) for weight, item in weighted_a}
    by_id_b = {item.item_id: (weight, item) for weight, item in weighted_b}
    share_a, contrib_a = cluster_effect(ids, by_id_a, total_a)
    share_b, contrib_b = cluster_effect(ids, by_id_b, total_b)

    print("D. worked example: cluster {} holds {} report(s)".format(
        case["cluster_id"], len(case["members"])))
    print("   {:<4}{:<15}{:<18}{:<10}{:<15}{:>7}   {}".format(
        "", "outlet", "published", "sentiment", "scope", "signed",
        "x from that source"))
    for member in case["members"]:
        sign = {"positive": 1.0, "negative": -1.0}.get(
            (member["sentiment"] or "").lower(), 0.0)
        signed = (sign * float(member["impact_expected"] or 0.0)
                  * cfg.scope_weight(member["scope"] or ""))
        print("   {:<4}{:<15}{:<18}{:<10}{:<15}{:>+7.2f}   x{}".format(
            "REP" if member["is_representative"] else "",
            brief(member["source"], 14), str(member["published"])[:16],
            member["sentiment"] or "-", member["scope"] or "-", signed,
            member["same_source"]))
    print()
    print("   {:<34} {:>10} {:>10}".format("", "A", "B"))
    print("   " + "-" * 56)
    print("   {:<34} {:>9.2%} {:>9.2%}".format(
        "share of the window's weight", share_a, share_b))
    print("   {:<34} {:>+10.5f} {:>+10.5f}".format(
        "contribution to S", contrib_a, contrib_b))
    print("   {:<34} {:>10.5f} {:>10.5f}".format(
        "level multiplier exp(c x S)", math.exp(cfg.c * contrib_a),
        math.exp(cfg.c * contrib_b)))
    print("   {:<34} {:>10} {:>10}".format(
        "reports counted from this story", len([i for i in ids if i in by_id_a]),
        len([i for i in ids if i in by_id_b])))
    print()
    print("The level multiplier is the whole argument in one number: below 1.0 is")
    print("the index calling the story bad news, above 1.0 good news. A takes one")
    print("report from the cluster, so the story's sign is whatever the election")
    print("picked; B takes one per outlet, so no single report can decide it, and a")
    print("widely covered story carries the weight its coverage earns.")
    print()
    print("Nothing here is written to the database. Adopting B means a new")
    print("config_version, a re-measured b on B's units, and a re-scored corpus for")
    print("any pair still unscored - all separate, deliberate steps.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
