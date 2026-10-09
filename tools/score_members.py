#!/usr/bin/env python3
"""Score the per-source reports the index currently ignores.

Why
---
``storage.analysed_rows`` consumes one item per cluster, preferring the
representative, so a story covered by six outlets contributes one report's
score and the other five are never analysed at all. That was a deliberate
trade - one score per story, no double counting - and section 11 of
``DESIGN-HISTORY.md`` records what it costs: 27% of sampled members would flip
their story's sign, and the cluster's single score is one draw from a spread.

This scores the *inputs* of the alternative, not the alternative. For every
(cluster, source) pair it analyses the pair's richest-text member - the same
rule ``rpi.dedupe`` uses to elect a cluster representative, applied one level
down - so that "the story, as this outlet told it" has a score of its own.
Multiplicity then falls out of the arithmetic rather than needing a knob: a
story four outlets covered contributes four reports, while one outlet covering
it four times still contributes one, so a feed that writes more does not
thereby weigh more.

What it writes, and what it cannot move
---------------------------------------
It writes ``analyses`` rows for **non-representative** members, and nothing
else. That cannot move the index: all 3,131 cluster representatives already
have a score, ``analysed_rows`` prefers the representative by rank, so the new
rows are ignored until something reads them. ``pending_items`` and both backlog
gauges are scoped to representatives, so the health checks do not move either.
The one visible change is the total in the ``analyses`` table.

Idempotent and resumable: a pair is skipped once its member is scored, so an
interrupted run continues where it stopped and re-running it is free.

Usage::

    python tools/score_members.py --dry-run
    python tools/score_members.py --limit 250
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import analyse, api, calculator, config as config_mod  # noqa: E402
from rpi import paths, schema, storage  # noqa: E402

# The same election ``rpi.dedupe._reelect_representatives`` uses, one level
# down: richest text wins, ties broken by id so the choice is deterministic.
# Keeping the two in step matters - a pair's member should be picked the way a
# cluster's is, or the comparison measures the picker rather than the scheme.
PAIR_MEMBERS_SQL = (
    "WITH pair_pick AS ("
    "  SELECT m.cluster_id, i.source, i.id, i.title, i.summary,"
    "         i.published, i.fetched_at,"
    "         ROW_NUMBER() OVER ("
    "           PARTITION BY m.cluster_id, i.source"
    "           ORDER BY CASE WHEN i.id = c.representative THEN 0 ELSE 1 END,"
    "                    LENGTH(COALESCE(i.summary, '')) DESC,"
    "                    i.id ASC"
    "         ) AS rank"
    "  FROM cluster_members m"
    "  JOIN items i ON i.id = m.item_id"
    "  JOIN clusters c ON c.cluster_id = m.cluster_id"
    ")"
    " SELECT p.cluster_id, p.id, p.source, p.title, p.summary, p.published,"
    "        p.fetched_at"
    "   FROM pair_pick p"
    "   LEFT JOIN analyses a ON a.item_id = p.id AND a.schema_version = ?"
    "  WHERE p.rank = 1 AND a.item_id IS NULL"
    "  ORDER BY p.published ASC, p.id ASC"
)


def pending_pairs(conn: Any, schema_version: int,
                  limit: Optional[int] = None) -> List[Any]:
    """One row per (cluster, source) pair still missing its member's score."""
    sql = PAIR_MEMBERS_SQL + (" LIMIT ?" if limit else "")
    params: Sequence[Any] = ((schema_version, limit) if limit
                             else (schema_version,))
    return list(conn.execute(sql, params))


def pair_count(conn: Any) -> int:
    """How many (cluster, source) pairs the corpus holds in total."""
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT m.cluster_id, i.source FROM cluster_members m"
        "  JOIN items i ON i.id = m.item_id"
        "  GROUP BY m.cluster_id, i.source)").fetchone()["n"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--schema-version", type=int, default=schema.SCHEMA_VERSION)
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="score at most this many pairs, then stop")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be scored, without calling the analyser")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")  # titles may be any encoding
    except (AttributeError, ValueError):  # pragma: no cover - exotic streams
        pass

    cfg = config_mod.load()
    endpoint = args.endpoint or cfg.endpoint

    conn = storage.connect(args.db)
    try:
        pairs = pair_count(conn)
        remaining = pending_pairs(conn, args.schema_version)
        stored_model = storage.get_meta(conn, "model")
    finally:
        conn.close()

    todo = remaining[:args.limit] if args.limit else remaining
    print("corpus        : {} (story x source) pair(s)".format(pairs))
    print("already scored: {}".format(pairs - len(remaining)))
    print("pending       : {}".format(len(remaining)))
    if args.limit:
        print("this run      : {} (--limit)".format(len(todo)))
    print("model stored  : {}".format(stored_model))
    print()

    if not todo:
        print("nothing pending: every pair has its member's score")
        return 0

    if args.dry_run:
        for row in todo[:20]:
            print("  {:<7} {:<15} {:>5} chars  {}".format(
                row["cluster_id"], row["source"], len(row["summary"] or ""),
                " ".join((row["title"] or "?").split())[:50]))
        if len(todo) > 20:
            print("  ... and {} more".format(len(todo) - 20))
        print()
        print("(dry run - nothing scored, nothing stored)")
        return 0

    try:
        health = api.health(endpoint)
    except api.ApiError as exc:
        print("FATAL scoring service not reachable at {}: {}".format(endpoint, exc))
        return 2
    print("model now     : {}  ({})".format(health.get("model"), endpoint))
    print()

    conn = storage.connect(args.db)
    scored = 0
    failures = 0
    total_ms = 0.0
    try:
        for index, row in enumerate(todo, start=1):
            context = analyse.build_context({
                "title": row["title"],
                "summary": row["summary"] or "",
                "published": row["published"],
            })
            try:
                result, latency_ms = api.score_timed(
                    context, schema.ANALYSIS_SCHEMA, schema.SCORE_FIELDS,
                    endpoint=endpoint)
            except api.ApiError as exc:
                failures += 1
                print("  FAILED {} {}: {}".format(
                    str(row["id"])[:12], row["source"], exc))
                continue
            parsed = analyse.parse_response(result)
            total_ms += latency_ms
            storage.save_analysis(
                conn,
                item_id=row["id"],
                schema_version=args.schema_version,
                sentiment=parsed["sentiment"],
                impact=parsed["impact"],
                impact_expected=parsed["impact_expected"],
                scope=parsed["scope"],
                probabilities=parsed["probabilities"],
                latency_ms=latency_ms,
                raw_response=result,
            )
            conn.commit()
            scored += 1
            if index % 25 == 0 or index == len(todo):
                print("  [{}/{}] {} scored, {} failed".format(
                    index, len(todo), scored, failures))
    finally:
        conn.close()

    print()
    if scored:
        print("mean latency  : {:.0f} ms/pair".format(total_ms / scored))
    print("scored {} pair member(s), {} failed".format(scored, failures))
    print()
    print("The index is unchanged: every cluster representative already has a")
    print("score and storage.analysed_rows prefers it by rank, so these rows are")
    print("ignored until something reads them. Re-run to continue where this")
    print("stopped - a scored pair is never picked again.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
