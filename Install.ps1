# Install.ps1 - one-step setup for the ClinePass -> Hermes OAuth bridge.
# Checks prerequisites, then starts the bridge (watchdog).
#
#   .\Install.ps1              check + start
#   .\Install.ps1 -AutoStart   check + start + register logon auto-start
#
param([switch]$AutoStart)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $PSCommandPath

Write-Host '== ClinePass -> Hermes bridge: install =='

# 1. Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host 'ERROR: python not found. Install Python 3.8+ from https://python.org and re-run.'
    exit 1
}
Write-Host ('ok  python: ' + (python --version 2>&1))

# 2. Cline CLI
$cline = Get-Command cline -ErrorAction SilentlyContinue
if (-not $cline) {
    Write-Host 'Cline CLI not found. Installing globally via npm...'
    npm install -g cline
}
Write-Host ('ok  cline: ' + (cline --version 2>&1 | Select-Object -First 1))

# 3. OAuth session present?
$providersJson = Join-Path $env:USERPROFILE '.cline\data\settings\providers.json'
$haveTokens = $false
if (Test-Path $providersJson) {
    try {
        $j = Get-Content $providersJson -Raw | ConvertFrom-Json
        $haveTokens = [bool]($j.providers.'cline-pass'.settings.auth.refreshToken -or
                             $j.providers.'cline'.settings.auth.refreshToken)
    } catch { $haveTokens = $false }
}
if (-not $haveTokens) {
    Write-Host ''
    Write-Host 'No Cline OAuth session found yet. A browser window will open -'
    Write-Host 'sign in with the account that has your ClinePass subscription.'
    Read-Host 'Press Enter to continue'
    cline auth cline
}
Write-Host 'ok  Cline OAuth session found'

# 4. Start the bridge (no-op if already healthy)
if ($AutoStart) {
    & (Join-Path $here 'Start-Bridge.ps1') -Install
} else {
    & (Join-Path $here 'Start-Bridge.ps1')
}

Write-Host ''
Write-Host 'Done. Next steps:'
Write-Host '  1. Add the cline-pass custom provider to your Hermes config.yaml (see README.md, Setup step 3)'
Write-Host '  2. hermes -z "hello" --provider custom:cline-pass -m cline-pass/kimi-k3'
Write-Host 'Health: curl http://127.0.0.1:8317/health'
