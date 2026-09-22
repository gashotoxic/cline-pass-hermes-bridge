#!/usr/bin/env python3
"""
Cline Device Authorization -> providers.json (multi-account, per Bridge Guide v2.1 addendum).

Same RFC 8628 device flow as cline_device_auth.py, but on success the new
account is written into ~/.cline/data/settings/providers.json as a NEW
'cline-pass-N' key (legacy store) instead of overwriting secrets.json.
Existing accounts (incl. cline-secrets) are never touched.

Usage:
  python cline_device_auth_providers.py                # next free cline-pass-N key
  python cline_device_auth_providers.py --key cline-pass-4
Status is written to device-auth-status.json so agents can poll progress.
"""

import json
import os
import sys
import time
import base64
import shutil
import urllib.request
import urllib.parse
import urllib.error
import webbrowser
from datetime import datetime

CLIENT_ID = "client_01K3A541FN8TA3EPPHTD2325AR"
DEVICE_AUTH_URL = "https://api.workos.com/user_management/authorize/device"
AUTHENTICATE_URL = "https://api.workos.com/user_management/authenticate"

CLINE_DATA_DIR = os.path.join(os.path.expanduser("~"), ".cline", "data")
PROVIDERS_PATH = os.path.join(CLINE_DATA_DIR, "settings", "providers.json")
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(BRIDGE_DIR, "device-auth-status.json")

DEFAULT_MODEL = "cline-pass/kimi-k3"


def write_status(state):
    with open(STATUS_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), msg)
    print(line, flush=True)


def post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": "unknown"}


def jwt_claims(token):
    seg = token.split(".")[1]
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def next_free_key(provs):
    n = 1
    while ("cline-pass-%d" % (n + 1)) in provs or ("cline-pass-%d" % n) in provs:
        n += 1
    return "cline-pass-%d" % (n + 1)


def main():
    key = None
    if "--key" in sys.argv:
        key = sys.argv[sys.argv.index("--key") + 1]

    # [1/4] device code
    status, device = post_form(DEVICE_AUTH_URL, {"client_id": CLIENT_ID})
    if status != 200:
        write_status({"state": "failed", "step": 1, "error": "HTTP %s: %s" % (status, device)})
        sys.exit(1)
    write_status({"state": "waiting_for_browser_login",
                  "user_code": device["user_code"],
                  "verification_uri": device["verification_uri"],
                  "verification_uri_complete": device.get("verification_uri_complete", ""),
                  "expires_in": device["expires_in"]})
    log("user_code: %s" % device["user_code"])
    log("open: %s" % device.get("verification_uri_complete") or device["verification_uri"])
    try:
        webbrowser.open(device.get("verification_uri_complete") or device["verification_uri"])
    except Exception as exc:
        log("webbrowser.open failed: %s" % exc)

    # [3/4] poll
    deadline = time.time() + device["expires_in"]
    interval = device["interval"]
    token = None
    while time.time() < deadline:
        time.sleep(interval)
        status, body = post_form(AUTHENTICATE_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device["device_code"],
            "client_id": CLIENT_ID,
        })
        if status == 200:
            token = body
            break
        err = body.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "expired_token":
            write_status({"state": "failed", "step": 3, "error": "device code expired"})
            sys.exit(2)
        if err == "access_denied":
            write_status({"state": "failed", "step": 3, "error": "access denied"})
            sys.exit(3)
        write_status({"state": "failed", "step": 3, "error": "HTTP %s: %s" % (status, body)})
        sys.exit(4)

    if not token:
        write_status({"state": "failed", "step": 3, "error": "timed out"})
        sys.exit(2)

    # [4/4] write to providers.json as cline-pass-N
    access = token["access_token"]
    refresh = token["refresh_token"]
    claims = jwt_claims(access)
    email = claims.get("email", "?")
    display = ("%s %s" % (claims.get("firstName", ""), claims.get("lastName", ""))).strip()
    now_ms = int(time.time() * 1000)
    exp_ms = int(claims.get("exp", now_ms // 1000 + 3600)) * 1000

    with open(PROVIDERS_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    provs = data.setdefault("providers", {})

    if key is None:
        key = next_free_key(provs)

    bak = "%s.bak.device-auth-%s" % (PROVIDERS_PATH, datetime.now().strftime("%Y%m%d_%H%M%S"))
    try:
        shutil.copy2(PROVIDERS_PATH, bak)
    except OSError:
        pass

    userInfo = {
        "id": claims.get("external_id", claims.get("sub", "")),
        "email": email,
        "displayName": display,
        "termsAcceptedAt": "",
        "clineBenchConsent": False,
        "organizations": [],
        "createdAt": "",
        "updatedAt": "",
    }
    provs[key] = {
        "settings": {
            "provider": "cline-pass",
            "model": DEFAULT_MODEL,
            "reasoning": {"effort": "xhigh", "budgetTokens": 1471},
            "auth": {
                "accessToken": "workos:" + access,
                "refreshToken": refresh,
                "expiresAt": exp_ms,
                "accountId": claims.get("sub", ""),
                "metadata": {"sessionStartedAtMs": now_ms, "tokenType": "Bearer",
                             "userInfo": userInfo},
            },
        },
        "updatedAt": datetime.utcnow().isoformat() + "Z",
        "tokenSource": "oauth",
    }
    tmp = PROVIDERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, PROVIDERS_PATH)

    write_status({"state": "done", "key": key, "email": email, "expiresAt": exp_ms})
    log("OK: wrote %s -> %s (token valid ~3600s; bridge will rotate via refresh token)" % (key, email))
    log("NOTE: restart the bridge so discovery picks up the new account.")


if __name__ == "__main__":
    main()
