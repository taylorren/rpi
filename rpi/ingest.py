#!/usr/bin/env python3
"""Ingest fetcher output (JSONL) into the local SQLite store.

Idempotent at two levels:

* whole files are skipped when their content hash is unchanged, and
* individual items are skipped by primary key.

So re-running after a partial sync, or replaying a whole inbox directory, is
always safe.

Usage::

    python -m rpi.ingest
    python -m rpi.ingest --inbox inbox --db db/rpi.sqlite3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from . import paths, storage


def file_digest(path: Path) -> str:
    """Hash of file contents.

    The fetcher *appends* to a daily file, so the digest changes as items
    arrive and the file is re-read; duplicate rows are then filtered by the
    items primary key.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_records(path: Path) -> Iterator[Dict[str, Any]]:
    """Yield parsed JSON objects, reporting malformed lines rather than dying."""
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                print("  WARN {}:{}: {}".format(path.name, number, exc),
                      file=sys.stderr)
                continue
            if isinstance(value, dict):
                yield value


def ingest(inbox: Path, db_path: Path, force: bool = False) -> Dict[str, int]:
    """Ingest every ``*.jsonl`` file in ``inbox``. Returns summary counts."""
    conn = storage.connect(db_path)
    totals = {"files": 0, "skipped_files": 0, "inserted": 0, "duplicate": 0}

    try:
        files: List[Path] = sorted(inbox.glob("*.jsonl"))
        if not files:
            print("no .jsonl files in {}".format(inbox))
            return totals

        for path in files:
            digest = file_digest(path)
            if not force and not storage.file_needs_ingest(conn, path.name, digest):
                totals["skipped_files"] += 1
                continue

            records = list(iter_records(path))
            inserted, duplicate = storage.upsert_items(conn, records)
            storage.record_file(conn, path.name, digest, len(records))
            conn.commit()

            totals["files"] += 1
            totals["inserted"] += inserted
            totals["duplicate"] += duplicate
            print("  {:<28} {:>4} records, {:>4} new, {:>4} duplicate".format(
                path.name, len(records), inserted, duplicate))
    finally:
        conn.close()

    return totals


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest fetcher JSONL into SQLite.")
    parser.add_argument("--inbox", type=Path, default=paths.INBOX_DIR)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--force", action="store_true",
                        help="re-read files even if their hash is unchanged")
    args = parser.parse_args(argv)

    if not args.inbox.exists():
        print("ERROR inbox does not exist: {}".format(args.inbox))
        return 2

    print("ingesting {} -> {}".format(args.inbox, args.db))
    totals = ingest(args.inbox, args.db, force=args.force)

    conn = storage.connect(args.db)
    try:
        counts = storage.counts(conn)
    finally:
        conn.close()

    print("files processed: {} ({} unchanged)".format(
        totals["files"], totals["skipped_files"]))
    print("items inserted : {} ({} duplicates ignored)".format(
        totals["inserted"], totals["duplicate"]))
    print("store now holds: {} items, {} analyses, {} errors, {} snapshots".format(
        counts["items"], counts["analyses"], counts["analysis_errors"],
        counts["rpi_snapshots"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
