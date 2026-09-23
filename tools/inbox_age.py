#!/usr/bin/env python3
"""How long since the fetcher last produced an item, in minutes.

The pipeline has to answer one question: has the fetcher on the VPS stopped?
File modification times are the obvious way to ask it, and the wrong one - scp
stamps every transferred file with the time of the *transfer*, so a fetcher that
died days ago still looks brand new. Every record therefore carries
``fetched_at``: the UTC clock of the fetch run that produced it. This reads the
newest of those instead. That value travels inside the data, so no amount of
copying can falsify it.

Only the newest file is opened. Inbox files are named ``YYYY-MM-DD.jsonl``, so
the last name in sorted order holds the most recent fetches, and a year of
accumulated history costs one file to read rather than 365.

Be clear about what this measures: the last fetch that *wrote an item*. The
fetcher writes nothing when a run finds no new items, so a genuinely quiet
stretch and a dead fetcher look identical from here. At the measured
publication rate that is rare enough to live with (the arithmetic is in
``deploy/pull_and_run.ps1``), but it is a real limitation of the signal rather
than something the code works around.

Output is one line, ``<minutes> <timestamp>``. Exit status 0 when a timestamp
was found and 1 otherwise, so the caller can fall back to the file's own mtime
rather than trusting an age derived from nothing.

Usage:
    python tools/inbox_age.py [--inbox DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import paths  # noqa: E402


def parse_stamp(text: str) -> datetime:
    """Parse an RFC 3339 stamp. The fetcher always writes a trailing ``Z``."""
    value = text.strip()
    if value[-1:] in ("Z", "z"):
        value = value[:-1] + "+00:00"
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def newest_fetched_at(path: Path) -> Optional[datetime]:
    """Newest ``fetched_at`` in one inbox file, or None if it carries none."""
    newest: Optional[datetime] = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # one damaged line must not hide the ones around it
            if not isinstance(record, dict):
                continue
            raw = record.get("fetched_at")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                stamp = parse_stamp(raw)
            except ValueError:
                continue
            if newest is None or stamp > newest:
                newest = stamp
    return newest


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Age of the freshest fetched_at in the inbox.")
    parser.add_argument("--inbox", type=Path, default=paths.INBOX_DIR)
    args = parser.parse_args(argv)

    files = sorted(args.inbox.glob("*.jsonl"))
    if not files:
        print("no .jsonl files in {}".format(args.inbox), file=sys.stderr)
        return 1

    newest = newest_fetched_at(files[-1])
    if newest is None:
        print("no usable fetched_at in {}".format(files[-1].name),
              file=sys.stderr)
        return 1

    age_minutes = (datetime.now(timezone.utc) - newest).total_seconds() / 60.0
    print("{:.1f} {}".format(age_minutes, newest.strftime("%Y-%m-%dT%H:%M:%SZ")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
