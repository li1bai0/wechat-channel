# wechat-channel Windows setup
# One-shot installer: checks Python/Node/Codex, installs pycryptodome,
# creates backend.json, and prints the exact next steps.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\Install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\Install.ps1 -SkipRegister
param(
    [switch]$SkipRegister,
    [switch]$NoDeps,
    [switch]$InstallCodex
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$bridgeDir = Join-Path $repo 'weixin_bridge'
$workDir = Join-Path $repo 'wechat_work'
$backendPath = Join-Path $bridgeDir 'backend.json'

Write-Host ''
Write-Host '=====================================================' -ForegroundColor Cyan
Write-Host ' wechat-channel  Windows setup' -ForegroundColor Cyan
Write-Host '=====================================================' -ForegroundColor Cyan
Write-Host "repo : $repo"
Write-Host ''

function Find-Cmd($name) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return (($c.Source -replace '\.cmd$', '') + (if ($c.CommandType -eq 'Application') { $c.Path } else { $c.Source })) }
    # simpler: return first non-empty of Path/Source
    if ($c.Path) { return $c.Path }
    if ($c.Source) { return $c.Source }
    return $null
}

# ---------- python ----------
$python = $null
$pt = Get-Command python -ErrorAction SilentlyContinue
if ($pt) { $python = $pt.Source }
if (-not $python) {
    $pt = Get-Command py -ErrorAction SilentlyContinue
    if ($pt) { $python = "$($pt.Source) -3" }
}
if (-not $python) {
    Write-Host 'Python not found. Install Python 3.10+ from https://www.python.org/downloads/' -ForegroundColor Red
    exit 1
}
& $python -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Python version too old. Need Python 3.10+.' -ForegroundColor Red
    exit 1
}
Write-Host "python : $python"

# ---------- node ----------
$node = $null
$nt = Get-Command node -ErrorAction SilentlyContinue
if ($nt) { $node = $nt.Source }
if (-not $node) {
    $cand = Join-Path $env:ProgramFiles 'nodejs\node.exe'
    if (Test-Path $cand) { $node = $cand }
}
if ($node) {
    Write-Host "node   : $node"
} else {
    Write-Host 'node   : NOT FOUND. Codex backend needs Node 18+.' -ForegroundColor Yellow
}

# ---------- codex ----------
$codex = $null
$ct = Get-Command codex.cmd -ErrorAction SilentlyContinue
if ($ct) { $codex = $ct.Source }
if (-not $codex) {
    $ct = Get-Command codex -ErrorAction SilentlyContinue
    if ($ct) { $codex = $ct.Source }
}
if (-not $codex) {
    $cands = @(
        (Join-Path $HOME '.codex\bin\codex.exe'),
        (Join-Path $HOME '.codex\bin\codex'),
        (Join-Path $env:APPDATA 'npm\codex.cmd'),
        (Join-Path $env:APPDATA 'npm\node_modules\@openai\codex\bin\codex.js')
    )
    foreach ($p in $cands) {
        if (Test-Path $p) { $codex = $p; break }
    }
}
if ($codex) {
    Write-Host "codex  : $codex"
} elseif ($InstallCodex) {
    Write-Host 'Installing @openai/codex globally...'
    if (-not $node) {
        Write-Host 'Cannot install codex: node not found. Install Node.js 18+ first.' -ForegroundColor Red
        exit 1
    }
    npm install -g @openai/codex
    if ($LASTEXITCODE -ne 0) { exit 1 }
    $codex = (Get-Command codex.cmd -ErrorAction SilentlyContinue).Source
    if (-not $codex) { $codex = (Get-Command codex -ErrorAction SilentlyContinue).Source }
    if ($codex) { Write-Host "codex  : $codex" }
} else {
    Write-Host 'codex  : NOT FOUND. Run: npm install -g @openai/codex' -ForegroundColor Yellow
}

# ---------- claude (optional) ----------
$claude = $null
$clt = Get-Command claude.cmd -ErrorAction SilentlyContinue
if ($clt) { $claude = $clt.Source }
if (-not $claude) {
    $clt = Get-Command claude -ErrorAction SilentlyContinue
    if ($clt) { $claude = $clt.Source }
}
if ($claude) { Write-Host "claude : $claude" }

# ---------- models.json max-effort check ----------
$modelsJson = Join-Path $HOME '.codex\models.json'
$modelsHint = ''
if (Test-Path $modelsJson) {
    try {
        $raw = Get-Content -LiteralPath $modelsJson -Raw -Encoding UTF8
        if ($raw -match '"effort"\s*:\s*"max"') {
            $modelsHint = ' [contained effort:max; if Codex reports unknown variant max, copy models.json to models.wechat.json without max entries and pass model_catalog_json to bridge/agent]'
        }
    } catch {}
}

# ---------- dirs ----------
New-Item -ItemType Directory -Force -Path $bridgeDir | Out-Null
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

# ---------- backend.json ----------
if (-not (Test-Path -LiteralPath $backendPath)) {
    $cfg = [ordered]@{
        backend = 'codex'
        node_exe = ''; codex_js = ''; claude_exe = ''
        work_dir = $workDir
        chat_model = 'deepseek-v4-flash'; chat_effort = 'medium'
        casual_effort = 'low'; task_model = 'deepseek-v4-flash'; task_effort = 'medium'
        generic = @{
            new_cmd = @('myagent','-p','{prompt}')
            resume_cmd = @('myagent','-p','{prompt}','-s','{session}')
            session_regex = 'session[=_ ]([0-9a-fA-F-]{8,})'
        }
    }
    if ($node) { $cfg.node_exe = $node }
    if ($codex) { $cfg.codex_js = $codex }
    $cfg | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $backendPath -Encoding UTF8
    Write-Host "created : $backendPath" -ForegroundColor Green
} else {
    Write-Host "exists  : $backendPath (kept as-is)"
}

# ---------- python deps ----------
if (-not $NoDeps) {
    Write-Host ''
    Write-Host 'Installing Python dependency: pycryptodome'
    & $python -m pip install pycryptodome
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'pycryptodome install failed.' -ForegroundColor Red
        exit 1
    }
}

# ---------- summary ----------
Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
if ($modelsHint) { Write-Host "models.json: $modelsHint" -ForegroundColor Yellow }
Write-Host ''
Write-Host 'Next steps:'
Write-Host '  1) 扫码注册机器人号:'
Write-Host "     python `"$repo\scripts\wechat_bridge.py`" register"
Write-Host '     (用将要当机器人的微信号扫码，不要用主号)'
Write-Host '  2) 启动常驻桥:'
Write-Host "     pythonw `"$repo\scripts\wechat_bridge.py`" run"
Write-Host '  3) 查看状态:'
Write-Host "     python `"$repo\scripts\wechat_bridge.py`" status"
Write-Host '  4) 开机自启示例（管理员 PowerShell）:'
Write-Host '     schtasks /Create /TN "wechat-channel" /TR "pythonw <repo-path>\scripts\wechat_bridge.py run" /SC ONLOGON /RL HIGHEST'
Write-Host ''
Write-Host 'If bridge cannot find codex/npm at runtime, update PATH:'
Write-Host '  $env:Path = "C:\\Program Files\\nodejs;$env:APPDATA\\npm;" + $env:Path'
Write-Host ''

if (-not $SkipRegister) {
    $ans = Read-Host 'Run register now and scan QR code? [Y/n]'
    if ($ans -eq '' -or $ans -match '^[Yy]') {
        Push-Location $repo
        try {
            & $python scripts\wechat_bridge.py register
        } finally {
            Pop-Location
        }
    }
}

Write-Host 'Done.' -ForegroundColor Green