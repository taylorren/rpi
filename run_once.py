#!/usr/bin/env python3
"""Run the whole pipeline once: ingest -> analyse -> calculate -> export.

Intended as the single entry point for the scheduled job on the Windows box,
so cron only has to know about one command.

Every stage is idempotent, so running this more often than there is new news is
cheap: ingestion skips unchanged files, analysis skips already-scored items,
and the snapshot series is recomputed from stored analyses.

Usage::

    python run_once.py
    python run_once.py --skip-analyse      # refresh chart from existing scores
    python run_once.py --limit 5           # cap items analysed this run
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from rpi import (analyse as analyse_mod, api, calculator, config as config_mod,
                 dedupe as dedupe_mod, export as export_mod, ingest as ingest_mod,
                 paths, schema, storage)


def step(name: str) -> None:
    print()
    print("== {} {}".format(name, "=" * max(0, 68 - len(name))))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the RPI pipeline once.")
    parser.add_argument("--inbox", type=Path, default=paths.INBOX_DIR)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--out", type=Path, default=paths.EXPORT_PATH)
    parser.add_argument("--limit", type=int, default=None,
                        help="cap items analysed in this run")
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--skip-dedupe", action="store_true")
    parser.add_argument("--skip-analyse", action="store_true")
    parser.add_argument("--skip-calculate", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    cfg = config_mod.load()
    warnings = 0

    if not args.skip_ingest:
        step("ingest")
        if not args.inbox.exists():
            print("WARN inbox missing: {}".format(args.inbox))
            warnings += 1
        else:
            totals = ingest_mod.ingest(args.inbox, args.db)
            print("{} new item(s), {} duplicate(s), {} file(s) unchanged".format(
                totals["inserted"], totals["duplicate"], totals["skipped_files"]))

    # De-duplication runs BEFORE analysis on purpose: a story covered by six
    # outlets should cost one model call, not six.
    if not args.skip_dedupe:
        step("dedupe")
        conn = storage.connect(args.db)
        try:
            if not api.is_available(cfg.endpoint):
                print("WARN scoring service unreachable; local matching only")
                warnings += 1
            counts_before = storage.counts(conn)
            if counts_before["items"] == 0:
                print("no items to cluster")
            else:
                dstats = dedupe_mod.rebuild_clusters(conn, cfg,
                                                     verbose=not args.quiet)
                groups = storage.duplicate_groups(conn)
                print("{} cluster(s) cover {} item(s); {} duplicate(s) removed"
                      .format(dstats["clusters"], dstats["items"],
                              dstats["items"] - dstats["clusters"]))
                for group in groups[:5]:
                    print("  x{} {}".format(
                        group["member_count"], group["title"][:62]))
                if len(groups) > 5:
                    print("  ... and {} more".format(len(groups) - 5))
        finally:
            conn.close()

    if not args.skip_analyse:
        step("analyse")
        # Check the service before spending time on it, and degrade rather than
        # fail: a stale chart beats no chart.
        if not api.is_available(cfg.endpoint):
            print("WARN scoring service unreachable at {}; skipping analysis".format(
                cfg.endpoint))
            print("     existing scores will still be used to rebuild the index")
            warnings += 1
        else:
            code = analyse_mod.run(args.db, cfg, args.limit, args.retry_failed,
                                   dry_run=False, quiet=args.quiet)
            if code != 0:
                warnings += 1

    if not args.skip_calculate:
        step("calculate")
        conn = storage.connect(args.db)
        try:
            rows = storage.analysed_rows(conn, schema.SCHEMA_VERSION)
            if not rows:
                print("no analyses available; nothing to calculate")
                warnings += 1
            else:
                from datetime import datetime, timezone
                items = calculator.build_items(rows, cfg)
                series = calculator.recalculate(
                    conn, cfg, schema.SCHEMA_VERSION,
                    now=datetime.now(timezone.utc))
                stats = calculator.summarise(
                    items, series, cfg, now=datetime.now(timezone.utc))
                print("{} snapshot(s) over {} item(s)".format(
                    len(series), stats["volume_total"]))
                print("level {:.3f}  change {:+.3f} ({:+.3f}%)".format(
                    stats["level"], stats["change"], stats["change_pct"]))
        finally:
            conn.close()

    if not args.skip_calculate:
        step("export")
        conn = storage.connect(args.db)
        try:
            from datetime import datetime, timezone
            payload = export_mod.build_payload(
                conn, cfg, schema.SCHEMA_VERSION,
                now=datetime.now(timezone.utc))
        finally:
            conn.close()

        if not payload["windows"]["today"] and not payload["windows"]["1m"]:
            print("nothing to export")
            warnings += 1
        else:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            import json
            args.out.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8")
            print("wrote {} ({} bytes)".format(args.out, args.out.stat().st_size))

    elapsed = time.perf_counter() - started
    print()
    print("done in {:.1f}s{}".format(
        elapsed, "  ({} warning(s))".format(warnings) if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
