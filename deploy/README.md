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

## Dependencies

Nothing has to be installed on either machine. Every stage is stdlib-only, and the one
third-party import in the repository is optional and confined to a single file:

| Where | Needs | Notes |
| --- | --- | --- |
| VPS | Python 3.12 (already present) | runs `fetcher/fetch_rss.py` only. `pip` is not installed on this host. |
| Workstation (scheduled) | Python 3.14 at `%LOCALAPPDATA%\Programs\Python\Python314` | `run_once.py` + the `rpi` package. Hardcoded as `$PythonExe` in `pull_and_run.ps1`, so a package installed into `.venv` does **not** reach the scheduled run. |
| Browser | nothing | `ui/index.html` and `data/rpi.json` are static. |
| `python -m rpi.calibrate` (manual) | optionally NumPy | the only file that reads NumPy; the hourly pipeline never imports it. |

**NumPy is an optimisation, not a requirement.** `rpi/calibrate.py` uses it for an
FFT-based autocorrelation when it is importable, and falls back to a naive loop when it is
not. Both produce identical numbers on every series checked, including the live snapshot
series - so installing NumPy changes only how long a manual calibration report takes, and
that is too quick to notice either way (about 3 ms at the current 615 snapshots, and
0.8 - 3.0 s even after a year of history, against 7.7 ms with NumPy). The measurements are
recorded in that file's docstring and in `REQUIREMENTS.md`.

Consequently: **do not install anything to "make the pipeline work".** If an `import numpy`
failure ever appears in a log, the tool will have already fallen back on its own.

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
51 * * * * /usr/bin/python3 /home/tr/rpi/fetcher/fetch_rss.py --out /home/tr/rpi/inbox >> /home/tr/rpi/logs/fetch.log 2>&1
```

### Why :51, not :00

The workstation runs its pipeline at :56. Firing the fetch five minutes earlier means the
pull always reads output from the current hour, rather than racing a fetch that started at
the same moment. Losing that race is not fatal — the pull tolerates a stale inbox — but it
quietly costs an hour of freshness on every run it loses, and nothing reports it.

The fetch itself takes about three seconds (measured: cron fires 07:55:00, the inbox file
and state are written by 07:55:03), and the workstation cycle about two minutes
(measured 2026-09-29: pull 1 s when nothing changed, pipeline 30-150 s depending on
the scoring backlog, publish and live check 17 s). The five-minute gap in front of
the pull is generous.

The minute is also load-bearing for the chart, and that part is easy to undo by accident.
The "Today" window is the current UTC day (`rpi/export.py`), and the page only draws a
window holding two or more points. Because the snapshot grid is fifteen minutes, a run that
lands within the first fourteen minutes of a UTC day publishes a one-point window, and the
page shows "not enough data for this window yet" for that hour — which is what a run at :00
did every day, since :00 Beijing is 00:00 UTC. At :56 the window holds four points, and a
run would have to overrun by more than three minutes to land inside the new day. Keep the
pipeline at minute 15 or later, and clear of the midnight boundary.

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

`deploy/pull_and_run.ps1` pulls, checks freshness, then runs the pipeline. Register it
against **PowerShell 7**, by full path:

```powershell
schtasks /Create /TN "RPI pipeline" /SC HOURLY /MO 1 /ST 00:56 /F `
  /TR '"C:\Program Files\PowerShell\7\pwsh.exe" -NoProfile -ExecutionPolicy Bypass -File d:\programs\rpi\deploy\pull_and_run.ps1'
```

The path needs the inner quotes: without them, `schtasks` splits on the space in
`Program Files` and the task ends up with `Execute` = `C:\Program` and the rest as
arguments. Verified by reading the stored action back, not by eye.

Run it manually to test:
`& "$env:ProgramFiles\PowerShell\7\pwsh.exe" -NoProfile -ExecutionPolicy Bypass -File deploy\pull_and_run.ps1`

Remove it with: `schtasks /Delete /TN "RPI pipeline" /F`

### Cap the run time

`schtasks` cannot set a run-time limit, so it is applied separately - and it is
not optional here. Task Scheduler refuses to start a second instance of a running
task (`MultipleInstances = IgnoreNew`), so one hung cycle silently cancels the
next: measured 2026-09-24, a publish stuck on a dead SSH connection for 2 h 11 min
and the 13:56 and 14:56 cycles never ran at all. The script kills its own pull and
publish at 120 s and 180 s (`-PullTimeoutSeconds`, `-PublishTimeoutSeconds`); this
limit is the backstop for anything else that hangs.

```powershell
$t = Get-ScheduledTask -TaskName "RPI pipeline"
$t.Settings.ExecutionTimeLimit = 'PT30M'
Set-ScheduledTask -TaskName "RPI pipeline" -Settings $t.Settings
```

30 minutes is ten times the longest healthy cycle, so it leaves room for a
catch-up run after an outage while still freeing the slot well before the next
hour.

`StartWhenAvailable` is deliberately left off: replaying a missed start whenever
the machine comes back would run the pipeline at an arbitrary minute, and the
minute is load-bearing (see "Why :51, not :00"). Whatever was missed is picked up
by the next scheduled cycle regardless, because ingestion is idempotent and the
inbox only accumulates.

### Why the task names the full pwsh path

`powershell` and `pwsh` are two different products, not two versions of one thing.
`powershell` is Windows PowerShell 5.1, a frozen OS component at
`System32\WindowsPowerShell\v1.0\powershell.exe`; `pwsh` is PowerShell 7.x, a separate
install at `Program Files\PowerShell\7\pwsh.exe`. Installing 7.x does not move the
`powershell` name, so this task ran 5.1 for its whole life while 7.6.6 sat unused beside
it, and nothing said so: the task definition reading `powershell` and the `StartBoundary`
of 2026-09-24 were the only traces.

That mattered, because 5.1 is where the bug below lives — `[datetime]::UnixEpoch`
evaluates to `$null` there — and where `Add-Content` writes the ANSI codepage, which is
why `Write-Log` goes through .NET instead. Both were diagnosed the hard way. The stored
path also keeps `PATH` out of it, since a scheduled task inherits a different environment
from an interactive shell.

The host is now logged at the top of every cycle, for the same reason:

```
2026-09-29 16:08:41  INFO    host: PowerShell 7.6.6 (C:\Program Files\PowerShell\7\pwsh.exe)
```

The script still runs correctly under 5.1, so `powershell -File` stays fine for
hand-testing — the encoding pinning and the epoch-from-parts workaround are kept for
exactly that case.

### Alerts under PowerShell 7: the toast is delegated

`Send-Alert` shows a Windows toast. WinRT cannot be loaded by PowerShell 7 at all:
`[Windows.UI.Notifications.ToastNotificationManager, ..., ContentType = WindowsRuntime]`
resolves under 5.1 and throws "type not found" under 7.6.6. There is no projection
assembly to fall back on either — a native 7.x toast needs `Microsoft.Windows.SDK.NET.dll`
and `WinRT.Runtime.dll` from the Windows App SDK, and this repository installs nothing.

So the toast code lives in `deploy/toast.ps1` and is invoked through the in-box
`powershell.exe`, which is an OS component rather than a dependency. Title and message
travel as `RPI_TOAST_TITLE` and `RPI_TOAST_MESSAGE` environment variables rather than
arguments: quoting a message containing parentheses, a timestamp and a full stop for both
`CreateProcess` and PowerShell at once is how an alert gets silently mangled. Delegating
also keeps the alert identical whichever host runs the pipeline — one code path, one
behaviour to test, rather than a version that works under 5.1 and a silent no-op under 7.

A toast is best-effort and never the record: `logs/alerts.log` is written before it is
attempted, and the toast has its own 30 s deadline so a stuck one cannot hold the cycle.

### Why the pull is incremental, not a wildcard

The pull used to be a single `scp 'go4pro:rpi/inbox/*.jsonl' inbox`, which
re-sent the **entire retained inbox** on every hourly cycle. That is correct and
harmless while the inbox is small, and it stops being either as soon as it is not:

| | |
| --- | --- |
| Retained inbox on 2026-09-29 | 7 files, 1.7 MB |
| Time to transfer all of it | ~300 s (measured, twice) |
| `-PullTimeoutSeconds` | 120 s |

So the pull was killed by its own deadline on **every** cycle, the inbox was never
updated, and the index froze — while the fetcher on the VPS was perfectly healthy
and had been writing items the whole time. The staleness check did its job and
alerted correctly ("No new items for 427 min"); the alert just pointed at cron,
because the message describes the symptom the check can see, not the pull that
caused it.

Nothing was wrong with the fetcher, the VPS, the network, or the deadline. The
deadline was the only thing behaving as designed; it was timing out on work that
did not need doing. Note that the original 11 s measurement was honest — the
inbox was simply a fraction of its present size, and the wildcard made the cost
of every cycle grow with retention while the deadline stayed fixed.

The pull now asks the VPS which files are missing or newer (size **and** mtime)
and transfers only those, so a cycle costs one `ssh` listing plus at most one
~80 KB file. Measured after the change: 1 s when the inbox is already current,
against ~300 s before, and it no longer grows as history accumulates. The fetcher
only ever appends to the file for the current UTC day, so nothing older than
yesterday can change.

Two details are load-bearing and easy to undo by accident:

* **`[datetime]::UnixEpoch` is unavailable in Windows PowerShell 5.1** (it arrived in
  .NET Core 2.1), and evaluates to `$null` there rather than failing loudly. This was
  live, not hypothetical: the scheduled task ran 5.1 until 2026-09-29 (see "Why the task
  names the full pwsh path"), so every comparison silently failed and every file was
  re-pulled — the exact behaviour this step exists to prevent. The epoch is therefore
  built from parts and pinned to UTC.
* **The size must travel with the file name.** Carrying a bare name left `$size`
  pointing at the last entry of the listing, so the transfer log reported every
  file with the same wrong "before" size.

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
& "$env:ProgramFiles\PowerShell\7\pwsh.exe" -NoProfile -ExecutionPolicy Bypass -File deploy\publish_ui.ps1 -Verify
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
| Scoring service down | analysis skipped, index rebuilt from stored scores; the backlog stays in the export, so the health check alerts |
| An item fails to score | recorded per item and retried automatically after 30 min, up to 3 attempts; then *parked* and alerted as its own condition |
| Pull or publish hangs | killed at its own deadline (120 s and 180 s), logged as WARN; the cycle still finishes and the next one runs |
| The pull is slower than its deadline | the transfer is incremental, not a wildcard - see below - so it does not grow with inbox history |
| Fetcher emits nothing | normal; ingestion is idempotent and finds no new items |
| Publish fails | logged as WARN; the local chart is still correct and the next cycle retries |
| A toast cannot be shown | `logs/alerts.log` was already written, so the record survives; the popup is best-effort and, because WinRT is unavailable in PowerShell 7, is raised by the in-box 5.1 instead |

Nothing fails silently, which matters because every stage is quiet on success.

The two ways an item can be missing are published separately, because they need
different responses: `summary.pending` is work the analyser is expected to
consume (a number that does not fall means the service is stuck), while
`summary.failed` is work the retry policy has given up on and which only
`run_once.py --retry-failed` will attempt again. Folding the second into the
first is exactly what made the backlog permanently non-zero after the
2026-09-24 CUDA fault, which turned the health check into noise.

Alerts are throttled per alert type, one `state/last-alert-<key>.txt` each, so a condition
that persists notifies once every 6 hours without a second, unrelated problem being
swallowed behind it. Those stamps, and the lines in `logs/alerts.log`, are UTC: the throttle
arithmetic is UTC-to-UTC, and a stamp in the future is treated as expired, so a clock change
can cost an extra alert but never hours of silence. The staleness check needs this to be
meaningful: if the fetcher dies,
nothing is left pending, so the scoring-service health check stays silent and the stale
inbox would otherwise be the only trace.

## Logs

* VPS: `~/rpi/logs/fetch.log`
* Workstation: `logs/pipeline-YYYY-MM-DD.log` and `logs/publish-YYYY-MM-DD.log`

Both are host-local and safe to delete; neither is a source of truth. The database
(`db/rpi.sqlite3`) is the only thing that matters, and it is the one thing that must be
backed up.
