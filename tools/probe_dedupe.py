#!/usr/bin/env python3
"""Probe near-duplicate detection on the real stored items.

Two things are being validated here, *before* any of it is relied upon:

1. **The cheap local prefilter.** Title-token similarity is meant to narrow
   candidate pairs so only the ambiguous band needs a model call. If real
   duplicates do not score highly, or unrelated pairs do, the thresholds are
   wrong.

2. **The model's "same event?" judgement.** Everything downstream depends on the
   API being able to tell two reports of one event from two different events
   that merely sound alike.

Prints the highest-scoring pairs with their component scores and time gap, then
asks the API about a sample so the verdicts can be compared against the titles.

Usage::

    python tools/probe_dedupe.py --top 25
    python tools/probe_dedupe.py --top 12 --ask
"""

from __future__ import annotations

import argparse
import itertools
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import api, config as config_mod, paths, schema, storage  # noqa: E402

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "from", "by", "as", "is", "are", "was", "were", "be", "been", "it",
    "its", "this", "that", "these", "those", "has", "have", "had", "will",
    "would", "can", "could", "may", "might", "says", "said", "after", "over",
    "into", "about", "amid", "new", "more", "than", "who", "how", "why", "what",
}

_PUNCT_RE = re.compile(r"[^a-z0-9]+")
_LEAD_TAG_RE = re.compile(r"^(video|photos?|live|watch|breaking|update)s?\b[:\s-]*",
                          re.IGNORECASE)


def tokens(title: str) -> set:
    """Normalised token set: lowercase, punctuation stripped, stopwords dropped."""
    text = _LEAD_TAG_RE.sub("", title or "")
    text = _PUNCT_RE.sub(" ", text.lower())
    return {t for t in text.split() if len(t) >= 2 and t not in STOPWORDS}


def similarity(a: str, b: str) -> Tuple[float, float, float]:
    """Return (blended, jaccard, containment) for two titles."""
    left, right = tokens(a), tokens(b)
    if not left or not right:
        return 0.0, 0.0, 0.0
    inter = len(left & right)
    union = len(left | right)
    jaccard = inter / union if union else 0.0
    containment = inter / min(len(left), len(right))
    # Containment catches "X: short" against "X: much longer version", which
    # Jaccard punishes purely for the length difference.
    return max(jaccard, containment), jaccard, containment


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def event_time(row: sqlite3.Row) -> Optional[datetime]:
    return parse_ts(row["published"]) or parse_ts(row["fetched_at"])


def load_items(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, title, summary, source, published, fetched_at FROM items"))


def candidate_pairs(rows: Sequence[sqlite3.Row], window_hours: float
                    ) -> List[Tuple[float, float, float, float, sqlite3.Row, sqlite3.Row]]:
    out = []
    window = timedelta(hours=window_hours)
    for left, right in itertools.combinations(rows, 2):
        if left["id"] == right["id"]:
            continue
        lt, rt = event_time(left), event_time(right)
        if lt is None or rt is None:
            continue
        gap_hours = abs((lt - rt).total_seconds()) / 3600.0
        if gap_hours > window_hours:
            continue
        # Cheap rejection before the more expensive token work.
        if abs(len(left["title"]) - len(right["title"])) > 90:
            continue
        score, jac, cont = similarity(left["title"], right["title"])
        if score <= 0.15:
            continue
        out.append((score, jac, cont, gap_hours, left, right))
    out.sort(key=lambda entry: -entry[0])
    return out


def ask_api(cfg: config_mod.RpiConfig, left: sqlite3.Row,
            right: sqlite3.Row) -> Tuple[Optional[float], Optional[bool], str]:
    """Ask whether two items describe the same real-world event."""
    context = (
        "Item A\n"
        "Headline: {}\n"
        "Summary: {}\n\n"
        "Item B\n"
        "Headline: {}\n"
        "Summary: {}\n"
    ).format(
        left["title"], (left["summary"] or "(no summary)")[:400],
        right["title"], (right["summary"] or "(no summary)")[:400],
    )
    question = {
        "same_event": {
            "type": "boolean",
            "description": ("Are these two items reporting the SAME real-world "
                            "event, rather than two different events that "
                            "merely involve similar topics or people?"),
        }
    }
    try:
        result = api.score(context, question, [], endpoint=cfg.endpoint)
    except api.ApiError as exc:
        return None, None, str(exc)
    return result.get("fields", {}).get("same_event", {}).get("probabilities", {}).get("true"), \
        bool((result.get("output") or {}).get("same_event")), ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage")[0].strip(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--window-hours", type=float, default=72.0)
    parser.add_argument("--ask", action="store_true",
                        help="send the top pairs to the API for a verdict")
    parser.add_argument("--ask-n", type=int, default=10)
    args = parser.parse_args(argv)

    conn = storage.connect(args.db)
    try:
        rows = load_items(conn)
    finally:
        conn.close()

    print("items: {}".format(len(rows)))
    pairs = candidate_pairs(rows, args.window_hours)
    print("candidate pairs within {}h with score > 0.15: {}".format(
        args.window_hours, len(pairs)))
    print()

    header = "{:>6} {:>6} {:>6} {:>7}  {:<34} {:<34}".format(
        "score", "jacc", "cont", "gap(h)", "A", "B")
    print(header)
    print("-" * len(header))

    for score, jac, cont, gap, left, right in pairs[:args.top]:
        print("{:>6.2f} {:>6.2f} {:>6.2f} {:>7.1f}  {:<34} {:<34}".format(
            score, jac, cont, gap,
            "{}|{}".format(left["source"][:10], left["title"][:22]),
            "{}|{}".format(right["source"][:10], right["title"][:22])))

    if not args.ask:
        print()
        print("re-run with --ask to get the model's verdict on the top pairs")
        return 0

    cfg = config_mod.load()
    if not api.is_available(cfg.endpoint):
        print("ERROR API not reachable at {}".format(cfg.endpoint))
        return 2

    print()
    print("{:>6} {:>9} {:>7}  {}".format("score", "P(same)", "verdict", "pair"))
    print("-" * 108)
    for score, _jac, _cont, _gap, left, right in pairs[:args.ask_n]:
        prob, verdict, error = ask_api(cfg, left, right)
        if error:
            print("{:>6.2f} {:>9} {:>7}  {}".format(score, "-", "ERROR", error[:50]))
            continue
        print("{:>6.2f} {:>9.3f} {:>7}  {}  <>  {}".format(
            score, prob if prob is not None else -1.0,
            "SAME" if verdict else "diff",
            left["title"][:40], right["title"][:40]))

    return 0


if __name__ == "__main__":
    sys.exit(main())
