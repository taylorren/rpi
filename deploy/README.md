# Deployment

Two machines, one data direction.

```
  VPS (go4pro.org:22220)                      Windows workstation
  ─────────────────────                       ───────────────────
  fetch_rss.py    hourly                      pull_and_run.ps1   hourly
        │                                            │
        ▼                                            ▼
  ~/rpi/inbox/*.jsonl  ───── scp (pull) ──────►  inbox/*.jsonl
  ~/rpi/state/            never synced           db/rpi.sqlite3
  ~/rpi/logs/                                     data/rpi.json + ui/

  nginx :443  ◄──── scp (publish) ─────  data/rpi.json + ui/index.html
  ~/rpi/public/
        │
        ▼
  https://rpi.go4pro.org   (public, read-only)
```

Three stages, and the analyser sits in the middle: news flows out of the VPS, the
workstation analyses it, and only the finished static page flows back.

## Why this shape

* **The fetcher needs the VPS for network access.** It runs on a mainland-China
  workstation during development, where much of the news is unreachable, but production
  has no such restriction. Verified: `--check-sources` passes for all 10 configured feeds
  from the VPS, and reports `proxy: none (auto-detected nothing)`.
* **The analyser needs the GPU.** The scoring model is a local HTTP service on port 8765;
  it is not exposed and should not be.
* **Pull, not push.** The workstation has no SSH server, so it initiates the transfer.
* **`state/` must never be synced.** It records which item ids have already been emitted.
  Sharing it across hosts — or restoring it from a backup — would suppress legitimate news
  as "already seen". The same applies to a stale copy left in the repo.

## Prerequisites

* SSH key login to the VPS for user `tr`, port 22220, using `~/.ssh/id_ed25519`.
  An alias in `~/.ssh/config` keeps the port in one place:

  ```
  Host go4pro
    HostName go4pro.org
    Port 22220
    User tr
    IdentityFile ~/.ssh/id_ed25519
  ```

* **Key auth is required, not optional.** Cron runs unattended and cannot type a password.
* The scoring service must be running locally before analysis can do anything:
  `.venv\Scripts\python.exe .\nimble_serve.py --quant 4bit --port 8765`.
  If it is down the pipeline still runs and rebuilds the index from stored scores, logging
  a warning rather than failing.

## Deploying the fetcher to the VPS

```powershell
ssh go4pro 'mkdir -p ~/rpi/fetcher ~/rpi/inbox ~/rpi/state ~/rpi/logs'
scp fetcher\fetch_rss.py fetcher\feeds.json go4pro:rpi/fetcher/
```

Verify the source landscape **from the VPS** — verdicts measured on the workstation do not
transfer, because that is the whole reason the fetcher is remote:

```powershell
ssh go4pro 'python3 $HOME/rpi/fetcher/fetch_rss.py --check-sources'
```

Python 3.12 is present on the target. The fetcher is stdlib-only, so nothing needs
installing — no venv, no `pip`.

## VPS cron

Installed as:

```cron
55 * * * * /usr/bin/python3 /home/tr/rpi/fetcher/fetch_rss.py --out /home/tr/rpi/inbox >> /home/tr/rpi/logs/fetch.log 2>&1
```

### Why :55, not :00

The workstation runs its pipeline on the hour. Firing the fetch five minutes earlier means
the pull always reads output from the current hour, rather than racing a fetch that started
at the same moment. Losing that race is not fatal — the pull tolerates a stale inbox — but
it quietly costs an hour of freshness on every run it loses, and nothing reports it.

### Why hourly (measured, `tools/feed_cadence.py`)

Polling frequency does **not** decide whether a story is captured — the feed's own
backlog does. Each poll takes the newest `max_items` (20) per feed, so nothing is lost as
long as fewer than 20 new items appear between polls. Measured publication rates give the
longest interval that still misses nothing:

| Feed | Rate | Cap covers | Safe interval |
| --- | --- | --- | --- |
| cgtn-world | 0.49/h | 40.6 h | 40.6 h |
| guardian-world | 0.32/h | 62.5 h | 62.5 h |
| bbc-world | 0.83/h | 24.2 h | 24.2 h |
| aljazeera-all | 2.42/h | 8.3 h | **8.3 h** (tightest enabled) |

Hourly sits **8x inside** that bound. Two further points:

* **Publisher `ttl` is a floor, not a target.** BBC declares 15 min, CNA and solidot 20 min.
  Those say "do not poll faster than this", so polling *faster* than 15 min would be
  impolite; anything slower is fine.
* **Feed lag dominates our lag.** The newest item at the source is already 0.2-4.8 h old,
  so adding up to an hour changes little. Only BBC and Al Jazeera (0.2-0.3 h) would notice.

A slower poll does not damage history either: snapshots are recomputed from event times on
every run, so a late-arriving item is backfilled into the correct position. Only the live
right edge of the chart is affected.

Do not reduce below 15 minutes — that is the one firm line, set by the publishers' `ttl`.
Re-measure with `python tools/feed_cadence.py` after adding sources.

Reinstall from `deploy/crontab.txt` if needed. **That file must keep LF line endings** —
a CRLF crontab fails in confusing ways. It was generated with `[System.IO.File]::WriteAllText`
for exactly this reason; do not edit it in a Windows editor that adds CRs.

To install: `scp deploy\crontab.txt go4pro:crontab.new` then
`ssh go4pro 'crontab ~/crontab.new && rm ~/crontab.new'`.

Watch it: `ssh go4pro 'tail -20 ~/rpi/logs/fetch.log'`.
The log grows ~3 MB/year at 24 runs a day; rotate or truncate it occasionally.

## Workstation schedule

`deploy/pull_and_run.ps1` pulls, checks freshness, then runs the pipeline. Register it:

```powershell
schtasks /Create /TN "RPI pipeline" /SC HOURLY /MO 1 /ST 00:00 /F `
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File d:\programs\rpi\deploy\pull_and_run.ps1"
```

Run it manually to test: `powershell -NoProfile -ExecutionPolicy Bypass -File deploy\pull_and_run.ps1`

Remove it with: `schtasks /Delete /TN "RPI pipeline" /F`

## Publishing the UI

The front end is static: `ui/index.html` plus `data/rpi.json`, with no server-side code.
`deploy/publish_ui.ps1` copies exactly those two files to `~/rpi/public/` and is called
automatically at the end of every `pull_and_run.ps1` cycle (`-SkipPublish` disables it).

Layout served:

```
/home/tr/rpi/public/index.html
/home/tr/rpi/public/data/rpi.json
```

**Publishing needs no root.** The tree lives under `/home/tr`, which the deploying user
already owns, and nginx only needs traverse permission on `/home/tr` and `/home/tr/rpi`.
Explicit `chmod` runs after every upload, because `scp` inherits the remote umask and a
restrictive one would leave files mode 600 — which surfaces as an unexplained 403 rather
than anything pointing at permissions.

### One-time nginx setup (needs sudo)

The site config is in `deploy/nginx-rpi.go4pro.org.conf`, mirroring the existing
`wiki.go4pro.org` site on the same host. It is staged at `~/rpi/nginx-rpi.go4pro.org.conf`:

```bash
sudo cp ~/rpi/nginx-rpi.go4pro.org.conf /etc/nginx/sites-available/rpi
sudo ln -sf /etc/nginx/sites-available/rpi /etc/nginx/sites-enabled/rpi
sudo nginx -t          # check BEFORE reloading; this host also serves default/webmail/wiki
sudo systemctl reload nginx
```

Confirm plain HTTP serves before adding TLS:

```bash
curl -I http://rpi.go4pro.org      # expect 200
```

Then TLS, which also adds the HTTP-to-HTTPS redirect:

```bash
sudo certbot --nginx -d rpi.go4pro.org
```

certbot needs port 80 reachable for the HTTP-01 challenge, and nginx already listens there.

### Verifying end to end

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\publish_ui.ps1 -Verify
```

`-Verify` fetches the public URL and reports the level, item count and duplicates merged
from the served JSON — so it confirms the whole chain, not just that a file was uploaded.
Before nginx is configured it reports a warning rather than failing, since a TLS trust
error is the expected outcome when only the default server answers.

## Failure behaviour

| Failure | Result |
| --- | --- |
| VPS unreachable | pull logged as WARN; pipeline still runs on existing data |
| VPS cron dead | logged as WARN *and* alerted once the freshest `fetched_at` is > 150 min old (2.5 cycles) |
| Scoring service down | analysis skipped, index rebuilt from stored scores |
| Fetcher emits nothing | normal; ingestion is idempotent and finds no new items |
| Publish fails | logged as WARN; the local chart is still correct and the next cycle retries |

Nothing fails silently, which matters because every stage is quiet on success.

Alerts are throttled per alert type, one `state/last-alert-<key>.txt` each, so a condition
that persists notifies once every 6 hours without a second, unrelated problem being
swallowed behind it. The staleness check needs this to be meaningful: if the fetcher dies,
nothing is left pending, so the scoring-service health check stays silent and the stale
inbox would otherwise be the only trace.

## Logs

* VPS: `~/rpi/logs/fetch.log`
* Workstation: `logs/pipeline-YYYY-MM-DD.log` and `logs/publish-YYYY-MM-DD.log`

Both are host-local and safe to delete; neither is a source of truth. The database
(`db/rpi.sqlite3`) is the only thing that matters, and it is the one thing that must be
backed up.
