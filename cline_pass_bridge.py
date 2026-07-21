#!/usr/bin/env python3
"""
ClinePass -> Hermes auth bridge (multi-account)
=================================================

Lets Hermes Agent (or any OpenAI-compatible client) use your ClinePass
subscription(s) via the Cline CLI's OAuth login - NO API key involved.

Supports N accounts with round-robin rotation and per-account failover.

How it works (same pattern as antigravity-claude-proxy):
  1. You sign in once with the Cline CLI:  `cline auth cline`  (browser OAuth via WorkOS)
     Tokens land in  ~/.cline/data/settings/providers.json
  2. This bridge reads those tokens. WorkOS access tokens only live ~1 hour,
     so it refreshes them automatically via WorkOS before expiry and writes the
     rotated tokens back to providers.json so the Cline CLI stays in sync.
  3. It exposes a local OpenAI-compatible endpoint:
         http://127.0.0.1:8317/v1/chat/completions
     Hermes points at it as a `custom_providers` entry with a dummy api_key.
  4. Multi-account: any providers.json key matching "cline-pass*" is loaded
     as a separate account. Requests rotate round-robin across all accounts.
     On 401/429 from one account, the bridge fails over to the next.
  5. Failure handling: 401 -> force refresh + retry; 429/5xx -> next account;
     if refresh dies (revoked/expired refresh token) re-run `cline auth cline`.

Stdlib only. No pip installs required.
"""

import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from base64 import urlsafe_b64decode
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------- configuration -----------------------------
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
CLINE_PROVIDERS_JSON = os.environ.get(
    "CLINE_PROVIDERS_JSON",
    os.path.expanduser(os.path.join("~", ".cline", "data", "settings", "providers.json")),
)
WORKOS_AUTHENTICATE_URL = "https://api.workos.com/user_management/authenticate"
# WorkOS client_id is public and embedded in the access-token JWT; auto-detected.
WORKOS_CLIENT_ID = os.environ.get("CLINE_WORKOS_CLIENT_ID", "")
UPSTREAM_BASE = os.environ.get("CLINE_API_BASE", "https://api.cline.bot/api/v1")
LISTEN_HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("BRIDGE_PORT", "8317"))
REFRESH_MARGIN_SECONDS = int(os.environ.get("REFRESH_MARGIN", "180"))
REQUEST_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "300"))
# providers.json entries that share the same WorkOS tokens (prefix match):
PROVIDER_PREFIX = "cline-pass"
COOLDOWN_SECONDS = 60  # skip a failed account for this long
PERM_FAIL_TIMEOUT = 3600  # mark unrecoverable accounts for 1h
LOG_FILE = os.environ.get("BRIDGE_LOG", os.path.join(BRIDGE_DIR, "bridge.log"))
MAX_RETRIES = 3
RETRY_BACKOFF = (1, 2, 4)  # seconds


def log(msg):
    line = "[%s] %s" % (datetime.now().isoformat(timespec="seconds"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _jwt_payload(token):
    """Decode a JWT payload segment (no verification - we only read client_id)."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(urlsafe_b64decode(seg.encode()))
    except Exception:
        return {}


# ----------------------------- account store -------------------------------
class AccountStore:
    """Manages OAuth tokens for one ClinePass account (one provider key)."""

    def __init__(self, path, key):
        self.path = path
        self.key = key  # e.g. "cline-pass", "cline-pass-2", "cline-pass-3"
        self.lock = threading.Lock()
        self._mtime = 0.0
        self.data = {}
        self._load()

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            self.data = json.load(fh)
        self._mtime = os.path.getmtime(self.path)

    def _reload_if_changed(self):
        """If the file changed on disk, pick up the newer copy."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime != self._mtime:
            try:
                self._load()
                log("providers.json changed on disk; reloaded tokens for %s" % self.key)
            except Exception as exc:
                log("reload failed for %s (%s); keeping in-memory copy" % (self.key, exc))

    def _auth_block(self):
        entry = self.data.get("providers", {}).get(self.key)
        if not entry:
            raise RuntimeError(
                "No provider entry '%s' found in providers.json" % self.key
            )
        auth = entry.get("settings", {}).get("auth")
        if not auth or not auth.get("refreshToken"):
            raise RuntimeError(
                "No OAuth tokens in '%s' - run `cline auth cline` or add tokens to providers.json" % self.key
            )
        return auth

    def email(self):
        try:
            auth = self._auth_block()
            return auth.get("metadata", {}).get("userInfo", {}).get("email", "unknown")
        except Exception:
            return "unknown"

    def status(self):
        self._reload_if_changed()
        auth = self._auth_block()
        exp_ms = int(auth.get("expiresAt") or 0)
        remaining = int((exp_ms - time.time() * 1000) / 1000)
        email = auth.get("metadata", {}).get("userInfo", {}).get("email", "unknown")
        # ok = token is valid now OR has a future expiry OR has a valid refresh token
        has_rt = bool(auth.get("refreshToken"))
        # Try a lightweight refresh check: if it expires soon and we have a RT,
        # consider it ok (it'll refresh on next access_token() call).
        still_good = remaining > 0 or has_rt
        return {
            "key": self.key,
            "email": email,
            "expires_in_seconds": remaining,
            "ok": still_good,
            "has_refresh_token": has_rt,
        }

    def access_token(self, force_refresh=False):
        with self.lock:
            self._reload_if_changed()
            auth = self._auth_block()
            exp_ms = int(auth.get("expiresAt") or 0)
            now_ms = int(time.time() * 1000)
            if force_refresh or now_ms >= exp_ms - REFRESH_MARGIN_SECONDS * 1000:
                self._refresh(auth)
                auth = self._auth_block()
            tok = auth["accessToken"]
            # normalize: upstream rejects WorkOS tokens without the prefix
            if not tok.startswith("workos:"):
                tok = "workos:" + tok
            return tok

    def _refresh(self, auth):
        client_id = WORKOS_CLIENT_ID or _jwt_payload(auth["accessToken"]).get("client_id")
        if not client_id:
            raise RuntimeError("Could not determine WorkOS client_id from access token")
        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": auth["refreshToken"],
            }
        ).encode()
        req = urllib.request.Request(
            WORKOS_AUTHENTICATE_URL,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        log("Refreshing WorkOS access token for %s..." % self.key)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            is_invalid_grant = '"invalid_grant"' in detail
            err_msg = "WorkOS refresh failed for %s: HTTP %s %s%s" % (
                self.key, exc.code, detail,
                "" if not is_invalid_grant else " - TOKEN REVOKED, re-auth required"
            )
            log(err_msg)
            if is_invalid_grant:
                raise RuntimeError("REVOKED:" + err_msg)
            raise RuntimeError(err_msg)
        except Exception as exc:
            raise RuntimeError("WorkOS refresh failed for %s: %s" % (self.key, exc))

        new_access = payload.get("access_token")
        new_refresh = payload.get("refresh_token") or auth["refreshToken"]
        if not new_access:
            raise RuntimeError("WorkOS refresh returned no access_token")
        expires_in = int(payload.get("expires_in") or 3600)
        # api.cline.bot REQUIRES the CLI's "workos:" scheme prefix on the
        # bearer token (that is how the CLI stores it in providers.json).
        token_prefix = "workos:"

        # Re-read in case the file changed while we were refreshing, then update
        # this account's provider entry.
        self._load()
        entry = self.data.get("providers", {}).get(self.key)
        if not entry:
            raise RuntimeError("providers.json lost '%s' during refresh" % self.key)
        a = entry.get("settings", {}).get("auth")
        if not a:
            raise RuntimeError("providers.json lost auth block for '%s' during refresh" % self.key)
        a["accessToken"] = token_prefix + new_access
        a["refreshToken"] = new_refresh
        a["expiresAt"] = int(time.time() * 1000) + expires_in * 1000
        self._write_atomic()
        log("Token refreshed OK for %s (expires_in=%ss)" % (self.key, expires_in))

    def _write_atomic(self):
        backup = "%s.bak.%s" % (self.path, datetime.now().strftime("%Y%m%d_%H%M%S"))
        try:
            shutil.copy2(self.path, backup)
        except OSError:
            pass
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.path)
        self._mtime = os.path.getmtime(self.path)


# ----------------------------- multi-account manager -----------------------
class MultiAccountStore:
    """Discovers all cline-pass* accounts in providers.json and round-robins them."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._counter = 0
        self._cooldowns = {}  # key -> cooldown_until (unix timestamp)
        self._accounts = {}   # key -> AccountStore
        self._discover()

    def _discover(self):
        """Scan providers.json for all cline-pass* keys and create AccountStores."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            raise RuntimeError("Cannot read providers.json: %s" % exc)

        provs = data.get("providers", {})
        keys = sorted(k for k in provs if k.startswith(PROVIDER_PREFIX))
        if not keys:
            raise RuntimeError(
                "No '%s*' provider entries found in providers.json" % PROVIDER_PREFIX
            )

        for key in keys:
            entry = provs[key]
            auth = entry.get("settings", {}).get("auth", {})
            if auth.get("refreshToken"):
                self._accounts[key] = AccountStore(self.path, key)
                log("discovered account: %s (%s)" % (key, auth.get("metadata", {}).get("userInfo", {}).get("email", "?")))
            else:
                log("skipping %s: no refreshToken" % key)

        if not self._accounts:
            raise RuntimeError("No accounts with refreshToken found in providers.json")

    def _is_cooling_down(self, key):
        until = self._cooldowns.get(key, 0)
        if time.time() < until:
            return True
        if until:
            del self._cooldowns[key]
        return False

    def next_account(self):
        """Return the next available (key, AccountStore) in round-robin order."""
        with self._lock:
            keys = sorted(self._accounts.keys())
            if not keys:
                raise RuntimeError("No accounts available")
            # Try each account in round-robin order, skipping cooldowns
            for _ in range(len(keys)):
                idx = self._counter % len(keys)
                self._counter += 1
                key = keys[idx]
                if not self._is_cooling_down(key):
                    return key, self._accounts[key]
            # All cooling down — return the one with the shortest remaining cooldown
            key = min(keys, key=lambda k: self._cooldowns.get(k, 0))
            return key, self._accounts[key]

    def access_token(self, key, force_refresh=False):
        """Get a token for a specific account."""
        store = self._accounts.get(key)
        if not store:
            raise RuntimeError("Unknown account key: %s" % key)
        return store.access_token(force_refresh=force_refresh)

    def mark_failure(self, key, perm=False):
        """Mark an account as failed.
        
        Args:
            perm: If True (revoked token), use a longer cooldown.
        """
        delay = PERM_FAIL_TIMEOUT if perm else COOLDOWN_SECONDS
        self._cooldowns[key] = time.time() + delay
        log("account %s cooling down for %ds%s" % (key, delay, " (REVOKED)" if perm else ""))

    def status(self):
        """Return status of all accounts."""
        result = []
        for key in sorted(self._accounts.keys()):
            try:
                st = self._accounts[key].status()
                st["cooling_down"] = self._is_cooling_down(key)
                result.append(st)
            except Exception as exc:
                result.append({"key": key, "email": "unknown", "ok": False, "error": str(exc)})
        return result

    @property
    def data(self):
        """Merged data from all accounts (for /v1/models)."""
        # Just read the file once for model list
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {"providers": {}}


STORE = None  # set in main()


def _models_payload():
    """Synthetic /v1/models list (upstream /models 404s). Built from providers.json."""
    models = []
    seen = set()
    try:
        provs = STORE.data.get("providers", {})
        for key, entry in provs.items():
            if not key.startswith(PROVIDER_PREFIX):
                continue
            mid = entry.get("settings", {}).get("model")
            if mid and mid not in seen:
                seen.add(mid)
                models.append(mid)
    except Exception:
        pass
    for fallback in ("cline-pass/kimi-k3", "cline-pass/glm-5.2"):
        if fallback not in seen:
            models.append(fallback)
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 0, "owned_by": "cline-pass"}
            for m in models
        ],
    }


def _open_upstream(path, raw_body, token):
    req = urllib.request.Request(
        UPSTREAM_BASE + path,
        data=raw_body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "User-Agent": "cline-pass-hermes-bridge/2.0",
            "X-Title": "hermes-agent (via cline-pass bridge)",
            "HTTP-Referer": "https://github.com/NousResearch/hermes-agent",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClinePassBridge/2.0"

    def log_message(self, fmt, *args):
        log("http: " + (fmt % args))

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/health":
            try:
                accounts = STORE.status()
                all_ok = all(a.get("ok") for a in accounts)
                any_ok = any(a.get("ok") for a in accounts)
                self._send_json(200 if any_ok else 503, {
                    "ok": any_ok,
                    "all_healthy": all_ok,
                    "accounts": accounts,
                    "upstream": UPSTREAM_BASE,
                })
            except Exception as exc:
                self._send_json(503, {"ok": False, "error": str(exc)})
            return
        if path in ("/v1/models", "/models"):
            self._send_json(200, _models_payload())
            return
        self._send_json(404, {"error": "not found", "path": self.path})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": "not found", "path": self.path})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            want_stream = bool(json.loads(raw).get("stream", True))
        except Exception:
            want_stream = True

        resp = None
        tried_accounts = set()
        account_count = len(STORE._accounts)

        for attempt in range(MAX_RETRIES + account_count + 1):
            # Pick next account
            try:
                acct_key, acct_store = STORE.next_account()
            except RuntimeError as exc:
                self._send_json(503, {
                    "error": {
                        "message": "cline-pass auth unavailable: %s" % exc,
                        "type": "auth_error",
                        "hint": "Run `cline auth cline` to re-login; the bridge picks it up automatically.",
                    }
                })
                return

            # Get token for this account
            try:
                token = STORE.access_token(acct_key)
            except Exception as exc:
                log("token error for %s: %s" % (acct_key, exc))
                is_revoked = "REVOKED" in str(exc)
                STORE.mark_failure(acct_key, perm=is_revoked)
                tried_accounts.add(acct_key)
                if len(tried_accounts) >= account_count:
                    self._send_json(503, {
                        "error": {
                            "message": "all accounts failed token refresh: %s" % exc,
                            "type": "auth_error",
                            "hint": "Run `cline auth cline` to re-login.",
                        }
                    })
                    return
                continue

            # Try upstream
            try:
                resp = _open_upstream("/chat/completions", raw, token)
                log("served by %s (attempt %d)" % (acct_key, attempt + 1))
                break
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if exc.code == 401:
                    log("upstream 401 for %s; forcing token refresh" % acct_key)
                    try:
                        STORE.access_token(acct_key, force_refresh=True)
                        # Retry same account with fresh token
                        token = STORE.access_token(acct_key)
                        resp = _open_upstream("/chat/completions", raw, token)
                        break
                    except Exception as rexc:
                        log("refresh+retry failed for %s: %s" % (acct_key, rexc))
                        STORE.mark_failure(acct_key)
                        tried_accounts.add(acct_key)
                        if len(tried_accounts) >= account_count:
                            self._send_json(401, {
                                "error": {
                                    "message": "all accounts failed: %s" % rexc,
                                    "type": "auth_error",
                                    "hint": "Run `cline auth cline` to re-login.",
                                }
                            })
                            return
                        continue
                if exc.code in (429, 500, 502, 503, 529):
                    log("upstream %s for %s" % (exc.code, acct_key))
                    STORE.mark_failure(acct_key)
                    tried_accounts.add(acct_key)
                    if len(tried_accounts) >= account_count:
                        # All accounts exhausted — pass through last error
                        self.send_response(exc.code)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    continue
                # pass the upstream error through verbatim
                self.send_response(exc.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as exc:  # timeouts, connection resets, DNS...
                log("upstream error for %s: %s" % (acct_key, exc))
                tried_accounts.add(acct_key)
                if len(tried_accounts) >= account_count:
                    self._send_json(502, {"error": {"message": "upstream unreachable: %s" % exc,
                                                    "type": "upstream_error"}})
                    return
                continue

        if resp is None:
            self._send_json(502, {"error": {"message": "no account succeeded", "type": "upstream_error"}})
            return

        # ---- relay response (streaming-aware) ----
        ctype = resp.headers.get("Content-Type", "application/json")
        is_sse = want_stream or "text/event-stream" in ctype
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype)
        if is_sse:
            # close-delimited stream (BaseHTTPRequestHandler can't do chunked)
            self.protocol_version = "HTTP/1.0"
            self.close_connection = True
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            body = resp.read()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main():
    global STORE
    STORE = MultiAccountStore(CLINE_PROVIDERS_JSON)

    if "--check" in sys.argv:
        for st in STORE.status():
            print("auth OK: %s (%s) (access token expires in %ds)" % (st["key"], st["email"], st["expires_in_seconds"]))
        return
    if "--refresh-now" in sys.argv:
        for key in sorted(STORE._accounts.keys()):
            STORE.access_token(key, force_refresh=True)
            st = STORE._accounts[key].status()
            print("refresh OK: %s (%s) (new token expires in %ds)" % (key, st["email"], st["expires_in_seconds"]))
        return

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log("ClinePass multi-account bridge listening on http://%s:%d/v1 -> %s" % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE))
    for st in STORE.status():
        log("account: %s (%s) | access token expires in %ds" % (st["key"], st["email"], st["expires_in_seconds"]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
