# Start-Bridge.ps1 - launcher + watchdog for the ClinePass -> Hermes bridge.
# Safe to run repeatedly: no-op if the bridge is already healthy.
#
#   .\Start-Bridge.ps1            start/repair the bridge
#   .\Start-Bridge.ps1 -Install   register it to auto-start at logon (Scheduled Task)
#
param([switch]$Install)

$BridgeDir = 'H:\cline-pass-hermes-bridge'
$BridgePy  = Join-Path $BridgeDir 'cline_pass_bridge.py'
$HealthUrl = 'http://127.0.0.1:8317/health'
$TaskName  = 'ClinePassHermesBridge'

function Test-Bridge {
    try {
        $h = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
        return $h.ok -eq $true
    } catch { return $false }
}

if ($Install) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Description 'Starts the ClinePass->Hermes OAuth bridge (cline_pass_bridge.py)' -Force | Out-Null
    Write-Host "Scheduled task '$TaskName' registered (runs at logon)."
}

if (Test-Bridge) {
    Write-Host 'Bridge already healthy - nothing to do.'
    exit 0
}

# Kill stale bridge processes, then start fresh.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'cline_pass_bridge' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Process -FilePath 'python' -ArgumentList "`"$BridgePy`"" -WindowStyle Hidden
Start-Sleep -Seconds 4

if (Test-Bridge) {
    Write-Host 'Bridge started and healthy.'
    exit 0
}

# Bridge is up but auth may be broken -> try a forced token refresh.
Write-Host 'Bridge not healthy yet; forcing token refresh...'
& python $BridgePy --refresh-now
Start-Sleep -Seconds 2

if (Test-Bridge) {
    Write-Host 'Bridge healthy after token refresh.'
    exit 0
}

Write-Host ''
Write-Host 'AUTH NEEDS REPAIR: the WorkOS refresh token is rejected.'
Write-Host 'Fix it by signing in again (browser opens), then re-run this script:'
Write-Host '    cline auth cline'
Write-Host '    .\Start-Bridge.ps1'
exit 1
