<#
.SYNOPSIS
    Pull the news inbox from the VPS and run the RPI pipeline.

.DESCRIPTION
    Intended to be run on a schedule (hourly). Each invocation:

      1. copies inbox/*.jsonl from the VPS (the machine with unobstructed
         network access) into the local inbox,
      2. warns if the freshest item in the inbox is stale, which is the symptom
         of a dead fetcher on the VPS rather than a local fault,
      3. runs the pipeline (ingest -> dedupe -> analyse -> calculate -> export),
      4. publishes the static page back to the VPS and verifies it.

    Design notes:

    * A failed pull is NOT fatal. The pipeline still runs on whatever has
      already been ingested, because a stale chart beats no chart. It is
      logged loudly instead.
    * The VPS is only ever read, never written. Its ``state/`` directory is
      host-local bookkeeping and must never be synced, or legitimate items
      would be suppressed.
    * Absolute paths throughout: Task Scheduler starts with a different
      working directory and environment than an interactive shell.
    * Every external command has a hard deadline. Measured 2026-09-24: one
      publish blocked for 2 h 11 min on a stalled SSH connection, and because
      Task Scheduler will not start a second instance of a running task, the
      following two hourly cycles never ran at all.

.PARAMETER SkipPull
    Run the pipeline only, without contacting the VPS.

.PARAMETER SkipPublish
    Do not push the regenerated UI back to the VPS.

.PARAMETER StaleMinutes
    Warn if the newest inbox file is older than this. Default 150, i.e. 2.5 missed
    hourly cycles. Must stay comfortably above the fetch interval or it will warn
    on every run - it was 45 when the schedule was every 15 minutes.

.PARAMETER PullTimeoutSeconds
    Deadline for the inbox copy. Default 120 s, against a measured ~11 s.

.PARAMETER PublishTimeoutSeconds
    Deadline for the whole publish step, including its scp calls. Default 180 s,
    against a measured ~17 s.
#>
[CmdletBinding()]
param(
    [switch] $SkipPull,
    [switch] $SkipPublish,
    [int]    $StaleMinutes = 150,
    [int]    $PullTimeoutSeconds = 120,
    [int]    $PublishTimeoutSeconds = 180
)

$ErrorActionPreference = 'Continue'

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe   = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'
$SshHost     = 'go4pro'          # alias from ~/.ssh/config (port 22220, user tr)
$InboxDir    = Join-Path $ProjectRoot 'inbox'
$LogDir      = Join-Path $ProjectRoot 'logs'

$logFile = Join-Path $LogDir ('pipeline-{0:yyyy-MM-dd}.log' -f (Get-Date))
New-Item -ItemType Directory -Force -Path $LogDir, $InboxDir | Out-Null

# Written through .NET rather than Add-Content, whose default encoding depends on
# the host. Measured: under Windows PowerShell (which the scheduled task uses)
# Add-Content writes the ANSI codepage and a zero-width space - which Guardian
# headlines are full of - becomes a literal "?"; PowerShell 7 writes UTF-8.
# Pinning it here makes the log byte-identical whichever host runs it.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Log {
    param([string] $Message, [string] $Level = 'INFO')
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1,-7} {2}' -f (Get-Date), $Level, $Message
    [System.IO.File]::AppendAllText($logFile, $line + "`n", $utf8NoBom)
    Write-Host $line
}

if (-not (Test-Path $PythonExe)) {
    Write-Log "python not found at $PythonExe - check the PythonExe variable" 'ERROR'
    exit 2
}

# Belt and braces alongside rpi/__init__.py's stream reconfiguration. Without
# this, a headline containing an unusual character crashes the run with a
# UnicodeEncodeError under a legacy console codepage (GBK here).
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# And the read-back side. The child's output is UTF-8, so ask for UTF-8 rather
# than trusting whatever the console codepage happens to be: under GBK a
# zero-width space comes back as two stray CJK characters. PowerShell 7 already
# defaults to this; 5.1 does not. A console-less process - which is how the
# scheduled task runs - can refuse the set.
#
# This is not a complete fix, and is not claimed as one. 5.1 has still been
# observed mangling the decode with this pinned, while the scheduled task's own
# output was correct. So a mojibake headline in the log remains possible; it
# affects the log only and never the data, which is UTF-8 end to end.
try {
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
} catch {
    Write-Log "could not pin console output encoding: $($_.Exception.Message)" 'WARN'
}

# ---------------------------------------------------------------------------
# Alerting
#
# Defined here rather than alongside the health check further down, because the
# staleness check needs it too and PowerShell resolves functions in execution
# order. Delivery is layered and best-effort: a toast for immediacy, plus an
# append-only alert log that still records the problem if the toast cannot be
# shown. Repeats are throttled so a condition that persists does not notify
# every single hour.
#
# Timestamps here are UTC, tagged "Z", and that is not cosmetic. The first
# version wrote local time with a "Z" and read it back with [datetime]::Parse,
# which converts a trailing "Z" to local time - so the age came out negative
# ("alert throttled; last sent 2026-09-25 14:59:37Z (-7.0h ago)" in
# logs/pipeline-2026-09-25.log), the window became 6 h plus the UTC offset (14 h
# here), and every alert raised in its first 8 hours was swallowed. RoundtripKind
# and ToUniversalTime keep both sides of the comparison in UTC, and a stamp in
# the future - a clock change, or a leftover written in the old format - is
# treated as expired rather than trusted, so the failure mode is one extra alert
# instead of hours of silence.
# ---------------------------------------------------------------------------
$AlertEveryHours = 6
$alertDir = Join-Path $ProjectRoot 'state'
$alertLog = Join-Path $ProjectRoot 'logs\alerts.log'

function Send-Alert {
    param([string] $Title, [string] $Message)

    # One throttle per alert type. A single shared state file would let whichever
    # alert fired first silence every other one for the whole window, so a dead
    # fetcher could go unreported behind an unrelated scoring-service alert.
    $key = ($Title -replace '[^A-Za-z0-9]+', '-').Trim('-').ToLower()
    $alertState = Join-Path $alertDir "last-alert-$key.txt"

    $previous = $null
    if (Test-Path $alertState) {
        $raw = (Get-Content $alertState -Raw -ErrorAction SilentlyContinue)
        if ($raw) { $raw = $raw.Trim() }
        if ($raw) {
            # InvariantCulture because a scheduled task does not inherit an
            # interactive shell's culture; RoundtripKind so the trailing "Z"
            # stays UTC instead of being converted to local time (see above).
            try {
                $previous = [datetime]::Parse($raw,
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
            } catch { $previous = $null }
        }
    }
    # Throttle on a UTC-to-UTC age. A negative age is not "recently sent" - it
    # means the stored stamp is in the future - so it deliberately falls through
    # to the alert rather than suppressing it.
    $ageHours = $null
    if ($previous) {
        $ageHours = ((Get-Date).ToUniversalTime() - $previous).TotalHours
    }
    if ($null -ne $ageHours -and $ageHours -ge 0 -and
        $ageHours -lt $AlertEveryHours) {
        Write-Log ("alert throttled; last sent {0} ({1:N1}h ago)" -f `
                   $previous.ToString('u'), $ageHours) 'WARN'
        return
    }

    New-Item -ItemType Directory -Force -Path $alertDir,
        (Split-Path -Parent $alertLog) | Out-Null
    Add-Content -Path $alertLog -Value ('{0:u}  {1}  {2}' -f (Get-Date).ToUniversalTime(), $Title, $Message)
    [System.IO.File]::WriteAllText($alertState, (Get-Date).ToUniversalTime().ToString('u'))

    try {
        [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' +
                     $Title + '</text><text>' + $Message + '</text>' +
                     '</binding></visual></toast>')
        $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
        # PowerShell's own AppID; a toast from an unregistered AppID is dropped
        # silently, which is why the alert log exists as a fallback.
        $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
        Write-Log "ALERT sent: $Title - $Message" 'WARN'
    } catch {
        Write-Log "toast unavailable ($($_.Exception.Message)) - alert recorded in logs\alerts.log" 'WARN'
    }
}

# ---------------------------------------------------------------------------
# Bounded external commands
#
# ssh and scp can hang indefinitely on a connection that is alive at the TCP
# level but stuck above it: ServerAliveInterval keeps such a channel alive
# rather than abandoning it, and ConnectTimeout only guards the handshake.
# Measured 2026-09-24: a publish blocked for 2 h 11 min on exactly that, and
# because Task Scheduler refuses to start a second instance of a running task,
# the 13:56 and 14:56 cycles never ran at all. So every external command gets a
# deadline, and a timeout is treated as an ordinary failure: the pipeline is
# local and still produces a correct chart.
# ---------------------------------------------------------------------------

function ConvertTo-ArgumentToken {
    # CreateProcess parses a single command-line string, so an argument holding a
    # space has to be quoted the way the C runtime expects - otherwise a path
    # like "C:\Program Files\..." arrives as two arguments.
    param([string] $Value)
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashes = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq '\') { $backslashes++; continue }
        if ($ch -eq '"') {
            [void]$sb.Append('\' * ($backslashes * 2 + 1))
            [void]$sb.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes) {
            [void]$sb.Append('\' * $backslashes)
            $backslashes = 0
        }
        [void]$sb.Append($ch)
    }
    if ($backslashes) { [void]$sb.Append('\' * ($backslashes * 2)) }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Invoke-WithTimeout {
    param(
        [string]   $FilePath,
        [string[]] $ArgumentList,
        [int]      $TimeoutSeconds,
        [string]   $Label
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName  = $FilePath
    $psi.Arguments = (($ArgumentList |
        ForEach-Object { ConvertTo-ArgumentToken $_ }) -join ' ')
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true

    $process = [System.Diagnostics.Process]::Start($psi)
    # Drain both pipes before waiting. A transfer progress meter overruns the
    # 4 KB pipe buffer easily, and a full pipe would deadlock the child against a
    # parent still waiting for it to exit.
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()

    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill() } catch { }
        # Kill() ends the child, not a grandchild it spawned; taskkill /T sweeps
        # up whatever ssh or scp left behind. Harmless if it is already gone.
        try { & taskkill /T /F /PID $process.Id 2>&1 | Out-Null } catch { }
        Write-Log "$Label did not finish within ${TimeoutSeconds}s - killed it" 'WARN'
        return [pscustomobject]@{ TimedOut = $true; ExitCode = $null; Output = @() }
    }

    $lines = @()
    foreach ($text in @($stdout.Result, $stderr.Result)) {
        if ($text) {
            $lines += @(($text -split "`r?`n") | Where-Object { $_ -ne '' })
        }
    }
    return [pscustomobject]@{
        TimedOut = $false
        ExitCode = $process.ExitCode
        Output   = $lines
    }
}

Write-Log '--- run start ---'

# ---------------------------------------------------------------------------
# 1. Pull from the VPS
# ---------------------------------------------------------------------------
if (-not $SkipPull) {
    # -p preserves the source mtime. Without it scp stamps every file with the
    # transfer time instead, which made the staleness check below read ~0
    # minutes on every successful run.
    #
    # ServerAliveInterval/CountMax detect a stalled connection that ConnectTimeout
    # alone cannot, but only when the peer is genuinely dead - a live-but-stuck
    # channel is kept alive, which is why this call carries a deadline too.
    $pull = Invoke-WithTimeout -FilePath 'scp' -Label 'pull' `
        -TimeoutSeconds $PullTimeoutSeconds -ArgumentList @(
            '-p', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
            '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
            "${SshHost}:rpi/inbox/*.jsonl", $InboxDir)
    $pullText = ($pull.Output -join ' ')
    if ($pull.TimedOut) {
        Write-Log 'continuing with whatever is already in the inbox' 'WARN'
    } elseif ($pull.ExitCode -ne 0) {
        Write-Log "pull from ${SshHost} FAILED (exit $($pull.ExitCode)): $pullText" 'WARN'
        Write-Log 'continuing with whatever is already in the inbox' 'WARN'
    } else {
        Write-Log "pull ok: $pullText"
    }
}

# ---------------------------------------------------------------------------
# 2. Staleness check
#
# The fetcher runs on a schedule, so an old inbox means something upstream has
# stopped - the machine itself is fine. Without this, the chart would simply
# freeze and nothing would say why.
#
# Two signals, in order of trust:
#
#   * fetched_at, carried inside the records. That is the fetch run's own UTC
#     clock, so it is what "the fetcher is alive" actually means, and no amount
#     of copying can falsify it.
#   * the file's mtime, as a fallback - usable only because the pull passes -p.
#     Plain scp stamps every file with the transfer time, which made this check
#     read ~0 minutes on every run, so it could never fire for the failure it
#     was written to catch.
#
# Caveat on both: the fetcher writes nothing when a run finds no new items, so a
# quiet stretch and a dead fetcher look identical from here. At the measured
# rate (~4 new items/hour across the four feeds) a 2.5-hour gap with nothing new
# is about a 1-in-26,000 event, so the threshold is left alone rather than
# inventing a heartbeat file to remove it.
# ---------------------------------------------------------------------------
$ageScript  = Join-Path $ProjectRoot 'tools\inbox_age.py'
$ageMinutes = $null
$ageDetail  = ''

if (Test-Path $ageScript) {
    $ageOutput = & $PythonExe $ageScript 2>&1
    if ($LASTEXITCODE -eq 0 -and $ageOutput) {
        $fields = (($ageOutput | Out-String).Trim()) -split '\s+'
        $parsed = 0.0
        # Invariant culture: under a comma decimal separator "24.7" would
        # otherwise parse as 247.
        if ($fields.Count -ge 1 -and [double]::TryParse($fields[0],
                [Globalization.NumberStyles]::Float,
                [Globalization.CultureInfo]::InvariantCulture, [ref] $parsed)) {
            $ageMinutes = $parsed
            $ageDetail = "newest fetched_at $($fields[-1])"
        }
    }
    if ($null -eq $ageMinutes) {
        Write-Log "inbox_age.py gave no usable age ($ageOutput) - using mtime" 'WARN'
    }
} else {
    Write-Log "missing $ageScript - using mtime" 'WARN'
}

if ($null -eq $ageMinutes) {
    $newest = Get-ChildItem -Path $InboxDir -Filter '*.jsonl' -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($null -eq $newest) {
        Write-Log 'no inbox files at all - has the fetcher ever run?' 'WARN'
    } else {
        $ageMinutes = ((Get-Date) - $newest.LastWriteTime).TotalMinutes
        $ageDetail = "mtime of $($newest.Name)"
    }
}

if ($null -ne $ageMinutes) {
    $ageMinutes = [math]::Round($ageMinutes, 1)
    if ($ageMinutes -gt $StaleMinutes) {
        $staleMsg = ("No new items for {0} min ({1}). The index is frozen at the " +
                     "last fetch - check cron and logs/fetch.log on the VPS.") -f `
                    $ageMinutes, $ageDetail
        Write-Log "stale inbox: $staleMsg" 'WARN'
        # Worth a real notification rather than only a log line: if the fetcher
        # dies, nothing is pending, so the health check below stays silent and
        # this would otherwise be the only trace of it.
        Send-Alert -Title 'RPI: fetcher on the VPS looks dead' -Message $staleMsg
    } else {
        Write-Log "freshest item is $ageMinutes min old ($ageDetail)"
    }
}

# ---------------------------------------------------------------------------
# 3. Pipeline
# ---------------------------------------------------------------------------
$runOutput = & $PythonExe (Join-Path $ProjectRoot 'run_once.py') 2>&1
$runOutput | ForEach-Object { Write-Log "  $_" }

if ($LASTEXITCODE -ne 0) {
    Write-Log "pipeline exited $LASTEXITCODE" 'ERROR'
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------------
# 4. Health check
#
# This is the one failure that every other stage hides. If the scoring service
# stops, ingest, dedupe, calculate and export all keep succeeding - the chart
# just quietly stops changing. The exported JSON carries the backlog, and a
# backlog that persists is the signature, so that is what gets checked.
#
# Send-Alert itself is defined up in the configuration block, because the
# staleness check in step 2 needs it too.
# ---------------------------------------------------------------------------
$exportPath = Join-Path $ProjectRoot 'data\rpi.json'
if (Test-Path $exportPath) {
    try {
        # Read as explicit UTF-8, NOT Get-Content -Raw. The export is written
        # BOM-less UTF-8 and this script runs under Windows PowerShell, whose
        # Get-Content decodes a BOM-less file with the ANSI codepage (GBK here).
        # Headlines carry non-ASCII (curly quotes, "Niño", zero-width spaces),
        # so the bytes mis-decode - and because a stray byte can be swallowed as
        # a GBK trail byte, a structural quote disappears and the parse fails
        # with "Invalid object passed in, ':' or '}' expected".
        #
        # Measured 2026-09-23: this made the check below throw on every run from
        # 15:47 onwards, so a three-hour scoring-service outage alerted nobody -
        # state/last-alert-* did not exist and logs/alerts.log was never created.
        # A health check that cannot fail loudly is worse than none, hence UTF-8.
        $export = [System.IO.File]::ReadAllText($exportPath,
            [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        # Waiting work means the analyser did not consume what was there, which
        # after a run that had the chance to is the signature of a scoring
        # service that is down or stuck. The gauge excludes items parked by the
        # retry policy, so this can reach zero on a healthy system and mean
        # something when it does not.
        $pending = [int]$export.summary.pending
        if ($pending -gt 0) {
            Send-Alert -Title 'RPI: scoring service appears offline' `
                -Message ("{0} item(s) waiting to be analysed. The index is frozen and will not reflect them." -f $pending)
        } else {
            Write-Log "health ok: no analysis backlog"
        }
        # Separate condition, separate alert type and throttle: these items are
        # missing from the index for good unless someone forces a retry, so
        # folding them into the backlog above (where they cannot change) is how
        # the 2026-09-24 CUDA batch went unreported.
        $parked = [int]$export.summary.failed
        if ($parked -gt 0) {
            Send-Alert -Title 'RPI: items failed to score' `
                -Message ("{0} item(s) failed to score and are missing from the index. They will not be retried again unless run_once.py --retry-failed is used." -f $parked)
        }
    } catch {
        # ConvertFrom-Json quotes the offending input back at you, which for this
        # file means the whole 69 KB document lands in a single log line. Keep the
        # reason, drop the payload.
        $reason = "$($_.Exception.Message)"
        if ($reason.Length -gt 200) { $reason = $reason.Substring(0, 200) + ' ...' }
        Write-Log "could not read $exportPath for the health check: $reason" 'WARN'
    }
} else {
    Write-Log "no export at $exportPath; skipping health check" 'WARN'
}

# ---------------------------------------------------------------------------
# 5. Publish
#
# The UI is static and reads a JSON file, so publishing is a two-file copy. It
# runs through a child PowerShell process on purpose: publish_ui.ps1 calls
# ``exit``, and in-process that would terminate this script too.
#
# A publish failure does not invalidate the run - the local chart stays correct
# - so it is a warning, not an error.
# ---------------------------------------------------------------------------
if (-not $SkipPublish) {
    $publishScript = Join-Path $PSScriptRoot 'publish_ui.ps1'
    # The deadline covers the whole step, including the ssh and scp calls inside
    # publish_ui.ps1 - the hang measured on 2026-09-24 was one of those.
    $publish = Invoke-WithTimeout -FilePath 'powershell' -Label 'publish' `
        -TimeoutSeconds $PublishTimeoutSeconds -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $publishScript,
            '-Verify')
    $publish.Output | ForEach-Object { Write-Log "  $_" }
    if ($publish.TimedOut) {
        Write-Log 'the local chart is still correct; the next cycle retries' 'WARN'
    } elseif ($publish.ExitCode -ne 0) {
        Write-Log "publish exited $($publish.ExitCode)" 'WARN'
    }
} else {
    Write-Log 'publish skipped (-SkipPublish)'
}

Write-Log '--- run ok ---'
exit 0
