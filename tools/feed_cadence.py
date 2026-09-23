#!/usr/bin/env python3
"""Measure how fast each feed publishes, to choose a sensible refresh interval.

Why this matters
----------------
Polling frequency does **not** determine whether a story is captured - the feed's
own backlog does. Each poll takes the newest ``max_items`` per feed, so as long as
fewer than ``max_items`` new items appear between polls, nothing is missed. A feed
publishing 25 items/day with a cap of 20 covers ~19 hours of news per poll, so it
could be polled twice a day without loss.

Two failure modes bracket the choice:

* **Poll too often** - wasted work. Harmless, because items already emitted are
  skipped by content hash (no model calls), but it achieves nothing.
* **Poll too rarely** - two distinct losses:
    1. more than ``max_items`` new items accumulate, so older ones scroll off the
       feed before they are ever seen - genuine, unrecoverable data loss;
    2. the live chart lags by up to one interval. Note this only affects the
       *live* view: snapshots are recomputed from event times on every run, so
       late-arriving items are backfilled into history correctly.

Reported per feed:

* ``rate``     - items published per hour, from the spread of publication times
* ``window``   - how much news the feed currently exposes, in hours
* ``safe``     - the longest interval that still loses nothing, ``max_items / rate``
* ``lag``      - age of the newest item, i.e. how far behind the feed is right now

Usage::

    python tools/feed_cadence.py
    python tools/feed_cadence.py --enabled-only
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fetcher"))

import fetch_rss  # noqa: E402

from rpi import paths  # noqa: E402

# Publishers can declare how often they want to be polled. RSS 2.0 has <ttl>
# (minutes); the Syndication module has updatePeriod/updateFrequency. Where a
# feed states one, that is authoritative and beats anything inferred here.
_TTL_RE = re.compile(rb"<ttl>\s*(\d+)\s*</ttl>", re.IGNORECASE)
_SY_PERIOD_RE = re.compile(
    rb"<sy:updatePeriod>\s*(hourly|daily|weekly|monthly)\s*</sy:updatePeriod>",
    re.IGNORECASE)
_SY_FREQ_RE = re.compile(rb"<sy:updateFrequency>\s*(\d+)\s*</sy:updateFrequency>",
                         re.IGNORECASE)
_PERIOD_MINUTES = {"hourly": 60, "daily": 1440, "weekly": 10080, "monthly": 43200}


def declared_interval_minutes(data: bytes) -> Optional[int]:
    """Poll interval the publisher asks for, in minutes, if stated."""
    match = _TTL_RE.search(data)
    if match:
        return int(match.group(1))

    period = _SY_PERIOD_RE.search(data)
    if period:
        unit = _PERIOD_MINUTES.get(period.group(1).decode().lower(), 0)
        frequency = _SY_FREQ_RE.search(data)
        count = int(frequency.group(1)) if frequency else 1
        return int(unit / count) if count else None
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage")[0].strip(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path,
                        default=paths.PROJECT_ROOT / "fetcher" / "feeds.json")
    parser.add_argument("--enabled-only", action="store_true")
    args = parser.parse_args(argv)

    config, feeds = fetch_rss.load_config(args.config)
    if args.enabled_only:
        feeds = [f for f in feeds if f.enabled]
    if not feeds:
        print("no feeds to measure")
        return 1

    opener = fetch_rss.build_opener(config.proxy)
    now = datetime.now(timezone.utc)
    cap = config.max_items_per_feed

    print("proxy: {}".format(fetch_rss.describe_proxy(config.proxy)))
    print("per-feed cap (max_items): {}\n".format(cap))

    header = "{:<18} {:>4} {:>7} {:>9} {:>9} {:>9} {:>8} {:>9}".format(
        "feed", "set", "items", "rate/h", "window(h)", "safe(h)", "lag(h)", "ttl")
    print(header)
    print("-" * len(header))

    rates: List[float] = []
    safes: List[float] = []
    declared: List[int] = []

    for spec in feeds:
        state = "on" if spec.enabled else "off"
        try:
            data = fetch_rss.read_source(spec.url, config.timeout, opener)
            entries = fetch_rss.parse_feed(data, config.max_summary_chars,
                                           config.max_title_chars)
        except Exception as exc:  # noqa: BLE001 - diagnostic tool
            print("{:<18} {:>4} {:>7} {:>9} {:>9} {:>9} {:>8} {:>9}".format(
                spec.name[:18], state, "-", "-", "-", "-", "-",
                "unreachable"))
            continue

        stated = declared_interval_minutes(data)
        if stated:
            declared.append(stated)
        stated_text = "{}m".format(stated) if stated else "-"

        dated = sorted(e.published for e in entries if e.published)
        if len(dated) < 2:
            print("{:<18} {:>4} {:>7} {:>9} {:>9} {:>9} {:>8} {:>9}".format(
                spec.name[:18], state, len(entries), "-", "-", "-", "-",
                stated_text))
            continue

        span_hours = (dated[-1] - dated[0]).total_seconds() / 3600.0
        rate = (len(dated) - 1) / span_hours if span_hours > 0 else 0.0
        window_hours = len(dated) / rate if rate > 0 else float("inf")
        # The longest interval that still sees every item: whichever comes first,
        # the cap filling up or the item scrolling off the end of the feed.
        safe_hours = min(cap / rate if rate > 0 else float("inf"), window_hours)
        lag_hours = (now - dated[-1]).total_seconds() / 3600.0

        rates.append(rate)
        safes.append(safe_hours)

        print("{:<18} {:>4} {:>7} {:>9.2f} {:>9.1f} {:>9.1f} {:>8.1f} {:>9}".format(
            spec.name[:18], state, len(dated), rate, window_hours,
            safe_hours, lag_hours, stated_text))

    if not rates:
        print("\nno usable data")
        return 1

    print()
    print("rate      = items published per hour (from the spread of their dates)")
    print("window    = how much news the feed currently exposes")
    print("safe      = longest interval that still misses nothing")
    print("lag       = age of the newest item right now")
    print("declared  = interval the publisher asks for (RSS ttl / sy:updatePeriod)")
    print()
    tightest = min(safes)
    print("tightest 'safe' across all feeds : {:.1f} h".format(tightest))
    print("slowest publishing feed          : {:.2f} items/h".format(min(rates)))
    print("fastest publishing feed          : {:.2f} items/h".format(max(rates)))
    if declared:
        print("publishers declaring a ttl       : {} of {} ({}m to {}m)".format(
            len(declared), len(feeds), min(declared), max(declared)))
    else:
        print("publishers declaring a ttl       : none")
    print()
    print("An interval comfortably below {:.1f} h loses nothing. Any extra frequency".format(tightest))
    print("beyond that buys freshness of the live view, not completeness of the data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
