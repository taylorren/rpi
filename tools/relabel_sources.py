#!/usr/bin/env python3
"""Relabel stored items so their ``source`` matches the feed config.

Why this exists: items record both a ``source`` label and the ``source_url``
they came from. If a feed was ever fetched with an explicit ``--feed <url>`` the
label became the raw URL, and because ingestion keys on the item hash it is
never rewritten by a later run. This rewrites those labels from ``feeds.json``.

Only touches rows whose label looks like a URL, so deliberately-chosen names are
never clobbered.

Usage::

    python tools/relabel_sources.py --dry-run
    python tools/relabel_sources.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import paths, storage  # noqa: E402


def load_feed_names(config_path: Path) -> Dict[str, str]:
    """Map feed URL -> configured name."""
    if not config_path.exists():
        return {}
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    mapping: Dict[str, str] = {}
    for entry in raw.get("feeds", []) or []:
        if isinstance(entry, dict) and entry.get("url") and entry.get("name"):
            mapping[str(entry["url"])] = str(entry["name"])
    return mapping


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage")[0].strip(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--config", type=Path,
                        default=paths.PROJECT_ROOT / "fetcher" / "feeds.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    names = load_feed_names(args.config)
    if not names:
        print("no feeds with names found in {}".format(args.config))
        return 1

    conn = storage.connect(args.db)
    try:
        # Only rows that still carry a URL-ish label and whose URL is known.
        rows = list(conn.execute(
            "SELECT DISTINCT source, source_url FROM items"
            " WHERE source LIKE 'http%' OR source LIKE '%://%'"))
        if not rows:
            print("no URL-style source labels found; nothing to do")
            return 0

        changes: List[tuple] = []
        for row in rows:
            new_name = names.get(row["source_url"] or "")
            if new_name and new_name != row["source"]:
                changes.append((row["source"], new_name))

        for old, new in changes:
            count = conn.execute(
                "SELECT COUNT(*) FROM items WHERE source = ?", (old,)).fetchone()[0]
            print("  {:>4} item(s): {}  ->  {}".format(count, old[:60], new))
            if not args.dry_run:
                conn.execute("UPDATE items SET source = ? WHERE source = ?",
                             (new, old))

        if args.dry_run:
            print("\n(dry run - nothing written)")
        else:
            conn.commit()
            print("\nrelabelled {} distinct source(s)".format(len(changes)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
