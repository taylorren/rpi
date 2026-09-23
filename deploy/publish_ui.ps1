<#
.SYNOPSIS
    Push the static UI and its data to the VPS so they can be served publicly.

.DESCRIPTION
    Publishes exactly two files:

      ui/index.html      ->  <RemoteDir>/index.html
      data/rpi.json      ->  <RemoteDir>/data/rpi.json

    The page fetches ``../data/rpi.json`` or ``data/rpi.json`` at runtime, so this
    layout works unchanged.

    No root privileges are needed: the remote directory is inside the deploying
    user's home, which nginx can already read. Only the one-time nginx site
    configuration needs sudo - see deploy/nginx-rpi.go4pro.org.conf.

.PARAMETER SshHost
    SSH config alias for the VPS. Default 'go4pro'.

.PARAMETER RemoteDir
    Target directory on the VPS. Default '/home/tr/rpi/public'.

.PARAMETER Verify
    After publishing, fetch the public URL and confirm it serves fresh data.
    Only useful once nginx is configured; a failure here is reported as a
    warning, not an error.

.PARAMETER Url
    Public URL used for verification. Default 'https://rpi.go4pro.org'.
#>
[CmdletBinding()]
param(
    [string] $SshHost   = 'go4pro',
    [string] $RemoteDir = '/home/tr/rpi/public',
    [string] $Url       = 'https://rpi.go4pro.org',
    [switch] $Verify
)

$ErrorActionPreference = 'Continue'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$UiFile      = Join-Path $ProjectRoot 'ui\index.html'
$DataFile    = Join-Path $ProjectRoot 'data\rpi.json'
$LogDir      = Join-Path $ProjectRoot 'logs'
$logFile     = Join-Path $LogDir ('publish-{0:yyyy-MM-dd}.log' -f (Get-Date))

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log {
    param([string] $Message, [string] $Level = 'INFO')
    $line = '{0:yyyy-MM-dd HH:mm:ss}  {1,-7} {2}' -f (Get-Date), $Level, $Message
    Add-Content -Path $logFile -Value $line
    # Bare (untimestamped) line to stdout: pull_and_run.ps1 captures this output
    # and re-logs it with its own timestamp, so including one here would prefix
    # every publish line twice.
    Write-Host ('{0,-7} {1}' -f $Level, $Message)
}

foreach ($file in @($UiFile, $DataFile)) {
    if (-not (Test-Path $file)) {
        Write-Log "missing $file - run the pipeline first (python run_once.py)" 'ERROR'
        exit 2
    }
}

$dataAge    = [math]::Round(((Get-Date) - (Get-Item $DataFile).LastWriteTime).TotalMinutes, 1)
$dataSizeKb = [math]::Round((Get-Item $DataFile).Length / 1KB, 1)
Write-Log "publishing $dataSizeKb KB of data (${dataAge} min old) to ${SshHost}:${RemoteDir}"

# SSH keepalive: ServerAliveInterval detects a stalled connection that
# ConnectTimeout alone cannot. 15s interval x 3 misses = 45s to bail.
$sshOpts = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
              '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3')

# ---------------------------------------------------------------------------
# Ensure the target tree exists, then upload.
# ---------------------------------------------------------------------------
& ssh $sshOpts $SshHost "mkdir -p $RemoteDir/data"
if ($LASTEXITCODE -ne 0) {
    Write-Log "could not create $RemoteDir on $SshHost (exit $LASTEXITCODE)" 'ERROR'
    exit 1
}

& scp $sshOpts $UiFile "${SshHost}:${RemoteDir}/index.html"
if ($LASTEXITCODE -ne 0) {
    Write-Log 'index.html upload FAILED' 'ERROR'
    exit 1
}

& scp $sshOpts $DataFile "${SshHost}:${RemoteDir}/data/rpi.json"
if ($LASTEXITCODE -ne 0) {
    Write-Log 'rpi.json upload FAILED' 'ERROR'
    exit 1
}

# scp inherits the remote umask. With a restrictive umask the files would land
# mode 600 and nginx (running as www-data) could not read them, which presents
# as an unexplained 403, so permissions are set explicitly.
& ssh $sshOpts $SshHost "chmod 755 $RemoteDir $RemoteDir/data; chmod 644 $RemoteDir/index.html $RemoteDir/data/rpi.json"
if ($LASTEXITCODE -ne 0) {
    Write-Log 'chmod failed - nginx may be unable to read the files' 'WARN'
}

Write-Log 'published index.html and data/rpi.json'

# ---------------------------------------------------------------------------
# Optional end-to-end verification
# ---------------------------------------------------------------------------
if ($Verify) {
    try {
        $response = Invoke-WebRequest -Uri "$Url/data/rpi.json?t=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())" `
            -UseBasicParsing -TimeoutSec 20
        $payload = $response.Content | ConvertFrom-Json
        $remoteLevel   = $payload.summary.level
        $remoteItems   = $payload.summary.volume_total
        $remoteDupes   = $payload.summary.duplicates_removed
        Write-Log ("live check OK: HTTP {0}, level {1:N3}, {2} item(s), {3} dupe(s) merged" -f `
                   $response.StatusCode, $remoteLevel, $remoteItems, $remoteDupes)
    } catch {
        Write-Log "live check FAILED for $Url - is nginx configured yet? $($_.Exception.Message)" 'WARN'
    }
}

exit 0
