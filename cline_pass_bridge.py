#!/usr/bin/env python3
"""
ClinePass -> Hermes auth bridge
================================

Lets Hermes Agent (or any OpenAI-compatible client) use your ClinePass
subscription via the Cline CLI's OAuth login - NO API key involved.

How it works (same pattern as antigravity-claude-proxy):
  1. You sign in once with the Cline CLI:  `cline auth cline`  (browser OAuth via WorkOS)
     Tokens land in  ~/.cline/data/settings/providers.json
  2. This bridge reads those tokens. WorkOS access tokens only live ~1 hour,
     so it refreshes them automatically via WorkOS before expiry and writes the
     rotated tokens back to providers.json so the Cline CLI stays in sync.
  3. It exposes a local OpenAI-compatible endpoint:
         http://127.0.0.1:8317/v1/chat/completions
     Hermes points at it as a `custom_providers` entry with a dummy api_key.
  4. Failure handling: 401 -> force refresh + retry; 429/5xx -> backoff retry;
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
# providers.json entries that share the same WorkOS tokens:
PROVIDER_KEYS = ("cline-pass", "cline")
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

# ----------------------------- token store -------------------------------
class TokenStore:
    """Keeps Cline OAuth tokens fresh; persists rotated tokens back to disk."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self._mtime = 0.0
        self.data = {}
        self._load()

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            self.data = json.load(fh)
        self._mtime = os.path.getmtime(self.path)

    def _reload_if_changed(self):
        """If the Cline CLI refreshed tokens itself, pick up its newer copy."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime != self._mtime:
            try:
                self._load()
                log("providers.json changed on disk; reloaded tokens")
            except Exception as exc:
                log("reload failed (%s); keeping in-memory copy" % exc)

    def _auth_block(self):
        provs = self.data.get("providers", {})
        for key in PROVIDER_KEYS:
            entry = provs.get(key)
            if not entry:
                continue
            auth = entry.get("settings", {}).get("auth")
            if auth and auth.get("refreshToken"):
                return auth
        raise RuntimeError(
            "No Cline OAuth tokens found in providers.json - run `cline auth cline` first."
        )

    def status(self):
        self._reload_if_changed()
        auth = self._auth_block()
        exp_ms = int(auth.get("expiresAt") or 0)
        remaining = int((exp_ms - time.time() * 1000) / 1000)
        email = auth.get("metadata", {}).get("userInfo", {}).get("email", "unknown")
        return {"email": email, "expires_in_seconds": remaining}

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
        log("Refreshing WorkOS access token...")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise RuntimeError(
                "WorkOS refresh failed: HTTP %s %s - re-run `cline auth cline`" % (exc.code, detail)
            )
        except Exception as exc:
            raise RuntimeError("WorkOS refresh failed: %s" % exc)

        new_access = payload.get("access_token")
        new_refresh = payload.get("refresh_token") or auth["refreshToken"]
        if not new_access:
            raise RuntimeError("WorkOS refresh returned no access_token")
        expires_in = int(payload.get("expires_in") or 3600)
        # api.cline.bot REQUIRES the CLI's "workos:" scheme prefix on the
        # bearer token (that is how the CLI stores it in providers.json).
        token_prefix = "workos:"

        # Re-read in case the CLI wrote while we were refreshing, then update
        # every provider entry that shares this session's tokens.
        self._load()
        updated = 0
        for key in PROVIDER_KEYS:
            entry = self.data.get("providers", {}).get(key)
            if not entry:
                continue
            a = entry.get("settings", {}).get("auth")
            if not a:
                continue
            a["accessToken"] = token_prefix + new_access
            a["refreshToken"] = new_refresh
            a["expiresAt"] = int(time.time() * 1000) + expires_in * 1000
            updated += 1
        if not updated:
            raise RuntimeError("providers.json lost its OAuth auth block during refresh")
        self._write_atomic()
        log("Token refreshed OK (expires_in=%ss); wrote back to %d provider entr%s"
            % (expires_in, updated, "y" if updated == 1 else "ies"))

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


STORE = None  # set in main()


def _models_payload():
    """Synthetic /v1/models list (upstream /models 404s). Built from providers.json."""
    models = []
    seen = set()
    try:
        provs = STORE.data.get("providers", {})
        for key in PROVIDER_KEYS:
            entry = provs.get(key) or {}
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
            "User-Agent": "cline-pass-hermes-bridge/1.0",
            "X-Title": "hermes-agent (via cline-pass bridge)",
            "HTTP-Referer": "https://github.com/NousResearch/hermes-agent",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClinePassBridge/1.0"

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
                st = STORE.status()
                st["ok"] = True
                st["upstream"] = UPSTREAM_BASE
                self._send_json(200, st)
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
        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            try:
                token = STORE.access_token()
            except Exception as exc:
                self._send_json(503, {
                    "error": {
                        "message": "cline-pass auth unavailable: %s" % exc,
                        "type": "auth_error",
                        "hint": "Run `cline auth cline` to re-login; the bridge picks it up automatically.",
                    }
                })
                return
            try:
                resp = _open_upstream("/chat/completions", raw, token)
                break
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if exc.code == 401 and not refreshed:
                    refreshed = True
                    log("upstream 401; forcing token refresh and retrying")
                    try:
                        STORE.access_token(force_refresh=True)
                        continue
                    except Exception as rexc:
                        self._send_json(401, {
                            "error": {
                                "message": "token refresh failed: %s" % rexc,
                                "type": "auth_error",
                                "hint": "Run `cline auth cline` to re-login.",
                            }
                        })
                        return
                if exc.code in (429, 500, 502, 503, 529) and attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    log("upstream %s; retry %d/%d in %ds" % (exc.code, attempt + 1, MAX_RETRIES, wait))
                    time.sleep(wait)
                    continue
                # pass the upstream error through verbatim
                self.send_response(exc.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as exc:  # timeouts, connection resets, DNS...
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    log("upstream error %s; retry %d/%d in %ds" % (exc, attempt + 1, MAX_RETRIES, wait))
                    time.sleep(wait)
                    continue
                self._send_json(502, {"error": {"message": "upstream unreachable: %s" % exc,
                                                "type": "upstream_error"}})
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
    STORE = TokenStore(CLINE_PROVIDERS_JSON)

    if "--check" in sys.argv:
        st = STORE.status()
        print("auth OK: %s (access token expires in %ds)" % (st["email"], st["expires_in_seconds"]))
        return
    if "--refresh-now" in sys.argv:
        STORE.access_token(force_refresh=True)
        st = STORE.status()
        print("refresh OK: %s (new token expires in %ds)" % (st["email"], st["expires_in_seconds"]))
        return

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log("ClinePass bridge listening on http://%s:%d/v1 -> %s" % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE))
    st = STORE.status()
    log("account: %s | access token expires in %ds" % (st["email"], st["expires_in_seconds"]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

