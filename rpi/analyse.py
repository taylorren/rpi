#!/usr/bin/env python3
"""Analyse pending news items via the scoring API.

One request per item, carrying all three questions at once (the API accepts a
schema mapping, so there is no reason to spend three round trips).

Items already analysed at the current ``schema_version`` are skipped, so this
is incremental and safe to re-run. Failures are recorded per item and retried
automatically a few times with a gap between attempts (the policy lives in
``rpi.storage``); after that they are parked, so a permanently unscoreable item
cannot wedge the queue, and ``--retry-failed`` forces one more look regardless.

Requests are issued serially. The service keeps one quantized model resident in
VRAM and serialises internally, so threading would add complexity without
throughput - measure before changing that.

Usage::

    python -m rpi.analyse
    python -m rpi.analyse --limit 5 --dry-run
    python -m rpi.analyse --retry-failed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import api, config, paths, schema, storage


def build_context(record: Any) -> str:
    """Title plus summary - the analysable unit that fits the token budget."""
    title = (record["title"] or "").strip()
    summary = (record["summary"] or "").strip()
    published = record["published"] or "unknown"
    if summary:
        return "Published: {}\nHeadline: {}\nSummary: {}".format(
            published, title, summary)
    return "Published: {}\nHeadline: {}".format(published, title)


def parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the fields we persist from a ``/score`` response.

    Types are coerced defensively: the API currently returns ``impact`` as a
    JSON number, but it is an enum over integer *strings*, so a string form is
    equally plausible and must not silently become ``None``.
    """
    output = result.get("output") or {}
    expected = api.expected_scores(result)

    raw_impact = output.get("impact")
    impact: Optional[int]
    try:
        impact = int(raw_impact) if raw_impact is not None else None
    except (TypeError, ValueError):
        impact = None

    sentiment = output.get("sentiment")
    scope = output.get("scope")

    return {
        "sentiment": str(sentiment) if sentiment is not None else None,
        "impact": impact,
        "impact_expected": expected.get("impact"),
        "scope": str(scope) if scope is not None else None,
        "probabilities": api.compact_probabilities(
            result, ["sentiment", "impact", "scope"]),
    }


def run(db_path: Path, cfg: config.RpiConfig, limit: Optional[int],
        retry_failed: bool, dry_run: bool, quiet: bool,
        model_name: Optional[str] = None) -> int:
    conn = storage.connect(db_path)
    # Record which model produced these scores. Attribution belongs in the data,
    # not in the front end: a hardcoded model name would silently go stale the
    # moment the served model is changed.
    if model_name:
        storage.set_meta(conn, "model", model_name)
        conn.commit()
    analysed = 0
    failed = 0
    parked = 0
    total_ms = 0.0

    try:
        pending: List[Any] = storage.pending_items(
            conn, schema.SCHEMA_VERSION, limit=limit, retry_failed=retry_failed)
        if not pending:
            if not quiet:
                print("nothing pending at schema_version={}".format(
                    schema.SCHEMA_VERSION))
            return 0
        if not quiet:
            print("{} item(s) pending at schema_version={}".format(
                len(pending), schema.SCHEMA_VERSION))

        for index, record in enumerate(pending, start=1):
            context = build_context(record)
            label = (record["title"] or "")[:56]
            try:
                result, latency_ms = api.score_timed(
                    context, schema.ANALYSIS_SCHEMA, schema.SCORE_FIELDS,
                    endpoint=cfg.endpoint)
            except api.ApiError as exc:
                failed += 1
                if not dry_run:
                    storage.record_analysis_error(
                        conn, record["id"], schema.SCHEMA_VERSION, str(exc))
                    conn.commit()
                if not quiet:
                    print("[{}/{}] FAILED {}: {}".format(
                        index, len(pending), label, exc))
                continue

            parsed = parse_response(result)
            total_ms += latency_ms
            analysed += 1

            if not dry_run:
                storage.save_analysis(
                    conn,
                    item_id=record["id"],
                    schema_version=schema.SCHEMA_VERSION,
                    sentiment=parsed["sentiment"],
                    impact=parsed["impact"],
                    impact_expected=parsed["impact_expected"],
                    scope=parsed["scope"],
                    probabilities=parsed["probabilities"],
                    latency_ms=latency_ms,
                    raw_response=result,
                )
                # Commit per item so an interrupted run keeps its progress.
                conn.commit()

            if not quiet:
                print("[{}/{}] {:<56} {:<8} impact={:<4} exp={:<5} {}".format(
                    index, len(pending), label,
                    parsed["sentiment"] or "?",
                    parsed["impact"] if parsed["impact"] is not None else "?",
                    "{:.2f}".format(parsed["impact_expected"])
                    if parsed["impact_expected"] is not None else "?",
                    parsed["scope"] or "?"))
        # Parked items are invisible in the numbers above: they are neither
        # pending nor a failure of *this* run, and no later run will pick them
        # up. Silent omission is how the 2026-09-24 CUDA batch went unnoticed.
        parked = storage.failed_count(conn, schema.SCHEMA_VERSION)
    finally:
        conn.close()

    print("analysed {} item(s), {} failed{}".format(
        analysed, failed, " (dry run, nothing written)" if dry_run else ""))
    if analysed:
        print("mean latency: {:.0f} ms/item".format(total_ms / analysed))
    if parked:
        print("{} item(s) parked after {} failed attempt(s); no scheduled run "
              "will retry them - use --retry-failed to force one".format(
                  parked, storage.MAX_ANALYSIS_ATTEMPTS))
    return 0 if failed == 0 else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Analyse pending news items.")
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--retry-failed", action="store_true",
                        help="retry failed items even if the automatic retry "
                             "policy has parked them")
    parser.add_argument("--dry-run", action="store_true",
                        help="call the API but write nothing")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--endpoint", default=None,
                        help="override the scoring service URL")
    args = parser.parse_args(argv)

    cfg = config.load()
    if args.endpoint:
        cfg.endpoint = args.endpoint

    model_name: Optional[str] = None
    if not args.dry_run:
        try:
            health = api.health(cfg.endpoint)
        except api.ApiError as exc:
            print("ERROR scoring service not reachable at {}: {}".format(
                cfg.endpoint, exc))
            print("      start it with: .venv\\Scripts\\python.exe .\\nimble_serve.py "
                  "--quant 4bit --port 8765")
            return 2
        reported = health.get("model")
        model_name = str(reported) if reported else None

    return run(args.db, cfg, args.limit, args.retry_failed, args.dry_run,
               args.quiet, model_name=model_name)


if __name__ == "__main__":
    sys.exit(main())
