# Fetcher

Reads news from RSS/Atom feeds and writes normalised items as JSON Lines into
an inbox directory. Deliberately **stdlib-only** so it can be dropped onto a
Linux box and run from cron with no virtualenv and no `pip install`.

```
feeds  --fetch-->  inbox/YYYY-MM-DD.jsonl  --sync-->  analyser (Windows)
```

The fetcher never talks to the model. It only produces news.

## Configuration

Everything about *which* sources are read lives in `feeds.json`. No code
changes are needed to add, remove or swap a source.

```json
{
  "feeds": [
    { "name": "chinanews-scroll", "url": "https://.../scroll-news.xml", "enabled": true },
    { "name": "solidot",          "url": "https://www.solidot.org/index.rss", "enabled": false }
  ],
  "max_items_per_feed": 20,
  "max_summary_chars": 1000,
  "max_title_chars": 300,
  "timeout": 20,
  "state_retention_days": 30
}
```

| Key | Meaning |
| --- | --- |
| `feeds[].name` | Label written to the `source` field; use something stable and short |
| `feeds[].url` | http(s), `file://`, or a local path |
| `feeds[].enabled` | `false` keeps a source in the file without fetching it |
| `feeds[].max_items` | Optional per-feed cap, overriding the global one |
| `max_summary_chars` | Hard cap per summary. Keep titles + summaries within roughly 1,400 characters so the analyser stays inside the API's 2,048-token prompt limit |
| `state_retention_days` | How long emitted item ids are remembered for de-duplication |

`enabled` exists so phase 1 (one source) and phase 2 (several sources) share a
single config file — turning on a second source is a one-word edit.

Inspect the resolved configuration at any time:

```powershell
python fetcher/fetch_rss.py --list-feeds
```

## Usage

```powershell
# Normal run - reads enabled feeds, appends to inbox/
python fetcher/fetch_rss.py

# See what would be written, without touching disk
python fetcher/fetch_rss.py --dry-run --no-state

# Test one candidate feed before adding it to the config
python fetcher/fetch_rss.py --feed https://example.com/rss.xml --dry-run --no-state --max-items 3

# Use a different config / output location
python fetcher/fetch_rss.py --config prod-feeds.json --out /srv/rpi/inbox
```

`--feed` overrides `feeds.json` entirely and always treats its argument as
enabled — handy for probing a source before committing it to the config.

Exit codes: `0` success (partial feed failures still return `0`), `1` all feeds
failed, `2` no feeds configured.

## Evaluating a candidate feed

**English sources are preferred** — see `REQUIREMENTS.md > RSS source(s)` for the
rationale and the current verdict table. Reachability is not the same as
usefulness, and both vary by network, so check all three before trusting a new
source:

1. **Reachable** — does it return a document at all?
2. **Current** — are the `published` dates actually recent? A feed can return
   HTTP 200 while serving content frozen years ago.
3. **Usable** — is the summary real article text, or empty/navigation boilerplate?

Four of the sources tested on 2026-09-23 passed reachability and still failed this
test, each for a different reason: `people.com.cn` (frozen at 2025-06-05),
Xinhua English (frozen at **2017-2018**), Global Times (descriptions empty —
titles only), and Sixth Tone (**no dates at all**, so no timeline is possible).
A 200 response is therefore not evidence that a feed is good.

Al Jazeera is **not** reachable from this network, despite being a commonly
suggested source. So are SCMP, BBC, Reuters, NYT, CNA and Caixin Global.

Feeds behind a redirect or bot challenge usually return HTML, which now fails
with a deliberately explicit error: `server returned HTML, not a feed
(redirect or bot challenge?)`. That message is the expected outcome for a
blocked source, not a bug.

## Output contract

One JSON object per line, oldest first, keys sorted. Appended to
`inbox/YYYY-MM-DD.jsonl` (UTC date), so daily files can be synced
independently.

| Field | Notes |
| --- | --- |
| `id` | `sha256` of the canonical link — the **idempotency key**. Re-running never re-emits an item the analyser has already seen |
| `source` | Feed name from the config |
| `source_url` | Feed the item came from |
| `title` | Cleaned headline |
| `summary` | Cleaned, whitespace-collapsed, truncated description |
| `link` | Canonicalised URL — fragment and tracking params (`utm_*`, `fbclid`, …) stripped |
| `published` | RFC 3339 UTC, or `null` if the feed had no usable date |
| `fetched_at` | RFC 3339 UTC time of this fetch |
| `schema` | Inbox record schema version |

Stripping tracking params means the same article arriving from two different
feeds collapses onto **one** `id` for free, before any model-assisted
de-duplication is needed.

## Cron

```cron
55 * * * * cd /srv/rpi && /usr/bin/python3 fetcher/fetch_rss.py --config fetcher/feeds.json --out inbox >> /var/log/rpi-fetch.log 2>&1
```

Hourly, not more often: measured feed publication rates (see `tools/feed_cadence.py`) show
the shortest interval that loses nothing is 8.3 h for the fastest enabled feed, and three
publishers declare a `ttl` of 15-20 minutes, which is a **floor** — do not poll faster than
that. Full reasoning in `deploy/README.md`.

The fetch sits at `:55` rather than `:00` so that anything pulling from it on the hour reads
a file from the current hour instead of racing the fetch itself.

Then sync the inbox to the analyser, e.g.:

```cron
5 * * * * rsync -a /srv/rpi/inbox/ rpi-host:/srv/rpi/inbox/
```

Five minutes after the fetch, so the copy never lands on a file that is being written.

Keep `state/` **out** of the sync — it is local bookkeeping, and sharing it
across hosts would suppress legitimate items.

## Tests

Offline fixtures cover RSS 2.0, Atom, escaped HTML, CDATA, script tags, missing
dates and tracking-parameter stripping:

```powershell
python fetcher/fetch_rss.py --feed fetcher/tests/sample_rss.xml --feed fetcher/tests/sample_atom.xml --dry-run --no-state
```
