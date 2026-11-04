# Optional Task Scheduler exit-code bridge; collection logic stays in one Python file.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Script,
    [Parameter(Mandatory = $true)][string]$Output,
    [Parameter(Mandatory = $true)][string]$RunId
)

$taskNativeExit = $null
$taskLauncherError = $null
$taskLog = $null
$taskStdout = $null
$taskStderr = $null

function ConvertTo-TaskArgument([string]$Value) {
    # Windows native argv quoting: double backslashes before a quote or the
    # closing delimiter. Start-Process does not preserve array item boundaries.
    $taskQuoted = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $taskQuoted = [regex]::Replace($taskQuoted, '(\\+)$', '$1$1')
    return '"' + $taskQuoted + '"'
}

try {
    $taskDirectory = [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetFullPath($Output))
    $taskLog = Join-Path $taskDirectory.FullName 'task-launcher.log'
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $Script -PathType Leaf)) {
        throw 'Python or crawler script does not exist.'
    }
    $taskToken = (Get-Date -Format 'yyyyMMddTHHmmss') + '-' + [guid]::NewGuid().ToString('N')
    $taskStdout = Join-Path $taskDirectory.FullName ($taskToken + '.stdout.log')
    $taskStderr = Join-Path $taskDirectory.FullName ($taskToken + '.stderr.log')
    ('{0:o} launcher_start run_id={1}' -f (Get-Date), $RunId) |
        Out-File -LiteralPath $taskLog -Encoding utf8 -Append -ErrorAction Stop
    $taskArguments = @('-u', (ConvertTo-TaskArgument $Script), '--output',
        (ConvertTo-TaskArgument $taskDirectory.FullName), '--run-id', (ConvertTo-TaskArgument $RunId))
    $taskProcess = Start-Process -FilePath $Python -ArgumentList $taskArguments -Wait -PassThru `
        -WindowStyle Hidden -RedirectStandardOutput $taskStdout -RedirectStandardError $taskStderr -ErrorAction Stop
    $taskNativeExit = $taskProcess.ExitCode
    if ($null -eq $taskNativeExit) {
        throw 'Python did not return an exit status.'
    }
}
catch {
    $taskLauncherError = $_.Exception.Message
}

# Task Scheduler lacks a per-exit-code RestartPreventExitStatus equivalent.
# Its zero below means "do not restart", not "the crawler finished the batch".
$taskSchedulerExit = if ($null -ne $taskLauncherError -or
    $taskNativeExit -in @(0, 2, 3, 4, 130)) { 0 } else { 1 }
$taskResult = '{0:o} native_exit={1} scheduler_exit={2} stdout={3} stderr={4} launcher_error={5}' -f
    (Get-Date), $taskNativeExit, $taskSchedulerExit, $taskStdout, $taskStderr, $taskLauncherError
if ($null -ne $taskLog) {
    $taskResult | Out-File -LiteralPath $taskLog -Encoding utf8 -Append
}
else {
    Write-Error $taskResult
}
exit $taskSchedulerExit
