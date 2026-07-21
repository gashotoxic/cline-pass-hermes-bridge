#!/usr/bin/env python3
"""
ClinePass Token Health Monitor
Checks token expiry and alerts when re-auth is needed.
Run: python token_monitor.py
"""

import json, os, sys, time, urllib.request, base64

PROVIDERS_PATH = os.path.expanduser("~/.cline/data/settings/providers.json")
BRIDGE_HEALTH = "http://127.0.0.1:8317/health"
LOG_PATH = os.path.expanduser("~/cline-pass-hermes-bridge/token_monitor.log")


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def decode_jwt_exp(token):
    parts = token.split('.')
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding = 4 - len(payload) % 4
    if padding != 4:
        payload += '=' * padding
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return decoded.get('exp', 0)
    except Exception:
        return None


def check_providers():
    """Check providers.json for token health."""
    if not os.path.isfile(PROVIDERS_PATH):
        log(f"ERROR: providers.json not found at {PROVIDERS_PATH}")
        return False
    
    with open(PROVIDERS_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    providers = data.get('providers', {})
    issues = []
    
    for key in sorted(providers.keys()):
        if not key.startswith('cline-pass'):
            continue
        
        p = providers[key]
        auth = p.get('settings', {}).get('auth', {})
        email = auth.get('metadata', {}).get('userInfo', {}).get('email', 'unknown')
        at = auth.get('accessToken', '')
        rt = auth.get('refreshToken', '')
        
        exp = decode_jwt_exp(at)
        if exp is None:
            issues.append(f"{key}: cannot decode access token")
            continue
        
        remaining = exp - time.time()
        has_rt = bool(rt)
        
        if remaining < 0:
            status = "EXPIRED"
        elif remaining < 300:
            status = f"EXPIRING SOON ({remaining:.0f}s)"
        else:
            status = f"OK ({remaining/60:.0f} min)"
        
        log(f"{key} ({email}): access token {status}, refresh_token={'yes' if has_rt else 'NO'}")
        
        if not has_rt:
            issues.append(f"{key}: NO REFRESH TOKEN — re-auth required")
        elif remaining < -3600:
            issues.append(f"{key}: access token expired {abs(remaining)/3600:.1f}h ago, likely revoked")
    
    return issues


def check_bridge():
    """Check bridge health endpoint."""
    try:
        with urllib.request.urlopen(BRIDGE_HEALTH, timeout=5) as r:
            data = json.loads(r.read())
        
        if not data.get('ok'):
            log("Bridge: NOT OK")
            return False
        
        accounts = data.get('accounts', [])
        for a in accounts:
            key = a.get('key', '?')
            email = a.get('email', '?')
            ok = a.get('ok', False)
            cooling = a.get('cooling_down', False)
            exp_in = a.get('expires_in_seconds', 0)
            
            if cooling:
                log(f"Bridge {key} ({email}): COOLING DOWN (expired {abs(exp_in)/60:.0f} min ago)")
            elif ok:
                log(f"Bridge {key} ({email}): healthy ({exp_in/60:.0f} min)")
            else:
                log(f"Bridge {key} ({email}): UNHEALTHY")
        
        return True
    except Exception as e:
        log(f"Bridge: UNREACHABLE ({e})")
        return False


def main():
    log("=" * 50)
    log("Token health check")
    
    issues = check_providers()
    bridge_ok = check_bridge()
    
    if issues:
        log("ISSUES FOUND:")
        for i in issues:
            log(f"  - {i}")
        log("")
        log("ACTION REQUIRED: Re-sign in to ClinePass via VS Code Cline extension")
        log("  1. Open VS Code")
        log("  2. Open Cline extension")
        log("  3. Sign in with ClinePass for the affected account")
        log("  4. Tell Hermes to extract fresh tokens from secrets.json")
        return 1
    elif not bridge_ok:
        log("Bridge is down — start it with:")
        log(f"  pythonw {os.path.expanduser('~/cline-pass-hermes-bridge/cline_pass_bridge.py')}")
        return 1
    else:
        log("All accounts healthy, bridge running")
        return 0


if __name__ == "__main__":
    sys.exit(main())
