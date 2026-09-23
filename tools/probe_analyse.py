#!/usr/bin/env python3
"""Probe the live analyser with the proposed analysis schema.

Purpose: validate the three-field schema against the real model *before*
storage is built around it. Checks that

  1. the service is reachable,
  2. a single call carrying three fields is accepted,
  3. ``expected_score`` comes back for the integer enum,
  4. probability distributions come back for all fields,
  5. the answers are sane for real news,
  6. end-to-end latency is bearable.

Usage::

    python tools/probe_analyse.py --limit 3
    python tools/probe_analyse.py --limit 5 --show-raw
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import api, config, schema  # noqa: E402


def latest_inbox(inbox: Path) -> Optional[Path]:
    files = sorted(glob.glob(str(inbox / "*.jsonl")))
    return Path(files[-1]) if files else None


def load_records(inbox: Path, limit: int) -> List[Dict[str, Any]]:
    path = latest_inbox(inbox)
    if path is None:
        raise SystemExit("no inbox files in {}".format(inbox))
    records: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    print("inbox: {} ({} records)".format(path.name, len(records)))
    return records[:limit]


def build_context(record: Dict[str, Any]) -> str:
    """Title plus summary - the analysable unit, per the token budget."""
    title = (record.get("title") or "").strip()
    summary = (record.get("summary") or "").strip()
    published = record.get("published") or "unknown"
    header = "Published: {}".format(published)
    if summary:
        return "{}\nHeadline: {}\nSummary: {}".format(header, title, summary)
    return "{}\nHeadline: {}".format(header, title)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage")[0].strip(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inbox", type=Path,
                        default=Path(__file__).resolve().parent.parent / "inbox")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--show-raw", action="store_true",
                        help="dump the full response for the first item")
    parser.add_argument("--endpoint", default=None)
    args = parser.parse_args(argv)

    cfg = config.load()
    endpoint = args.endpoint or cfg.endpoint

    print("endpoint: {}".format(endpoint))
    try:
        print("health  : {}".format(api.health(endpoint)))
    except api.ApiError as exc:
        print("FATAL {}".format(exc))
        return 2

    records = load_records(args.inbox, args.limit)
    if not records:
        print("no records to analyse")
        return 1

    print()
    print("schema_version={} fields={} score_fields={}".format(
        schema.SCHEMA_VERSION, list(schema.ANALYSIS_SCHEMA), schema.SCORE_FIELDS))
    print("=" * 108)

    failures = 0
    total_ms = 0.0

    for index, record in enumerate(records, start=1):
        context = build_context(record)
        try:
            result, elapsed_ms = api.score_timed(
                context, schema.ANALYSIS_SCHEMA, schema.SCORE_FIELDS,
                endpoint=endpoint)
        except api.ApiError as exc:
            failures += 1
            print("[{}] FAILED: {}".format(index, exc))
            continue

        total_ms += elapsed_ms
        output = result.get("output") or {}
        expected = api.expected_scores(result)
        probs = api.compact_probabilities(
            result, ["sentiment", "impact", "scope"])

        if args.show_raw and index == 1:
            print("RAW RESPONSE (item 1):")
            print(json.dumps(result, ensure_ascii=False, indent=2)[:3000])
            print("-" * 108)

        print("[{}] {} ({:.0f} ms)".format(
            index, (record.get("title") or "")[:70], elapsed_ms))
        print("    context chars : {}".format(len(context)))
        print("    sentiment     : {:<9} p={}".format(
            output.get("sentiment"),
            _fmt_probs(probs.get("sentiment"))))
        print("    impact        : {:<9} expected={} p={}".format(
            output.get("impact"),
            expected.get("impact"),
            _fmt_probs(probs.get("impact"))))
        print("    scope         : {:<9} p={}".format(
            output.get("scope"),
            _fmt_probs(probs.get("scope"))))
        print()

    print("=" * 108)
    ok = len(records) - failures
    print("{} of {} succeeded, {} failed".format(ok, len(records), failures))
    if ok:
        print("mean latency: {:.0f} ms/item".format(total_ms / ok))
    return 0 if failures == 0 else 1


def _fmt_probs(probs: Optional[Dict[str, float]]) -> str:
    if not probs:
        return "(none)"
    top = sorted(probs.items(), key=lambda kv: -kv[1])[:4]
    return " ".join("{}={:.3f}".format(k, v) for k, v in top)


if __name__ == "__main__":
    sys.exit(main())
