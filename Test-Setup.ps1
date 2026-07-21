# Test-Setup.ps1 - verifies every link in the ClinePass -> Hermes chain.
# Run after Install.ps1 (or any time something feels broken). Exit code 0 = all good.
#
#   .\Test-Setup.ps1
#
$ErrorActionPreference = 'Continue'
$global:allOk = $true

function Check([string]$name, [scriptblock]$test, [string]$fix) {
    $msg = & $test
    if ($null -eq $msg -or $msg -eq $true) {
        Write-Host "[PASS] $name"
    } else {
        Write-Host "[FAIL] $name -> $msg"
        Write-Host "       fix : $fix"
        $global:allOk = $false
    }
}

# 1. Python available
Check 'Python 3.8+ installed' {
    try { $v = (python --version 2>&1); if ($v -match 'Python 3\.([8-9]|\d{2})') { $true } else { "found '$v'" } }
    catch { 'python not on PATH' }
} 'Install from https://python.org (tick "Add to PATH").'

# 2. Cline CLI available
Check 'Cline CLI installed' {
    $c = Get-Command cline -ErrorAction SilentlyContinue
    if ($c) { $true } else { 'cline not on PATH' }
} 'npm install -g cline'

# 3. OAuth session exists in providers.json
Check 'Cline OAuth session (cline auth cline)' {
    $p = Join-Path $env:USERPROFILE '.cline\data\settings\providers.json'
    if (-not (Test-Path $p)) { return 'providers.json missing' }
    try {
        $j = Get-Content $p -Raw | ConvertFrom-Json
        if ($j.providers.'cline-pass'.settings.auth.refreshToken -or
            $j.providers.'cline'.settings.auth.refreshToken) { $true }
        else { 'no refresh token stored' }
    } catch { "providers.json unreadable: $_" }
} 'Run: cline auth cline  (browser sign-in with your ClinePass account).'

# 4. Hermes config parses cleanly and has the provider
Check 'Hermes config.yaml valid + cline-pass provider' {
    $cfg = Join-Path $env:LOCALAPPDATA 'hermes\config.yaml'
    if (-not (Test-Path $cfg)) { $cfg = Join-Path $env:USERPROFILE '.hermes\config.yaml' }
    if (-not (Test-Path $cfg)) { return 'config.yaml not found (run hermes once first)' }
    $raw = [System.IO.File]::ReadAllBytes($cfg)
    $bad = 0; foreach ($b in $raw) { if ($b -lt 32 -and $b -ne 9 -and $b -ne 10 -and $b -ne 13) { $bad++ } }
    if ($bad -gt 0) { return "$bad control chars - file is corrupted; restore a config.yaml.bak copy" }
    $text = [System.IO.File]::ReadAllText($cfg, [System.Text.Encoding]::UTF8)
    if ($text -notmatch 'name:\s*cline-pass') { return 'no cline-pass custom_providers entry' }
    $true
} 'Paste the block from hermes-config-example.yaml into config.yaml (UTF-8!).'

# 5. Bridge healthy
Check 'Bridge healthy on 127.0.0.1:8317' {
    try { $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8317/health' -TimeoutSec 5
          if ($h.ok) { $true } else { "health says: $($h | ConvertTo-Json -Compress)" } }
    catch { 'not reachable' }
} 'Run: .\Start-Bridge.ps1'

# 6. Live completion through the bridge (proves OAuth + upstream + model)
Check 'Live ClinePass completion via bridge' {
    $body = @{ model = 'cline-pass/kimi-k3'
               messages = @(@{ role = 'user'; content = 'say OK' })
               stream = $false; max_tokens = 20 } | ConvertTo-Json
    try {
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8317/v1/chat/completions' `
             -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 60
        if ($r.data.choices) { $true } else { 'unexpected response shape' }
    } catch { "request failed: $($_.Exception.Message) $($_.ErrorDetails.Message)" }
} 'Check bridge.log; if 401 -> cline auth cline, then .\Start-Bridge.ps1'

Write-Host ''
if ($global:allOk) { Write-Host 'ALL CHECKS PASSED - harness is ready.'; exit 0 }
else { Write-Host 'Some checks FAILED - apply the fixes above, then re-run.'; exit 1 }
