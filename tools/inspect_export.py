#!/usr/bin/env python3
"""Inspect an exported rpi.json without opening a browser.

Reports, per window, the actual level span alongside the span the chart uses
once the epoch baseline is forced into the axis range. A large gap between the
two means the chart is compressing the data and the curve will look flatter
than it is.

Usage::

    python tools/inspect_export.py
    python tools/inspect_export.py --show-points today
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import paths  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage")[0].strip(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", type=Path, default=paths.EXPORT_PATH)
    parser.add_argument("--show-points", default=None,
                        help="dump every point of one window, e.g. today")
    args = parser.parse_args(argv)

    payload: Dict[str, Any] = json.loads(args.path.read_text(encoding="utf-8"))
    base = payload["meta"]["base_level"]
    stats = payload["summary"]
    # The market's series is drawn on the same axis as the index's, scaled to
    # the index's first visible level - so it can widen the range the chart
    # auto-scales to, and a scale report that ignored it would quietly stop
    # describing the chart it claims to describe.
    market_windows = (payload.get("market") or {}).get("windows") or {}

    print("export      : {}".format(args.path))
    print("generated   : {}".format(payload["meta"]["generated_at"]))
    print("level       : {:.3f}   change {:+.3f} ({:+.3f}%)".format(
        stats["level"], stats["change"], stats["change_pct"]))
    print("analysed    : {} item(s), {} in last 24h".format(
        stats["volume_total"], stats["volume_24h"]))
    print("backlog     : {} waiting to be analysed, {} parked after repeated "
          "failures".format(stats.get("pending", 0), stats.get("failed", 0)))
    print("base_level  : {:.3f}".format(base))
    print()

    header = "{:<6} {:>4} {:>11} {:>11} {:>9} {:>9} {:>8} {:>14}".format(
        "window", "pts", "min", "max", "span", "occupancy", "baseline", "market")
    print(header)
    print("-" * len(header))

    for name, points in payload["windows"].items():
        if not points:
            print("{:<6} {:>4}  (empty)".format(name, 0))
            continue
        levels = [p["level"] for p in points]
        data_min, data_max = min(levels), max(levels)

        # Mirror the UI: the market line is anchored to the index's first
        # visible level, and it is inside the auto-scaled range too.
        mkt_win = market_windows.get(name) or {}
        mkt = mkt_win.get("points") or []
        if len(mkt) >= 2 and mkt[0]["value"]:
            scale = points[0]["level"] / mkt[0]["value"]
            scaled = [p["value"] * scale for p in mkt]
            data_min = min(data_min, min(scaled))
            data_max = max(data_max, max(scaled))
        data_span = data_max - data_min

        # Mirror the UI exactly: auto-scale to the visible data, with 12%
        # padding. The epoch baseline is NOT forced into the range - doing that
        # squashed a quiet day into a few percent of the panel height.
        padding = (data_span if data_span > 0
                   else max(abs(data_max), 1.0) * 0.001) * 0.12
        usable = data_span + 2 * padding
        ratio = (data_span / usable * 100.0) if usable else 0.0

        # The baseline is only drawn when it happens to fall inside the range.
        baseline_drawn = data_min <= base <= data_max

        # The market's own move over the window, which is the number the page
        # prints beside the index's - measured from the first visible reading to
        # the last, so the two compare like for like.
        mkt_change = mkt_win.get("change_pct")
        mkt_note = ("-" if not mkt else "{}p {:+.2f}%".format(
            len(mkt), mkt_change if mkt_change is not None else 0.0))

        print("{:<6} {:>4} {:>11.3f} {:>11.3f} {:>9.3f} {:>8.1f}% {:>8} {:>14}".format(
            name, len(points), data_min, data_max, data_span, ratio,
            "drawn" if baseline_drawn else "-", mkt_note))

    print()
    print("occupancy = share of the chart height the data occupies (higher is better;")
    print("            the UI auto-scales, so this is normally ~83%).")
    print("baseline  = whether the epoch line at {} falls inside this window;".format(base))
    print("            it is drawn only when it does, so it never skews the scale.")
    print("market    = points of the market line in this window and its move over")
    print("            the window. It widens the range above when it swings further")
    print("            than the index does, and it is dashed where the market was")
    print("            shut and the line was carried.")
    print("NOTE: 'span' is the real move. Compare it with the header change of "
          "{:+.3f}.".format(stats["change"]))

    if args.show_points:
        points = payload["windows"].get(args.show_points)
        if not points:
            print("\nno points for window {!r}".format(args.show_points))
            return 1
        print("\n{} points for {!r}:".format(len(points), args.show_points))
        for point in points:
            print("  {}  {:.4f}  s={:+.4f}  n={}".format(
                point["t"], point["level"], point["s"], point["n"]))

    return 0


if __name__ == "__main__":
    sys.exit(main())
