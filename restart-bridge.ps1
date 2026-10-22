# restart-bridge.ps1 — safe swap of the ClinePass bridge on 127.0.0.1:8317
#
# Kills EVERY pythonw/python process running cline_pass_bridge.py (the port
# can have multiple listeners thanks to SO_REUSEADDR — see the 2026-09-28
# pileup note in watchdog.ps1), then starts exactly one fresh instance and
# polls /health. Run after editing cline_pass_bridge.py.
#
# WARNING: anything currently talking to the bridge (pi, Hermes) loses its
# in-flight requests during the ~3s gap. Prefer running this between tasks.

$ErrorActionPreference = 'Continue'
$bridgeDir = 'C:\Users\Gasho\cline-pass-hermes-bridge'
$log = "$bridgeDir\bridge.log"

function Log($msg) {
    $line = "[$(Get-Date -Format s)] restart: $msg"
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

# 1. Find and kill all bridge processes (any port — 8317 live + stray shadows)
$targets = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'cline_pass_bridge\.py' }
if ($targets) {
    foreach ($t in $targets) {
        Log "killing bridge process PID $($t.ProcessId)"
        Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Log 'no running bridge process found'
}
Start-Sleep -Seconds 2

# 2. Verify the port is actually free (paranoid: wedged holders)
$still = netstat -ano | Select-String ':8317\s.*LISTENING'
if ($still) {
    Log "FATAL: port 8317 still LISTENING after kills ($still) - aborting start to avoid pileup"
    exit 1
}

# 3. Start exactly one fresh instance (silent, detached)
# 2026-10-22: zen anonymous tier is rate-limited on the Safaricom IP this PC
# shares with gashoteck. Relay all zen traffic through gashotech (Unwired
# ISP, different egress IP) over Tailscale. Only zen is diverted; Cline
# requests keep going out directly.
$env:ZEN_RELAY_BASE = 'http://100.105.117.30:8317/v1'
$pythonw = 'C:\Users\Gasho\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe'
Start-Process -FilePath $pythonw `
    -ArgumentList "`"$bridgeDir\cline_pass_bridge.py`"" `
    -WorkingDirectory $bridgeDir -WindowStyle Hidden
Log 'new bridge instance launched'

# 4. Poll /health up to 30s
$ok = $false
foreach ($i in 1..10) {
    Start-Sleep -Seconds 3
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8317/health' -TimeoutSec 5 -UseBasicParsing
        $ok = ($r.StatusCode -eq 200)
        if ($ok) { break }
    } catch {}
}
if ($ok) {
    Log 'bridge healthy after restart'
    Write-Host 'BRIDGE RESTART: HEALTHY'
    exit 0
} else {
    Log 'FATAL: bridge did not become healthy within 30s - check bridge.log'
    Write-Host 'BRIDGE RESTART: FAILED (check bridge.log)'
    exit 1
}
