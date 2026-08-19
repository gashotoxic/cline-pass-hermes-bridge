#!/usr/bin/env python3
"""
Cline Device Authorization (RFC 8628) — browser-based login, no VS Code needed.

Flow:
  1. POST /user_management/authorize/device  → get device_code + user_code
  2. Open verification_uri_complete in the user's browser
  3. Poll /user_management/authenticate with device_code until user completes
  4. Receive idToken + refreshToken, write to ~/.cline/data/secrets.json

The bridge watches secrets.json for mtime changes and picks up the new session
automatically — no bridge restart needed.

Usage:
  python cline_device_auth.py            # full login flow
  python cline_device_auth.py --probe    # just get a user_code, don't poll
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import webbrowser
from datetime import datetime

CLIENT_ID = "client_01K3A541FN8TA3EPPHTD2325AR"
DEVICE_AUTH_URL = "https://api.workos.com/user_management/authorize/device"
AUTHENTICATE_URL = "https://api.workos.com/user_management/authenticate"

CLINE_DATA_DIR = os.path.join(os.path.expanduser("~"), ".cline", "data")
SECRETS_PATH = os.path.join(CLINE_DATA_DIR, "secrets.json")
SECRETS_KEY = "cline:clineAccountId"


def post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": "unknown", "error_description": e.read().decode("utf-8", errors="replace")[:500]}


def step1_request_device_code():
    print("[1/4] Requesting device code from WorkOS...")
    status, body = post_form(DEVICE_AUTH_URL, {"client_id": CLIENT_ID})
    if status != 200:
        print(f"  FAILED: HTTP {status}: {body}")
        sys.exit(1)
    print(f"  user_code:              {body['user_code']}")
    print(f"  verification_uri:       {body['verification_uri']}")
    print(f"  verification_uri_complete: {body.get('verification_uri_complete', '')}")
    print(f"  expires_in:             {body['expires_in']}s")
    print(f"  poll interval:          {body['interval']}s")
    return body


def step2_open_browser(device):
    url = device.get("verification_uri_complete") or device["verification_uri"]
    print(f"\n[2/4] Opening browser: {url}")
    print(f"      If it didn't open, visit the URL manually and enter code: {device['user_code']}")
    try:
        webbrowser.open(url)
    except Exception as exc:
        print(f"  (webbrowser.open failed: {exc} — use manual URL above)")


def step3_poll_for_token(device):
    print(f"\n[3/4] Waiting for you to complete login in the browser...")
    print(f"      (polling every {device['interval']}s, expires in {device['expires_in']}s)")
    deadline = time.time() + device["expires_in"]
    interval = device["interval"]

    while time.time() < deadline:
        time.sleep(interval)
        status, body = post_form(AUTHENTICATE_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device["device_code"],
            "client_id": CLIENT_ID,
        })
        if status == 200:
            print("  ✓ Login complete!")
            return body
        err = body.get("error", "")
        desc = body.get("error_description", "")
        if err == "authorization_pending":
            print(f"  ... still waiting ({int(deadline - time.time())}s left)")
            continue
        if err == "slow_down":
            interval += 5
            print(f"  ... server asked to slow down, interval now {interval}s")
            continue
        if err == "expired_token":
            print("  ✗ Device code expired. Re-run the script.")
            sys.exit(2)
        if err == "access_denied":
            print("  ✗ You denied the request.")
            sys.exit(3)
        print(f"  ✗ Unexpected error: HTTP {status}: {body}")
        sys.exit(4)

    print("  ✗ Timed out waiting for login.")
    sys.exit(2)


def step4_save_tokens(token_resp):
    print(f"\n[4/4] Saving tokens to {SECRETS_PATH}")
    # WorkOS device-grant response: { access_token, refresh_token, token_type, expires_in? }
    # (No user object — the JWT itself carries the claims.)
    access = token_resp["access_token"]
    refresh = token_resp["refresh_token"]

    # Decode the JWT to get user info
    import base64
    payload = access.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    now = int(time.time())
    exp = claims.get("exp", now + 3600)

    # Build the secrets.json value in the exact shape Cline v4 writes
    auth = {
        "idToken": access,
        "refreshToken": refresh,
        "userInfo": {
            "id": claims.get("external_id", ""),
            "email": claims.get("email", ""),
            "displayName": f"{claims.get('firstName', '')} {claims.get('lastName', '')}".strip(),
            "termsAcceptedAt": "",
            "clineBenchConsent": False,
            "organizations": [],
            "createdAt": "",
            "updatedAt": "",
        },
        "expiresAt": exp,  # seconds since epoch (matches JWT exp)
        "provider": "cline",
    }

    # Backup existing
    if os.path.exists(SECRETS_PATH):
        bak = f"{SECRETS_PATH}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                old = f.read()
            with open(bak, "w", encoding="utf-8") as f:
                f.write(old)
            print(f"  backed up existing secrets.json → {os.path.basename(bak)}")
        except Exception as exc:
            print(f"  (backup failed: {exc})")

    os.makedirs(CLINE_DATA_DIR, exist_ok=True)
    payload = {SECRETS_KEY: json.dumps(auth)}
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"  ✓ wrote secrets.json for {auth['userInfo']['email']}")
    print(f"  ✓ expires at {datetime.fromtimestamp(auth['expiresAt'])}")
    print()
    print("The bridge will detect the change within seconds (mtime watch).")
    print("Verify with:  curl http://localhost:8317/health")


def main():
    if "--probe" in sys.argv:
        device = step1_request_device_code()
        print("\n(probe mode — not polling, not saving)")
        return

    device = step1_request_device_code()
    step2_open_browser(device)
    token_resp = step3_poll_for_token(device)
    step4_save_tokens(token_resp)


if __name__ == "__main__":
    main()
