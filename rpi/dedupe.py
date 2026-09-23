#!/usr/bin/env python3
"""Near-duplicate detection across news sources.

Why
---
The same event is covered by every outlet, so with more than one source the same
story would enter the index several times and be counted several times. Measured
examples from real data: "US to expand military presence in Greenland" appeared
in both CGTN and BBC on the same day, and CGTN alone carried two items on the
same China-US trade story.

Three stages, cheapest first
----------------------------
1. **Exact canonical URL.** Already free - the fetcher strips tracking
   parameters, so identical articles collapse to one id before we even look.
2. **Title similarity.** A local token-overlap score. Near-identical headlines
   merge outright; clearly unrelated pairs are dropped.
3. **The model.** Only the ambiguous band is sent for a verdict.

The model is the authority. The local score is *not* a reliable predictor and is
only used to rank and to skip hopeless pairs: in testing on real data, two pairs
that both scored 0.43 received opposite verdicts (P=0.001 and P=0.996), and a
genuine match appeared as the differently-worded pair
"Iranian president to attend UNGA session" / "Iranian President Masoud
Pezeshkian arrives...". Hence a permissive reject floor and a bounded number of
model calls per item.

Clustering is greedy against *representatives* only, not against every item.
That keeps candidate generation at O(new items x clusters) instead of O(n^2) -
with 80 items the all-pairs approach already produced 108 candidates, which
would grow quadratically.

Clusters are derived, so they are rebuilt from scratch each run; the expensive
part (pair verdicts) is cached in ``duplicate_pairs`` and never re-asked.

Event time and representative
-----------------------------
These are deliberately different things:

* ``first_published`` is the earliest event time in the cluster, and is what the
  index uses - a story should enter the time series when it broke, not when the
  slowest outlet got round to covering it.
* ``representative`` is the member with the richest text, and is the one sent
  for analysis - the model does better with a full summary than a terse one.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import api, config as config_mod, paths, schema, storage

# --------------------------------------------------------------------------- #
# Title similarity
# --------------------------------------------------------------------------- #

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "from", "by", "as", "is", "are", "was", "were", "be", "been", "it",
    "its", "this", "that", "these", "those", "has", "have", "had", "will",
    "would", "can", "could", "may", "might", "says", "said", "after", "over",
    "into", "about", "amid", "new", "more", "than", "who", "how", "why", "what",
}

_PUNCT_RE = re.compile(r"[^a-z0-9]+")
# Editorial prefixes that carry no meaning about the event itself.
_LEAD_TAG_RE = re.compile(
    r"^(video|photos?|live|watch|breaking|update|exclusive|analysis|opinion)s?"
    r"\b[:\s\-|]*",
    re.IGNORECASE)


def tokens(title: str) -> set:
    """Normalised token set: lowercase, punctuation stripped, stopwords dropped."""
    text = _LEAD_TAG_RE.sub("", title or "")
    text = _PUNCT_RE.sub(" ", text.lower())
    return {t for t in text.split() if len(t) >= 2 and t not in STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Token-overlap similarity in [0, 1].

    Uses the maximum of Jaccard and containment. Containment matters because
    Jaccard punishes pure length differences, and outlets routinely expand the
    same headline to very different lengths.
    """
    left, right = tokens(a), tokens(b)
    if not left or not right:
        return 0.0
    inter = len(left & right)
    union = len(left | right)
    jaccard = inter / union if union else 0.0
    containment = inter / min(len(left), len(right))
    return max(jaccard, containment)


# --------------------------------------------------------------------------- #
# Pair decisions
# --------------------------------------------------------------------------- #

def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def event_time(row: Any) -> Optional[datetime]:
    """When the story happened: its own publication time, else when we saw it."""
    for key in ("published", "fetched_at"):
        try:
            parsed = parse_ts(row[key])
        except (IndexError, KeyError):
            continue
        if parsed:
            return parsed
    return None


def _url_key(row: Any) -> Optional[str]:
    try:
        return row["link"] or None
    except (IndexError, KeyError):
        return None


def ask_same_event(cfg: config_mod.RpiConfig, left: Any, right: Any
                   ) -> Tuple[Optional[bool], Optional[float], str]:
    """Ask the model whether two items describe the same real-world event."""
    context = (
        "Item A\nHeadline: {}\nSummary: {}\n\n"
        "Item B\nHeadline: {}\nSummary: {}\n"
    ).format(
        left["title"], (left["summary"] or "(no summary)")[:400],
        right["title"], (right["summary"] or "(no summary)")[:400],
    )
    question = {
        "same_event": {
            "type": "boolean",
            "description": (
                "Are these two items reporting the SAME real-world event, "
                "rather than two different events that merely involve similar "
                "topics, places or people?"
            ),
        }
    }
    try:
        result = api.score(context, question, [], endpoint=cfg.endpoint)
    except api.ApiError as exc:
        return None, None, str(exc)

    detail = (result.get("fields") or {}).get("same_event") or {}
    probabilities = detail.get("probabilities") or {}
    probability = probabilities.get("true")
    verdict = (result.get("output") or {}).get("same_event")
    if probability is None:
        # Fall back to the discrete answer if probabilities are unavailable.
        return (bool(verdict) if verdict is not None else None), None, ""
    return bool(probability >= cfg.dedupe_api_threshold), float(probability), ""


def local_score(left: Any, right: Any, cfg: config_mod.RpiConfig
                ) -> Tuple[float, str]:
    """Free, local verdict for a pair. Returns ``(score, auto_method)``.

    ``auto_method`` is non-empty when the pair is decided without the model:
    ``"url"`` for an identical canonical link, ``"title"`` for a near-identical
    headline. Otherwise the pair is merely a candidate and ``score`` is used to
    rank it against other candidates.
    """
    left_url, right_url = _url_key(left), _url_key(right)
    if left_url and right_url and left_url == right_url:
        return 1.0, "url"

    score = similarity(left["title"], right["title"])
    if score >= cfg.dedupe_auto_merge:
        return score, "title"
    return score, ""


# --------------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------------- #

def rebuild_clusters(conn: Any, cfg: config_mod.RpiConfig,
                     verbose: bool = False) -> Dict[str, int]:
    """Rebuild all clusters from scratch. Expensive pair verdicts are cached."""
    storage.clear_clusters(conn)

    items = storage.all_items(conn)
    stats: Dict[str, int] = {
        "items": len(items), "clusters": 0, "merged": 0,
        "api_calls": 0, "api_errors": 0, "url_merges": 0, "title_merges": 0,
    }
    if not items:
        return stats

    window = timedelta(hours=cfg.dedupe_window_hours)
    # (cluster_id, representative row, event time) for clusters seen so far.
    reps: List[Tuple[int, Any, Optional[datetime]]] = []
    rep_index: Dict[int, int] = {}

    for item in items:
        moment = event_time(item)

        # --- phase 1: free local scoring against in-window representatives ---
        candidates: List[Tuple[float, int, Any, str]] = []
        for cluster_id, rep, rep_moment in reps:
            if moment and rep_moment and abs(moment - rep_moment) > window:
                continue
            score, auto_method = local_score(item, rep, cfg)
            if auto_method:
                candidates.append((score, cluster_id, rep, auto_method))
            elif score >= cfg.dedupe_reject_floor:
                candidates.append((score, cluster_id, rep, ""))

        # Best first, so a confident match is found before the budget is spent.
        candidates.sort(key=lambda entry: -entry[0])

        # --- phase 2: resolve, bounded by the per-item model-call budget ---
        chosen: Optional[Tuple[float, int, str]] = None
        budget = max(int(cfg.dedupe_max_api_per_item), 0)

        for score, cluster_id, rep, auto_method in candidates:
            if auto_method == "url":
                stats["url_merges"] += 1
                chosen = (score, cluster_id, "url")
                break
            if auto_method == "title":
                stats["title_merges"] += 1
                chosen = (score, cluster_id, "title")
                break

            cached = storage.pair_verdict(conn, item["id"], rep["id"])
            if cached is not None:
                if cached["same"]:
                    chosen = (score, cluster_id, "cache")
                    break
                continue

            if budget <= 0:
                continue
            budget -= 1

            verdict, _probability, error = ask_same_event(cfg, item, rep)
            stats["api_calls"] += 1
            if verdict is None:
                stats["api_errors"] += 1
                continue

            storage.save_pair_verdict(
                conn, item["id"], rep["id"], verdict, score,
                "api:v{}".format(config_mod.DEDUPE_PROMPT_VERSION))
            if verdict:
                chosen = (score, cluster_id, "api")
                break

        if chosen is not None:
            score, cluster_id, method = chosen
            storage.add_member(conn, item["id"], cluster_id, False, score, method)
            # Use the richest member found so far as the matching exemplar for
            # later items. Final representative election still happens after
            # the pass, but updating here avoids comparing every later item to
            # a terse first headline when a fuller duplicate is already known.
            index = rep_index.get(cluster_id)
            if index is not None:
                _old_cluster, rep, _rep_moment = reps[index]
                if len(item["summary"] or "") > len(rep["summary"] or ""):
                    reps[index] = (cluster_id, item, moment)
            stats["merged"] += 1
        else:
            # Nothing matched: this item starts a new cluster.
            ts = (moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                  if moment else None)
            cluster_id = storage.add_cluster(conn, item["id"], ts)
            storage.add_member(conn, item["id"], cluster_id, True, None, "new")
            rep_index[cluster_id] = len(reps)
            reps.append((cluster_id, item, moment))
            stats["clusters"] += 1

    _reelect_representatives(conn)

    if verbose:
        print("  {} item(s) -> {} cluster(s), {} merged".format(
            stats["items"], stats["clusters"], stats["merged"]))
        print("  merges: {} by url, {} by title, {} confirmed, {} model call(s)".format(
            stats["url_merges"], stats["title_merges"],
            stats["merged"] - stats["url_merges"] - stats["title_merges"],
            stats["api_calls"]))

    return stats


def _reelect_representatives(conn: Any) -> None:
    """Pick the richest-text member as representative, and the earliest time.

    The representative is what gets analysed, so preferring the longest summary
    gives the model the most to work with. Event time is kept separate so the
    timeline still reflects when the story broke.
    """
    clusters = list(conn.execute(
        "SELECT cluster_id FROM clusters ORDER BY cluster_id"))
    for cluster in clusters:
        cluster_id = int(cluster["cluster_id"])
        members = list(conn.execute(
            "SELECT i.id, i.summary, i.published, i.fetched_at"
            " FROM cluster_members m JOIN items i ON i.id = m.item_id"
            " WHERE m.cluster_id = ?", (cluster_id,)))
        if not members:
            continue

        best = max(members, key=lambda m: (len(m["summary"] or ""), m["id"]))
        moments = [t for t in (event_time(m) for m in members) if t is not None]
        first = min(moments) if moments else None
        first_text = (first.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                      if first else None)

        conn.execute(
            "UPDATE clusters SET representative = ?, first_published = ?"
            " WHERE cluster_id = ?", (best["id"], first_text, cluster_id))
        conn.execute(
            "UPDATE cluster_members SET is_representative = CASE"
            "  WHEN item_id = ? THEN 1 ELSE 0 END WHERE cluster_id = ?",
            (best["id"], cluster_id))

    storage.finalise_clusters(conn)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _memory_copy(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Return an in-memory copy of ``conn`` for non-mutating dry runs."""
    copy = sqlite3.connect(":memory:")
    copy.row_factory = sqlite3.Row
    conn.backup(copy)
    return copy


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild duplicate clusters from stored items.")
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen, without storing")
    parser.add_argument("--show-groups", type=int, default=0,
                        help="print this many multi-member clusters")
    args = parser.parse_args(argv)

    cfg = config_mod.load()
    conn = storage.connect(args.db)
    work_conn = conn
    try:
        if args.dry_run:
            work_conn = _memory_copy(conn)

        if not api.is_available(cfg.endpoint):
            print("WARNING API unreachable at {}; only local matching will work"
                  .format(cfg.endpoint))

        before = storage.counts(work_conn)
        print("clustering {} item(s) (window {}h, auto-merge {}, floor {}, api {})"
              .format(before["items"], cfg.dedupe_window_hours,
                      cfg.dedupe_auto_merge, cfg.dedupe_reject_floor,
                      cfg.dedupe_api_threshold))

        stats = rebuild_clusters(work_conn, cfg, verbose=True)

        if args.show_groups:
            groups = storage.duplicate_groups(work_conn)
            print()
            print("{} cluster(s) contain more than one item:".format(len(groups)))
            for group in groups[:args.show_groups]:
                sources = storage.cluster_sources(work_conn, int(group["cluster_id"]))
                print("  [{}] {}  ({})".format(
                    group["member_count"], group["title"][:58],
                    ", ".join(sources)))

        if args.dry_run:
            print("\n(dry run - no changes written)")
    finally:
        if work_conn is not conn:
            work_conn.close()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
