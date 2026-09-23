#!/usr/bin/env python3
"""RSS / Atom news fetcher for the RPI app.

Standalone and stdlib-only on purpose: this script is destined to run from a
cron job on a Linux box, so it must not need a virtualenv, a requirements
file, or any third-party package.

It fetches one or more feeds and appends *normalised* news items as JSON Lines
to an "inbox" directory. The analyser on the Windows box consumes that inbox;
this script never talks to the model itself.

    feeds  --fetch-->  inbox/YYYY-MM-DD.jsonl  --sync-->  analyser

Usage
-----
Smoke test against bundled fixtures (no network needed)::

    python fetcher/fetch_rss.py --feed fetcher/tests/sample_rss.xml \\
        --feed fetcher/tests/sample_atom.xml --out inbox --dry-run --no-state

Fetch the configured feeds for real::

    python fetcher/fetch_rss.py --config fetcher/feeds.json --out inbox

Output contract (one JSON object per line, keys sorted)
-------------------------------------------------------
    id          sha256 of the canonical link (or title|published) - the
                idempotency key; the analyser skips ids it has already seen
    source      feed name from the config
    source_url  the feed that produced the item
    title       cleaned headline
    summary     cleaned, whitespace-collapsed, truncated description
    link        canonicalised article URL (tracking params stripped)
    published   RFC 3339 UTC timestamp, or null if the feed had no usable date
    fetched_at  RFC 3339 UTC timestamp of this fetch
    schema      inbox record schema version (int)

Records are written oldest-first so the analyser sees a natural timeline.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

INBOX_SCHEMA_VERSION = 1

USER_AGENT = "rpi-fetcher/0.1 (personal news index; local use)"

ATOM_NS = "{http://www.w3.org/2005/Atom}"
RDF_NS = "{http://purl.org/rss/1.0/}"
RSS_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
XHTML_NS = "{http://www.w3.org/1999/xhtml}"

DEFAULT_MAX_ITEMS_PER_FEED = 20
DEFAULT_MAX_SUMMARY_CHARS = 1000
DEFAULT_MAX_TITLE_CHARS = 300
DEFAULT_TIMEOUT = 20.0
DEFAULT_STATE_RETENTION_DAYS = 30

# Proxy modes. "auto" picks up environment variables and, on Windows, the
# WinINET registry settings (which is how a system-wide VPN is discovered).
PROXY_AUTO = "auto"
PROXY_NONE = "none"

# Query-string noise that makes two URLs for the same article look different.
# Stripping it lets identical stories from different feeds collapse onto the
# same id for free, before any model-assisted de-duplication happens.
TRACKING_PREFIXES = ("utm_", "at_", "mc_", "pk_", "sc_")
TRACKING_EXACT = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "si", "s_kwcid",
    "cmpid", "CMP", "sh", "share_id", "ref_src", "ref_url", "spm", "cmp",
}

_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>",
                        re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Stripping inline markup leaves "text ." behind; tighten punctuation back up.
_PUNCT_SPACE_RE = re.compile(r"\s+([,.;:!?\u2026)])")
_OPEN_SPACE_RE = re.compile(r"([(\[])\s+")

# Tags become a space so Latin words don't fuse ("a<b>b" -> "a b"), but CJK
# scripts have no inter-word spaces, so "\u6280\u80fd<em>\u65e0\u58f0</em>" would become
# "\u6280\u80fd \u65e0\u58f0". Close those injected gaps back up afterwards.
_CJK_CLASS = (r"\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff"
              r"\uf900-\ufaff\uff00-\uffef")
_CJK_GAP_RE = re.compile("([{0}])\\s+([{0}])".format(_CJK_CLASS))

# A redirect or bot challenge usually yields HTML rather than a feed.
_HTML_SNIFF_RE = re.compile(rb"<(!doctype\s+html|html)\b", re.IGNORECASE)


def log(message: str) -> None:
    """Progress and diagnostics go to stderr so stdout stays pipeable."""
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Small helpers: text and time
# --------------------------------------------------------------------------- #

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def derive_name(url: str) -> str:
    """Best-effort readable label for a feed known only by its URL.

    Used when feeds are passed with ``--feed`` rather than named in the config,
    so records do not carry an unwieldy full URL as their source label.
    """
    if "://" not in url:
        return Path(url).stem or url
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return url
    for prefix in ("www.", "feeds.", "rss.", "news.", "www2."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host or url


def iso(dt: datetime) -> str:
    """RFC 3339 / ISO 8601 in UTC with a trailing Z."""
    return (dt.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"))


def parse_date(value: Optional[str]) -> Optional[datetime]:
    """Parse RSS (RFC 822) or Atom/DC (ISO 8601) dates. Returns None if unusable."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None

    # RFC 822, e.g. "Tue, 23 Sep 2026 10:11:12 GMT"
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        dt = None
    if dt is not None:
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    # ISO 8601, e.g. "2026-09-23T10:11:12.1234567Z"
    candidate = _FRACTION_RE.sub(r"\1", value.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def truncate(text: str, limit: int) -> str:
    """Trim to ``limit`` chars, preferring a word boundary and marking the cut."""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "\u2026"


def clean_text(raw: Optional[str], limit: int) -> str:
    """Strip markup and entities from feed text and collapse whitespace.

    Feed descriptions are inconsistently encoded: some are plain text, some
    escaped HTML (``&lt;p&gt;``), some raw HTML. Unescape, strip, unescape
    again so entities that survived inside markup still resolve.
    """
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _CONTROL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    text = _PUNCT_SPACE_RE.sub(r"\1", text)
    text = _OPEN_SPACE_RE.sub(r"\1", text)
    text = _close_cjk_gaps(text)
    return truncate(text, limit)


def _close_cjk_gaps(text: str) -> str:
    """Remove spaces that tag stripping injected between two CJK characters."""
    previous = None
    while previous != text:
        previous = text
        text = _CJK_GAP_RE.sub(r"\1\2", text)
    return text


def canonical_url(url: Optional[str]) -> Optional[str]:
    """Drop the fragment and tracking query params; normalise host casing."""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    if not parts.scheme or not parts.netloc:
        # Relative or malformed; hand it back untouched rather than guessing.
        return url

    kept = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low in TRACKING_EXACT or low.startswith(TRACKING_PREFIXES):
            continue
        kept.append((key, value))

    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    return urlunsplit((
        parts.scheme.lower(),
        netloc,
        parts.path or "/",
        urlencode(kept, doseq=True),
        "",  # drop fragment
    ))


def item_id(link: Optional[str], title: str, published: Optional[datetime]) -> str:
    """Stable content hash used as the idempotency key."""
    basis = link or "{}|{}".format(title, iso(published) if published else "")
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------- #
# Feed parsing
# --------------------------------------------------------------------------- #

@dataclass
class Entry:
    """One news item, already normalised."""
    title: str
    link: Optional[str]
    summary: str
    published: Optional[datetime]


def _text(elem: Optional[ET.Element]) -> Optional[str]:
    """Flatten an element's text, including CDATA and nested markup."""
    if elem is None:
        return None
    joined = "".join(elem.itertext()).strip()
    return joined or None


def _first(elem: ET.Element, *names: str) -> Optional[ET.Element]:
    for name in names:
        found = elem.find(name)
        if found is not None:
            return found
    return None


def _load_xml(data: bytes) -> ET.Element:
    """Parse bytes, falling back to a sanitising pass for real-world feeds."""
    try:
        return ET.fromstring(data)
    except ET.ParseError as original:
        text = data.decode("utf-8", errors="replace")
        text = _XML_DECL_RE.sub("", text, count=1)
        # XML has no notion of &nbsp; and friends; make them numeric.
        for entity, code in (("&nbsp;", "&#160;"), ("&mdash;", "&#8212;"),
                             ("&ndash;", "&#8211;"), ("&hellip;", "&#8230;"),
                             ("&rsquo;", "&#8217;"), ("&lsquo;", "&#8216;"),
                             ("&ldquo;", "&#8220;"), ("&rdquo;", "&#8221;")):
            text = text.replace(entity, code)
        try:
            return ET.fromstring(text)
        except ET.ParseError:
            raise original


def _atom_link(entry: ET.Element) -> Optional[str]:
    fallback: Optional[str] = None
    for link in entry.findall(ATOM_NS + "link"):
        href = (link.get("href") or "").strip()
        if not href:
            continue
        if link.get("rel", "alternate") == "alternate":
            return href
        if fallback is None:
            fallback = href
    return fallback


def _from_rss_item(node: ET.Element, summary_limit: int,
                   title_limit: int) -> Entry:
    title = clean_text(_text(_first(node, "title")), title_limit)
    link = canonical_url(_text(_first(
        node, "link", RDF_NS + "link", "guid",
    )))
    candidates = [
        _text(_first(node, "description", RDF_NS + "description", "summary")),
        _text(_first(node, RSS_CONTENT_NS + "encoded", "encoded",
                     XHTML_NS + "div")),
    ]
    # Feeds disagree about which field is richer; take the most informative.
    raw_summary = max((c for c in candidates if c), key=len, default=None)
    summary = clean_text(raw_summary, summary_limit)
    published = parse_date(_text(_first(
        node, "pubDate", "published", "date", DC_NS + "date",
    )))
    return Entry(title=title, link=link, summary=summary, published=published)


def _from_atom_entry(node: ET.Element, summary_limit: int,
                     title_limit: int) -> Entry:
    title = clean_text(_text(_first(node, ATOM_NS + "title")), title_limit)
    link = canonical_url(_atom_link(node))
    raw_summary = max(
        (v for v in (
            _text(_first(node, ATOM_NS + "summary")),
            _text(_first(node, ATOM_NS + "content")),
        ) if v),
        key=len,
        default=None,
    )
    summary = clean_text(raw_summary, summary_limit)
    published = parse_date(_text(_first(
        node, ATOM_NS + "published", ATOM_NS + "updated", DC_NS + "date",
    )))
    return Entry(title=title, link=link, summary=summary, published=published)


def parse_feed(data: bytes, summary_limit: int,
               title_limit: int) -> List[Entry]:
    """Parse an RSS 2.0, RSS 1.0/RDF or Atom document into Entries."""
    data = data.lstrip(b"\xef\xbb\xbf")  # a UTF-8 BOM defeats ElementTree
    if _HTML_SNIFF_RE.search(data[:2048]):
        raise ValueError(
            "server returned HTML, not a feed (redirect or bot challenge?)")
    root = _load_xml(data)
    tag = root.tag

    if tag == ATOM_NS + "feed":
        nodes = root.findall(ATOM_NS + "entry")
        entries = [_from_atom_entry(n, summary_limit, title_limit)
                   for n in nodes]
    else:
        nodes = (root.findall("./channel/item")
                 or root.findall(RDF_NS + "item")
                 or root.findall(".//item"))
        entries = [_from_rss_item(n, summary_limit, title_limit)
                   for n in nodes]

    # An item with neither headline nor body gives the analyser nothing to read.
    return [e for e in entries if e.title or e.summary]


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def read_source(url: str, timeout: float,
                opener: Optional[urllib.request.OpenerDirector] = None) -> bytes:
    """Read a feed from http(s), a file:// URL, or a plain local path."""
    if url.startswith("file://"):
        return Path(url[len("file://"):]).read_bytes()

    if "://" not in url:
        path = Path(url)
        if not path.exists():
            raise FileNotFoundError(
                "not a URL and no such file: {}".format(url))
        return path.read_bytes()

    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": ("application/rss+xml, application/atom+xml, "
                   "application/xml, text/xml;q=0.9, */*;q=0.8"),
    })
    client = opener or urllib.request.build_opener()
    with client.open(request, timeout=timeout) as response:
        return response.read()


def build_opener(proxy: str) -> urllib.request.OpenerDirector:
    """Build a URL opener according to the proxy setting.

    "auto" deliberately delegates to ``urllib``, which reads ``http_proxy`` /
    ``https_proxy`` and, on Windows, the WinINET registry settings. That is how
    a system-wide VPN's local proxy is picked up without configuration - but it
    is also invisible, which is why the resolved value is logged on every run
    and can be overridden explicitly with ``--proxy``.

    Only HTTP proxies are supported: SOCKS would require PySocks, and this
    script is deliberately stdlib-only. Most VPN clients expose a mixed port
    that answers HTTP CONNECT on a SOCKS port, so pointing an HTTP proxy
    setting at one usually works.
    """
    if proxy == PROXY_NONE:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy and proxy != PROXY_AUTO:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def describe_proxy(proxy: str) -> str:
    """Human-readable description of the effective proxy, for the log."""
    if proxy == PROXY_NONE:
        return "none (direct connection)"
    if proxy and proxy != PROXY_AUTO:
        return "{} (explicit)".format(proxy)
    detected = urllib.request.getproxies()
    found = detected.get("https") or detected.get("http")
    if found:
        return "{} (auto-detected)".format(found)
    return "none (auto-detected nothing)"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    max_items_per_feed: int = DEFAULT_MAX_ITEMS_PER_FEED
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS
    max_title_chars: int = DEFAULT_MAX_TITLE_CHARS
    timeout: float = DEFAULT_TIMEOUT
    state_retention_days: int = DEFAULT_STATE_RETENTION_DAYS
    # "auto", "none", or an explicit http:// proxy URL.
    proxy: str = PROXY_AUTO


@dataclass
class FeedSpec:
    """One configured news source.

    ``enabled`` lets extra sources live in the config without being fetched,
    so phase 1 (a single source) and phase 2 (several) can share one file:
    flip a flag instead of editing URLs. ``max_items`` overrides the global
    cap for tapid or noisy feeds.
    """
    name: str
    url: str
    enabled: bool = True
    max_items: Optional[int] = None


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def project_root() -> Path:
    return script_dir().parent


def load_config(config_path: Optional[Path]) -> Tuple[Config, List[FeedSpec]]:
    """Load settings, accepting several spellings for each feed entry.

    A feed may be a bare URL string, or an object with ``name``/``url`` and
    optional ``enabled``/``max_items``. Unknown keys are ignored so the
    config can grow without breaking older runs.
    """
    config = Config()
    feeds: List[FeedSpec] = []

    if config_path is None or not config_path.exists():
        return config, feeds

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("cannot read config {}: {}".format(config_path, exc))

    if not isinstance(raw, dict):
        raise SystemExit("config {} must be a JSON object".format(config_path))

    for entry in raw.get("feeds", []) or []:
        if isinstance(entry, str):
            feeds.append(FeedSpec(name=entry, url=entry))
            continue
        if not isinstance(entry, dict) or not entry.get("url"):
            log("WARN ignoring malformed feed entry: {!r}".format(entry))
            continue
        max_items = entry.get("max_items")
        feeds.append(FeedSpec(
            name=str(entry.get("name") or entry["url"]),
            url=str(entry["url"]),
            # Absent means enabled: a feed listed but unspecified is intended.
            enabled=bool(entry.get("enabled", True)),
            max_items=int(max_items) if isinstance(max_items, int) else None,
        ))

    for key in ("max_items_per_feed", "max_summary_chars", "max_title_chars",
                "state_retention_days"):
        if isinstance(raw.get(key), int):
            setattr(config, key, raw[key])
    if isinstance(raw.get("timeout"), (int, float)):
        config.timeout = float(raw["timeout"])
    if isinstance(raw.get("proxy"), str) and raw["proxy"].strip():
        config.proxy = raw["proxy"].strip()

    return config, feeds


# --------------------------------------------------------------------------- #
# Already-emitted tracking
# --------------------------------------------------------------------------- #

def load_seen(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log("WARN state file {} unreadable; starting fresh".format(path))
        return {}
    ids = raw.get("ids") if isinstance(raw, dict) else None
    if not isinstance(ids, dict):
        return {}
    return {str(k): str(v) for k, v in ids.items()}


def save_seen(path: Path, seen: Dict[str, str], retention_days: int) -> None:
    """Persist seen ids, dropping entries older than the retention window.

    Retention must comfortably exceed how long an item stays in its feed,
    otherwise a still-published item could be re-emitted as a duplicate.
    """
    cutoff = utcnow() - timedelta(days=max(retention_days, 1))
    pruned: Dict[str, str] = {}
    for key, stamp in seen.items():
        parsed = parse_date(stamp)
        if parsed is None or parsed >= cutoff:
            pruned[key] = stamp

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": iso(utcnow()), "ids": pruned}
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build_record(entry: Entry, source: str, source_url: str,
                 fetched_at: datetime) -> Dict[str, Any]:
    return {
        "id": item_id(entry.link, entry.title, entry.published),
        "source": source,
        "source_url": source_url,
        "title": entry.title,
        "summary": entry.summary,
        "link": entry.link,
        "published": iso(entry.published) if entry.published else None,
        "fetched_at": iso(fetched_at),
        "schema": INBOX_SCHEMA_VERSION,
    }


def check_sources(feeds: List[FeedSpec], config: Config,
                  opener: urllib.request.OpenerDirector,
                  stale_days: float = 3.0) -> int:
    """Automatically apply the three-part source acceptance test.

    A feed is only usable if it is (1) reachable, (2) serving current dates, and
    (3) carrying real article text. An HTTP 200 proves none of those: several
    feeds tested on 2026-09-23 returned 200 while being years stale or entirely
    without descriptions.

    Checks every configured feed, including disabled ones, so candidates can be
    vetted before being enabled. Returns the number of *enabled* feeds that
    failed, so this can gate a pipeline run.
    """
    now = utcnow()
    header = "{:<20} {:<4} {:>5} {:>6} {:>8} {:>7}  {}".format(
        "feed", "set", "items", "chars", "age(h)", "empty", "verdict")
    print(header)
    print("-" * len(header))

    enabled_failures = 0

    for spec in feeds:
        state = "on" if spec.enabled else "off"
        try:
            data = read_source(spec.url, config.timeout, opener)
            entries = parse_feed(data, config.max_summary_chars,
                                 config.max_title_chars)
        except (urllib.error.URLError, OSError, ET.ParseError, ValueError) as exc:
            print("{:<20} {:<4} {:>5} {:>6} {:>8} {:>7}  {}".format(
                spec.name[:20], state, "-", "-", "-", "-",
                "UNREACHABLE: {}".format(exc)[:58]))
            if spec.enabled:
                enabled_failures += 1
            continue

        if not entries:
            print("{:<20} {:<4} {:>5} {:>6} {:>8} {:>7}  {}".format(
                spec.name[:20], state, 0, "-", "-", "-",
                "EMPTY: feed parsed but has no items"))
            if spec.enabled:
                enabled_failures += 1
            continue

        dated = [e.published for e in entries if e.published]
        lengths = sorted(len(e.summary) for e in entries)
        median = lengths[len(lengths) // 2] if lengths else 0
        empty = sum(1 for e in entries if not e.summary)
        newest_age = (min((now - d).total_seconds() for d in dated) / 3600.0
                      if dated else None)

        if not dated:
            verdict = "NO DATES: cannot build a timeline"
            bad = True
        elif newest_age is not None and newest_age > stale_days * 24:
            verdict = "STALE: newest item is {:.0f}h old".format(newest_age)
            bad = True
        elif median == 0:
            verdict = "NO TEXT: every summary is empty"
            bad = True
        elif empty:
            verdict = "ok ({} of {} lack a summary)".format(empty, len(entries))
            bad = False
        else:
            verdict = "OK"
            bad = False

        if bad and spec.enabled:
            enabled_failures += 1

        print("{:<20} {:<4} {:>5} {:>6} {:>8} {:>7}  {}".format(
            spec.name[:20], state, len(entries), median,
            "{:.1f}".format(newest_age) if newest_age is not None else "-",
            empty, verdict))

    print()
    print("checks: reachable / current dates / non-empty text")
    if enabled_failures:
        print("{} enabled feed(s) FAILED the acceptance test".format(enabled_failures))
    else:
        print("all enabled feeds passed")
    return enabled_failures


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch RSS/Atom feeds into a JSONL inbox for the RPI analyser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage")[0].strip(),
    )
    parser.add_argument("--config", type=Path, default=None,
                        help="JSON config file (default: <script dir>/feeds.json)")
    parser.add_argument("--feed", action="append", default=[], metavar="URL_OR_PATH",
                        help="feed to fetch; repeatable, overrides --config")
    parser.add_argument("--out", type=Path, default=None,
                        help="inbox directory (default: <project root>/inbox)")
    parser.add_argument("--state", type=Path, default=None,
                        help="seen-ids file (default: <project root>/state/seen.json)")
    parser.add_argument("--max-items", type=int, default=None,
                        help="cap items taken per feed")
    parser.add_argument("--max-summary-chars", type=int, default=None,
                        help="truncate each summary to this many characters")
    parser.add_argument("--timeout", type=float, default=None,
                        help="per-request timeout in seconds")
    parser.add_argument("--no-state", action="store_true",
                        help="do not read or write the seen-ids file")
    parser.add_argument("--dry-run", action="store_true",
                        help="print records to stdout instead of writing")
    parser.add_argument("--list-feeds", action="store_true",
                        help="show the resolved feed configuration and exit")
    parser.add_argument("--check-sources", action="store_true",
                        help="test every configured feed and exit")
    parser.add_argument("--proxy", default=None, metavar="URL",
                        help="explicit http:// proxy (overrides config)")
    parser.add_argument("--no-proxy", action="store_true",
                        help="force a direct connection, ignoring any proxy")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    config_path = args.config
    if config_path is None:
        default_config = script_dir() / "feeds.json"
        config_path = default_config if default_config.exists() else None

    config, feeds = load_config(config_path)

    if args.feed:
        # An explicit --feed always wins over the config file, and is enabled.
        feeds = [FeedSpec(name=derive_name(url), url=url) for url in args.feed]

    if args.list_feeds:
        print("config: {}".format(config_path or "(none - defaults only)"))
        print("inbox:  {}".format(args.out or (project_root() / "inbox")))
        print("feeds:  {} configured, {} enabled".format(
            len(feeds), sum(1 for spec in feeds if spec.enabled)))
        for spec in feeds:
            cap = spec.max_items or config.max_items_per_feed
            print("  [{}] {:<18} cap={:<3} {}".format(
                "x" if spec.enabled else " ", spec.name, cap, spec.url))
        return 0

    active = [spec for spec in feeds if spec.enabled]

    # Proxy resolution is reported because "auto" can silently depend on
    # machine-level settings that do not exist on another host.
    if args.no_proxy:
        config.proxy = PROXY_NONE
    elif args.proxy:
        config.proxy = args.proxy
    opener = build_opener(config.proxy)

    if args.check_sources:
        if not feeds:
            log("ERROR no feeds configured")
            return 2
        print("proxy: {}".format(describe_proxy(config.proxy)))
        print()
        return 1 if check_sources(feeds, config, opener) else 0

    if not active:
        log("ERROR no enabled feeds; pass --feed or set \"enabled\": true on an "
            "entry in {}".format(config_path or (script_dir() / "feeds.json")))
        return 2

    if args.max_items is not None:
        config.max_items_per_feed = args.max_items
    if args.max_summary_chars is not None:
        config.max_summary_chars = args.max_summary_chars
    if args.timeout is not None:
        config.timeout = args.timeout
    config.max_items_per_feed = max(config.max_items_per_feed, 1)
    config.max_summary_chars = max(config.max_summary_chars, 80)

    out_dir = args.out or (project_root() / "inbox")
    state_path = args.state or (project_root() / "state" / "seen.json")

    log("reading {} feed(s){} ({} disabled)".format(
        len(active),
        " from {}".format(config_path) if config_path else "",
        len(feeds) - len(active)))
    log("proxy: {}".format(describe_proxy(config.proxy)))

    seen = {} if args.no_state else load_seen(state_path)
    fetched_at = utcnow()
    records: List[Dict[str, Any]] = []
    emitted_ids = set()
    failures = 0

    for spec in active:
        try:
            data = read_source(spec.url, config.timeout, opener)
            entries = parse_feed(data, config.max_summary_chars,
                                 config.max_title_chars)
        except (urllib.error.URLError, OSError, ET.ParseError, ValueError) as exc:
            failures += 1
            log("WARN {}: {}".format(spec.name, exc))
            continue

        considered = entries[:spec.max_items or config.max_items_per_feed]
        added = 0
        for entry in considered:
            record = build_record(entry, spec.name, spec.url, fetched_at)
            key = record["id"]
            if key in seen or key in emitted_ids:
                continue
            emitted_ids.add(key)
            records.append(record)
            added += 1

        log("  {:<24} {:>3} parsed, {:>3} new".format(
            spec.name, len(considered), added))

    # Oldest first so the analyser sees a chronological timeline.
    records.sort(key=lambda r: (r["published"] or r["fetched_at"], r["id"]))

    written_path: Optional[Path] = None
    if records:
        if args.dry_run:
            for record in records:
                print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            written_path = out_dir / "{:%Y-%m-%d}.jsonl".format(fetched_at)
            with written_path.open("a", encoding="utf-8", newline="\n") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False,
                                            sort_keys=True) + "\n")

        if not args.no_state and not args.dry_run:
            for record in records:
                seen.setdefault(record["id"], iso(fetched_at))
            save_seen(state_path, seen, config.state_retention_days)

    log("{} new item(s){}".format(
        len(records),
        " -> {}".format(written_path) if written_path else
        (" (dry run)" if args.dry_run else " (nothing written)")))

    if failures:
        log("{} of {} feed(s) failed".format(failures, len(active)))
        return 1 if failures == len(active) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
