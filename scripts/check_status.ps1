$ErrorActionPreference = 'SilentlyContinue'
$base = Join-Path $PSScriptRoot 'weixin_bridge'

$logFile = Join-Path $base 'bridge.log'
$stateFile = Join-Path $base 'state.json'
$accountFile = Join-Path $base 'account.json'
$backendFile = Join-Path $base 'backend.json'

Write-Output '== bridge process =='
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'wechat_bridge\.py' }
if ($procs) {
    $procs | ForEach-Object { "running PID=$($_.ProcessId) started at $($_.CreationDate)" }
} else {
    Write-Output 'not running'
}

Write-Output '== backend =='
if (Test-Path -LiteralPath $backendFile) {
    $cfg = Get-Content -LiteralPath $backendFile -Raw | ConvertFrom-Json
    "backend=$($cfg.backend)"
} else {
    Write-Output 'backend=codex (default)'
}

Write-Output '== account =='
if (Test-Path -LiteralPath $accountFile) {
    $acc = Get-Content -LiteralPath $accountFile -Raw | ConvertFrom-Json
    "account_id=$($acc.account_id)  saved_at=$($acc.saved_at)"
} else {
    Write-Output 'no account.json - need to run register first'
}

Write-Output '== recent log =='
if (Test-Path -LiteralPath $logFile) {
    Get-Content -LiteralPath $logFile -Encoding UTF8 -Tail 12
} else {
    Write-Output 'no bridge.log'
}

Write-Output '== verdict =='
if (Test-Path -LiteralPath $logFile) {
    $tail = Get-Content -LiteralPath $logFile -Encoding UTF8 -Tail 3
    $sentMark = [string][char]0x5DF2 + [char]0x56DE + [char]0x5FAE + [char]0x4FE1
    $startMark = [string][char]0x542F + [char]0x52A8
    if ($tail -match '-14') {
        Write-Output '>>> WeChat session expired (-14), need to re-register by scanning QR'
    } elseif ($tail -match [regex]::Escape($sentMark)) {
        Write-Output '>>> channel OK, latest reply sent successfully'
    } elseif ($tail -match [regex]::Escape($startMark)) {
        Write-Output '>>> bridge just started, no session-expiry errors'
    } else {
        Write-Output '>>> inspect the log above manually'
    }
}
