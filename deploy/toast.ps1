<#
.SYNOPSIS
    Show a Windows toast notification for an RPI pipeline alert.

.DESCRIPTION
    A separate script, and deliberately run under Windows PowerShell 5.1, for one
    reason: WinRT.

    ToastNotificationManager lives in the Windows Runtime, and PowerShell 7
    cannot load WinRT types at all. Measured on this machine (pwsh 7.6.6 against
    5.1.26100):

        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,
         ContentType = WindowsRuntime]

        5.1 -> loads
        7.x -> "type not found"

    There is no projection assembly to fall back on either. A native 7.x toast
    needs Microsoft.Windows.SDK.NET.dll and WinRT.Runtime.dll from the Windows App
    SDK, so doing it in-process would mean installing something purely to raise an
    alert. This repository installs nothing (see "Dependencies" in
    deploy/README.md), so instead the toast goes through the in-box
    powershell.exe: an OS component present on every Windows install, not a
    dependency.

    Keeping it here also keeps the toast on a single code path. It no longer
    matters which host runs the pipeline - the alert is raised identically either
    way, so there is one version of this behaviour to test rather than two.

    Title and message arrive as environment variables (RPI_TOAST_TITLE,
    RPI_TOAST_MESSAGE), not as parameters. The message contains parentheses, a
    timestamp and a full stop; passing that through a generated command line means
    quoting it for CreateProcess *and* for PowerShell, and a quote that survives
    one layer is eaten by the other. The environment block needs no escaping at
    all.

    Exit status is 0 when the toast was shown and 1 when it could not be. The
    caller logs the difference. A failed toast is not fatal: the alert has already
    been appended to logs/alerts.log by that point, which is the record that
    survives a toast that never appears.

    Usage - from pull_and_run.ps1, or by hand:

        $env:RPI_TOAST_TITLE = 'RPI: test'
        $env:RPI_TOAST_MESSAGE = 'body'
        powershell -NoProfile -ExecutionPolicy Bypass -File deploy\toast.ps1
#>

# Deliberately not $ErrorActionPreference = 'Stop': Write-Error would then become
# terminating, and this script reports its failures through its exit code.
function Write-Err {
    param([string] $Message)
    [Console]::Error.WriteLine($Message)
}

function ConvertTo-XmlText {
    # '&' first. Escaping it after '<' would double-escape the ampersands that
    # replacing '<' and '>' introduce.
    param([string] $Value)
    if ([string]::IsNullOrEmpty($Value)) { return '' }
    return $Value.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')
}

$title   = [string] $env:RPI_TOAST_TITLE
$message = [string] $env:RPI_TOAST_MESSAGE

if ([string]::IsNullOrEmpty($title)) {
    Write-Err 'RPI_TOAST_TITLE is not set'
    exit 1
}

try {
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    # Escaped, not concatenated raw. Every current message happens to be plain
    # text, but one '&' in a future message would make this XML invalid and the
    # toast would fail with nothing but a parse error to explain it.
    $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' +
                 (ConvertTo-XmlText $title) + '</text><text>' +
                 (ConvertTo-XmlText $message) + '</text></binding></visual></toast>')

    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml

    # PowerShell's own AppID. A toast from an unregistered AppID is dropped
    # silently - hence logs/alerts.log being written by the caller before this
    # ever runs. Using it means the notification is attributed to "Windows
    # PowerShell" rather than to this project, which is cosmetic and is the price
    # of not registering a launcher of our own.
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    exit 0
} catch {
    Write-Err "toast failed: $($_.Exception.Message)"
    exit 1
}
