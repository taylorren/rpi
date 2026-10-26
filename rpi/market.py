#!/usr/bin/env python3
"""The market's own reading, kept beside the index's.

Why this is here
----------------
The index answers "is today's news unusually bad?" and it is presented like a
stock index, so the obvious next question is the market's: did the shares move
the same way? Nothing inside the index can answer that - it is built from news
text alone - so the answer has to come from outside, and this module is where
the outside is read and kept.

What is stored, and what is deliberately not
--------------------------------------------
* **Bars, not a model.** What is stored is the market's own level, with the
  provider's move against the previous close. Nothing here is derived from the
  news and nothing here feeds the index: the two series are drawn next to each
  other so a reader can compare them, and neither is an input to the other.
  That is also why none of it lives in ``rpi.config.json`` - these numbers do
  not shape the index, so changing them must not invalidate a snapshot.
* **A bar's timestamp is when its value was true.** A 5-minute bar is stamped
  at its close (the session's last bar is 15:00, not 14:55), and a daily bar at
  the session's close too. So a market point and an index point that carry
  different timestamps are not misaligned arithmetic - they are two readings of
  the moments each was actually taken at. The exception is the newest bar, which
  a provider labels with the time it will *close* - the bar still being formed
  has not closed yet - so it is pinned to the moment it was fetched. See
  :func:`to_rows`.
* **SQLite, like everything else.** Bars are upserted into ``market_bars``,
  keyed by (symbol, resolution, timestamp), so re-fetching is free and a
  provider's rolling window cannot erase what earlier runs stored. The database
  is the record; the API is only how it gets filled.

Sources
-------
* **Eastmoney** (primary) - the ``push2his`` 5-minute series, about six weeks
  deep, so one request covers all three chart windows. Verified 2026-10-26
  against Tencent's daily series for 2026-10-15 .. 10-23: identical opens,
  closes, highs and lows. ``klt=1`` was rejected because it returns the current
  session only.
* **Tencent** (fallback) - daily bars. A different operator on a different
  endpoint is the point of a fallback; its own 5-minute endpoint does not
  resolve from this workstation (DNS fails), so a fallback day leaves the
  intraday windows coarse rather than empty.
* **Yahoo** (rejected) - ``000001.SS`` answers HTTP 403 from here and would work
  only from the VPS, which is the one host this pipeline does *not* run the
  market fetch on. A market source that needs the other machine is a different
  project.

The geography is inverted on purpose, and worth stating: the news feeds are
unreachable from this workstation and are fetched on the VPS, while a Chinese
market feed is reachable from here. So this call runs with the pipeline, on the
workstation, and is the only network request the pipeline itself makes.

Failure policy
--------------
Nothing here may fail a run. The index does not depend on the market, so a
provider outage, a changed response shape or a blocked network all end the same
way: the stored bars are left as they are, ``rpi.export`` omits the market
section, and the page says nothing about the market rather than something
wrong. See ``rpi.export.build_market``.

Usage::

    python -m rpi.market               # fetch and store
    python -m rpi.market --dry-run     # fetch and print, write nothing
    python -m rpi.market --show 12     # print the newest stored bars
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import paths, storage

# The instrument, by the exchange's own code rather than a vendor's ticker:
# 000001 on Shanghai's market is the composite, "sh000001" is the same thing at
# Tencent, and the name is the one the provider itself returns.
SYMBOL = "000001.SS"
NAME = "Shanghai Composite"
LOCAL_NAME = "上证指数"

# Bar resolutions. The rank decides which of two bars stamped at the same moment
# is kept: when a fallback's daily bar and the primary's 5-minute bar describe
# the same instant, the finer one is the real reading and the coarse one was
# only ever standing in for it.
RESOLUTION_DAILY = "1d"
RESOLUTION_FIVE_MIN = "5m"
RESOLUTION_RANK: Dict[str, int] = {RESOLUTION_DAILY: 0, RESOLUTION_FIVE_MIN: 1}

# How far apart consecutive readings normally are, per resolution. The page uses
# this to tell a carried line from a traded one: the market is shut for most of
# every day and all weekend, and the line is drawn straight across those hours
# because the last price still stands, so a segment wider than this is a period
# with no market in it rather than a market that did nothing.
RESOLUTION_MINUTES: Dict[str, int] = {RESOLUTION_FIVE_MIN: 5,
                                      RESOLUTION_DAILY: 24 * 60}

USER_AGENT = "rpi-market/0.1 (personal index; local use)"
DEFAULT_TIMEOUT = 20.0

# Shanghai is UTC+8 all year, so the session runs 01:30-07:00 UTC and a bar's
# label is its close. No daylight saving, no exceptions - which is why a fixed
# offset is honest here rather than a timezone database lookup.
CST = timezone(timedelta(hours=8))
SESSION_CLOSE_HOUR = 15

# How much history to ask for. The primary answers with roughly six weeks of
# 5-minute bars whatever is asked for, so these are ceilings on the request
# rather than promises about the answer.
EASTMONEY_BARS = 2000
TENCENT_DAYS = 60

EASTMONEY_HOST = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_HOST = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


class MarketError(RuntimeError):
    """Raised when no provider could supply the market's bars."""


@dataclass(frozen=True)
class Bar:
    """One reading: when it was true, and what it was.

    ``change`` and ``change_pct`` are the provider's own move against the
    previous close where it publishes one. They are carried rather than
    recomputed, because the previous close is the exchange's and not ours: a
    daily bar whose predecessor is missing from the batch (a new instrument, a
    truncated response) would otherwise be published as an unchanged day.
    """

    ts: datetime
    value: float
    change: Optional[float] = None
    change_pct: Optional[float] = None


def iso(moment: datetime) -> str:
    """A UTC stamp in the format the rest of the store uses.

    Fixed width and UTC, because ``market_bars`` is read back with string
    comparisons and ordered by ``ts``.
    """
    return (moment.astimezone(timezone.utc).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"))


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_stamp(text: Any) -> Optional[datetime]:
    """Parse ``"2026-10-23 15:00"`` as China Standard Time.

    A naive stamp is a local stamp, and the only local time this provider
    speaks is Shanghai's.
    """
    text = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CST)
        except ValueError:
            continue
    return None


def _get(url: str, timeout: float) -> bytes:
    """Read a URL, raising MarketError on anything the caller cannot use.

    One exception type for every transport failure, because the caller's
    response is the same in all of them: try the next provider.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, */*;q=0.8",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise MarketError("cannot reach {}: {}".format(url, exc)) from exc


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

def eastmoney_url() -> str:
    """The primary endpoint, spelled out rather than hidden behind a helper.

    ``klt=5`` is the 5-minute series: one request covers both the day the chart
    is showing and about six weeks behind it, which is what the 1w and 1m
    windows need. ``fqt=1`` (forward-adjusted) means nothing for an index; it is
    left in because the request then reads the same here as it does pasted into
    a browser, which is how it was checked.
    """
    params = {
        "secid": "1.000001",        # market 1 (Shanghai), instrument 000001
        "klt": "5",
        "fqt": "1",
        "beg": "0",
        "end": "20500101",
        "lmt": str(EASTMONEY_BARS),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    query = "&".join("{}={}".format(k, v) for k, v in params.items())
    return "{}?{}".format(EASTMONEY_HOST, query)


def tencent_url() -> str:
    """The fallback endpoint: the last ``TENCENT_DAYS`` daily bars.

    Written literally, including its empty fields, because the parameter is a
    comma-separated mini-grammar rather than a value: ``sh000001,day,,,60,``
    means "symbol, daily, no range, no date, 60 bars, no adjustment".
    """
    return "{}?param=sh000001,day,,{},,".format(TENCENT_HOST, TENCENT_DAYS)


def _with_session_change(bars: Sequence[Bar]) -> List[Bar]:
    """Fill in each bar's move against the previous session's close.

    The provider's own change fields are deliberately not used, because on an
    intraday series they are the move *within* the bar. Measured 2026-10-26 on
    the 5-minute series: every published percentage matched the bar's difference
    from the bar before it (09:40 +0.02% = +0.64 points from 09:35; 09:50 -0.24%
    = -8.88 from 09:45) rather than from the previous close. The two coincide
    only on a session's first bar, where the bar before it *is* the previous
    close - which is exactly the bar a spot check would look at. The daily series
    publishes the day's move, so the same field means two different things at two
    resolutions, and the number the page shows ("the market is down 0.6% today")
    has to be the same one at both.

    "Previous session" is the previous date in exchange time, which is what the
    bars are stamped in: consecutive bars sharing a date are one session.
    """
    resolved: List[Bar] = []
    previous_close: Optional[float] = None
    session: Optional[date] = None
    for bar in bars:
        day = bar.ts.astimezone(CST).date()
        if session is None:
            session = day
        elif day != session:
            previous_close = resolved[-1].value
            session = day
        change = None if previous_close is None else bar.value - previous_close
        pct = None if not previous_close else change / previous_close * 100.0
        resolved.append(Bar(ts=bar.ts, value=bar.value, change=change,
                            change_pct=pct))
    return resolved


def parse_eastmoney(raw: bytes) -> List[Bar]:
    """Read Eastmoney's ``klines``, one CSV row per bar.

    A row is ``"2026-10-23 15:00,open,close,high,low,volume,amount,amplitude,
    change_pct,change,turnover"``, so the close is field 2. The change fields are
    in the request but not read - see :func:`_with_session_change`.

    The instrument's code is checked as well as the payload's shape. A wrong
    ``secid`` would otherwise be stored silently as this index's own history,
    and nothing downstream could tell the difference.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketError("eastmoney returned no JSON: {}".format(exc)) from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("klines"):
        raise MarketError("eastmoney returned no bars for {}".format(SYMBOL))
    if str(data.get("code") or "") != "000001":
        raise MarketError("eastmoney answered for {!r}, not 000001".format(
            data.get("code")))

    bars: List[Bar] = []
    for row in data["klines"]:
        parts = str(row).split(",")
        if len(parts) < 3:
            continue
        moment = _parse_stamp(parts[0])
        value = _to_float(parts[2])
        if moment is None or value is None:
            continue
        bars.append(Bar(ts=moment, value=value))
    if not bars:
        raise MarketError("eastmoney's bars were all unreadable")
    bars.sort(key=lambda bar: bar.ts)
    return _with_session_change(bars)


def parse_tencent(raw: bytes) -> List[Bar]:
    """Read Tencent's daily rows: ``[date, open, close, high, low, volume]``.

    The row carries no move of its own, so each bar's change is resolved from
    the previous session by :func:`_with_session_change` - the same helper the
    intraday series goes through, so one resolution cannot quietly mean
    something different from the other.

    The bar is stamped at the session's close (15:00 CST) because that is when
    its value became true: a daily close is the price that held until the next
    session opened, and it is not a value that was true at midnight.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketError("tencent returned no JSON: {}".format(exc)) from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    instrument = data.get("sh000001") if isinstance(data, dict) else None
    if not isinstance(instrument, dict):
        raise MarketError("tencent returned no bars for sh000001")
    rows = instrument.get("day") or instrument.get("qfqday") or []

    bars: List[Bar] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        try:
            session = datetime.strptime(str(row[0]), "%Y-%m-%d").replace(
                hour=SESSION_CLOSE_HOUR, tzinfo=CST)
        except ValueError:
            continue
        value = _to_float(row[2])
        if value is None:
            continue
        bars.append(Bar(ts=session, value=value))
    if not bars:
        raise MarketError("tencent's bars were all unreadable")
    bars.sort(key=lambda bar: bar.ts)
    return _with_session_change(bars)


def _fetch_eastmoney(timeout: float) -> Tuple[str, List[Bar]]:
    return RESOLUTION_FIVE_MIN, parse_eastmoney(_get(eastmoney_url(), timeout))


def _fetch_tencent(timeout: float) -> Tuple[str, List[Bar]]:
    return RESOLUTION_DAILY, parse_tencent(_get(tencent_url(), timeout))


# Tried in order, first answer wins. Ordered by the detail they carry, not by
# convenience: the fallback is coarser, and a fallback that is quietly in use
# looks exactly like a primary that works.
PROVIDERS: Tuple[Tuple[str, Callable[[float], Tuple[str, List[Bar]]]], ...] = (
    ("eastmoney", _fetch_eastmoney),
    ("tencent", _fetch_tencent),
)


def fetch(timeout: float = DEFAULT_TIMEOUT,
          verbose: bool = True) -> Tuple[str, str, List[Bar]]:
    """Ask each provider in turn; return ``(provider, resolution, bars)``.

    Every failure is reported rather than swallowed, because which provider
    answered decides the resolution of what is stored - a run on the fallback
    is a run whose intraday windows are coarser, and that has to be visible.
    """
    problems: List[str] = []
    for name, call in PROVIDERS:
        try:
            resolution, bars = call(timeout)
        except (MarketError, ValueError) as exc:
            problems.append("{}: {}".format(name, exc))
            if verbose:
                print("  {} unavailable: {}".format(name, exc))
            continue
        if not bars:
            problems.append("{}: no bars".format(name))
            continue
        if verbose:
            print("  {} answered: {} {} bar(s), newest {} at {}".format(
                name, len(bars), resolution, bars[-1].value, iso(bars[-1].ts)))
        return name, resolution, bars
    raise MarketError("no provider answered ({})".format("; ".join(problems)))


def to_rows(bars: Sequence[Bar], resolution: str, source: str,
            fetched_at: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """The shape ``storage.save_market_bars`` writes, one row per bar.

    A bar is stamped at ``min(its own label, the moment it was fetched)``, which
    only ever moves the newest one. A provider labels a 5-minute bar with the
    time it *closes* - including the bar still being formed, so a fetch at 01:48
    returns a bar labelled 01:50 - and a daily bar is labelled with the session's
    close all day. Both are labels for a value that was true at the fetch, not
    stamps for a reading taken in the future, so the newest bar is pinned to the
    moment it was actually read. Completed bars are untouched: their label is
    already in the past, and that is when their value was true.
    """
    stamp = fetched_at or datetime.now(timezone.utc)
    stamp = stamp.astimezone(timezone.utc).replace(microsecond=0)
    return [{
        "symbol": SYMBOL,
        "resolution": resolution,
        "ts": iso(min(bar.ts.astimezone(timezone.utc), stamp)),
        "value": round(bar.value, 4),
        "change": None if bar.change is None else round(bar.change, 4),
        "change_pct": None if bar.change_pct is None else round(bar.change_pct, 4),
        "source": source,
        "fetched_at": iso(stamp),
    } for bar in bars]


def run(db_path: Optional[Path] = None, timeout: float = DEFAULT_TIMEOUT,
        dry_run: bool = False, verbose: bool = True) -> Dict[str, Any]:
    """Fetch one round and store it; return what happened.

    Returns a summary rather than printing one, so the pipeline can log it in
    its own format. It raises ``MarketError`` only when nothing could be
    fetched at all - deciding what to do about that belongs to the caller, and
    in the scheduled run the answer is "nothing": the index publishes either
    way.
    """
    source, resolution, bars = fetch(timeout=timeout, verbose=verbose)
    rows = to_rows(bars, resolution, source)

    written = 0
    if not dry_run:
        conn = storage.connect(db_path or paths.DB_PATH)
        try:
            written = storage.save_market_bars(conn, rows)
            conn.commit()
        finally:
            conn.close()

    return {
        "source": source,
        "resolution": resolution,
        "fetched": len(bars),
        "written": written,
        "newest": iso(bars[-1].ts),
        "value": bars[-1].value,
        "change_pct": bars[-1].change_pct,
    }


def show(conn: Any, limit: int) -> int:
    """Print the newest stored bars, so a fetch can be read back."""
    rows = storage.market_bars(conn, SYMBOL)
    if not rows:
        print("no market bars stored yet")
        return 1
    print("{} bar(s) stored; newest {}:".format(len(rows), rows[-1]["ts"]))
    print("{:<22} {:<4} {:>10} {:>8}  {}".format(
        "ts (UTC)", "res", "value", "chg %", "source"))
    for row in rows[-limit:]:
        pct = row["change_pct"]
        print("{:<22} {:<4} {:>10.2f} {:>8}  {}".format(
            row["ts"], row["resolution"], row["value"],
            "-" if pct is None else "{:+.2f}".format(pct), row["source"]))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch {} ({}) and store it beside the index.".format(
            NAME, LOCAL_NAME))
    parser.add_argument("--db", type=Path, default=paths.DB_PATH)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and report, but write nothing")
    parser.add_argument("--show", type=int, default=0, metavar="N",
                        help="print the newest N stored bars and exit")
    args = parser.parse_args(argv)

    if args.show:
        conn = storage.connect(args.db)
        try:
            return show(conn, args.show)
        finally:
            conn.close()

    print("fetching {} ({})".format(NAME, LOCAL_NAME))
    try:
        summary = run(args.db, timeout=args.timeout, dry_run=args.dry_run)
    except MarketError as exc:
        print("ERROR {}".format(exc))
        return 1

    pct = summary["change_pct"]
    print("{}  {}  {}  ({} {} bar(s) {}, {})".format(
        summary["newest"], summary["value"],
        "-" if pct is None else "{:+.2f}%".format(pct),
        summary["fetched"], summary["resolution"],
        "fetched, dry run" if args.dry_run else "stored", summary["source"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

