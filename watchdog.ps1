$log = 'C:\Users\Gasho\cline-pass-hermes-bridge\watchdog.log'
$ok = $false
try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8317/health' -TimeoutSec 5 -UseBasicParsing
    $ok = ($r.StatusCode -eq 200)
} catch {}
if (-not $ok) {
    # Guard against the 2026-09-28 pileup: never spawn a second bridge onto a
    # port that is already bound (even by a wedged process) - SO_REUSEADDR would
    # let it bind and create a multi-server fight over 8317.
    $listening = netstat -ano | Select-String ':8317\s.*LISTENING'
    if ($listening) {
        "$(Get-Date -Format s) health check failed but port 8317 already LISTENING (wedged holder PID(s): $($listening.Matches.Value | Select-Object -First 1)) - NOT spawning, manual restart required" | Out-File $log -Append -Encoding utf8
    } else {
        "$(Get-Date -Format s) health check failed, nothing listening on 8317 -> launching start-bridge.vbs" | Out-File $log -Append -Encoding utf8
        Start-Process wscript.exe -ArgumentList '"C:\Users\Gasho\cline-pass-hermes-bridge\start-bridge.vbs"' -WindowStyle Hidden
    }
}
