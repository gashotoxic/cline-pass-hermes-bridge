#!/usr/bin/env python3
"""
ClinePass -> Hermes auth bridge (multi-account, v3)
=====================================================

Lets Hermes Agent (or any OpenAI-compatible client) use your ClinePass
subscription(s) via the Cline CLI's OAuth login - NO API key involved.

Supports N accounts with round-robin rotation and per-account failover.

v3 changes:
  - Reads from Cline VS Code extension v4.0+ new token storage:
      ~/.cline/data/secrets.json  (key: "cline:clineAccountId")
      ~/.cline/data/globalState.json  (model selection)
  - Backwards-compatible with old ~/.cline/data/settings/providers.json
  - Free/legacy model lists are live-probed before every change;
    see FREE_MODELS / LEGACY_MODELS below.

How it works:
  1. You sign in with the Cline VS Code extension (browser OAuth via WorkOS).
     Tokens land in ~/.cline/data/secrets.json (v4+) or
     ~/.cline/data/settings/providers.json (legacy).
  2. This bridge reads those tokens. WorkOS access tokens only live ~1 hour,
     so it refreshes them automatically via WorkOS before expiry and writes the
     rotated tokens back so the extension stays in sync.
  3. It exposes a local OpenAI-compatible endpoint:
         http://127.0.0.1:8317/v1/chat/completions
     Hermes points at it as a `custom_providers` entry with a dummy api_key.
  4. Multi-account: discovers accounts from both secrets.json (new format)
     and providers.json (old format). Requests rotate round-robin.
     On 401/429 from one account, the bridge fails over to the next.
  5. Failure handling: 401 -> force refresh + retry; 429/5xx -> next account;
     if refresh dies (revoked/expired refresh token) re-run Cline extension login.

Section B (2026-02-16): opencode Zen free tier under zen/<model-id> -
anonymous client attestation (stream:true + a tool named "read" + opencode
client headers), optional OpenCode Console API keys via ZEN_KEYS for
round-robin per-account quota. Live model catalog with churn tracking in
zen-models-state.json. Ported from cline_pass_bridge.py (Windows v1).

Stdlib only. No pip installs required.
"""

import json
import os
import random
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
CLINE_DATA_DIR = os.path.expanduser(os.path.join("~", ".cline", "data"))
CLINE_SECRETS_JSON = os.environ.get(
    "CLINE_SECRETS_JSON",
    os.path.join(CLINE_DATA_DIR, "secrets.json"),
)
CLINE_GLOBAL_STATE_JSON = os.environ.get(
    "CLINE_GLOBAL_STATE_JSON",
    os.path.join(CLINE_DATA_DIR, "globalState.json"),
)
CLINE_PROVIDERS_JSON = os.environ.get(
    "CLINE_PROVIDERS_JSON",
    os.path.join(CLINE_DATA_DIR, "settings", "providers.json"),
)
WORKOS_AUTHENTICATE_URL = "https://api.workos.com/user_management/authenticate"
# WorkOS client_id is public and embedded in the access-token JWT; auto-detected.
WORKOS_CLIENT_ID = os.environ.get("CLINE_WORKOS_CLIENT_ID", "")
UPSTREAM_BASE = os.environ.get("CLINE_API_BASE", "https://api.cline.bot/api/v1")
LISTEN_HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("BRIDGE_PORT", "8317"))
REFRESH_MARGIN_SECONDS = int(os.environ.get("REFRESH_MARGIN", "180"))
REQUEST_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "300"))
COOLDOWN_SECONDS = 60  # skip a failed account for this long
PERM_FAIL_TIMEOUT = 3600  # mark unrecoverable accounts for 1h
# Accounts parked OUT of normal rotation (comma-separated provider keys,
# e.g. BRIDGE_PARKED_ACCOUNTS=cline-pass-1). A parked account is picked only
# when every non-parked account is cooling down - i.e. it is a LAST RESORT,
# so active accounts burn their quota windows first. Per-box policy via env.
# NOTE: the secrets.json account's key is literally 'cline-secrets' (E3) -
# that is the string to park if that store is in use on a box.
PARKED_ACCOUNTS = tuple(
    k.strip() for k in os.environ.get("BRIDGE_PARKED_ACCOUNTS", "").split(",") if k.strip()
)
LOG_FILE = os.environ.get("BRIDGE_LOG", os.path.join(BRIDGE_DIR, "bridge.log"))
MAX_RETRIES = 3
RETRY_BACKOFF = (1, 2, 4)  # seconds

# ------------------------- Section B: opencode Zen -------------------------
# Free tier = anonymous client attestation (no API key, no OAuth). Optional
# OpenCode Console API keys (ZEN_KEYS, comma-separated) add per-account quota
# with round-robin + failover and also unlock paid zen models.
ZEN_ENABLED = os.environ.get("ZEN_ENABLED", "1") == "1"
ZEN_BASE_URL = os.environ.get("ZEN_BASE_URL", "https://opencode.ai/zen/v1")
ZEN_KEYS = []
for _zk in os.environ.get("ZEN_KEYS", "").split(","):
    _zk = _zk.strip()
    if _zk and _zk not in ZEN_KEYS:
        ZEN_KEYS.append(_zk)
ZEN_CLIENT_VERSION = os.environ.get("ZEN_CLIENT_VERSION", "2.0.30")
ZEN_CATALOG_TTL = int(os.environ.get("ZEN_CATALOG_TTL", "900"))  # 15 min

# --- IP-reputation escape hatches -----------------------------------------
# Upstream can flag a *public egress IP* for the free tier (403 FreeTierError
# "...abusive traffic from this IP address"). Both knobs below let zen traffic
# leave through a different IP without touching anything else:
#
#   ZEN_PROXY=http://user:pass@host:port
#       Send zen requests through an HTTP proxy (stdlib ProxyHandler). Only the
#       zen path is affected; Cline traffic keeps going out directly.
#
#   ZEN_RELAY_BASE=http://100.x.y.z:8317/v1
#       Hand zen completions to *another* bridge instance (e.g. this one on a
#       clean connection) over Tailscale/LAN. That bridge does the attestation
#       and tiering; we just forward and relay the stream back. Requires the
#       relay host to listen on a reachable interface (BRIDGE_HOST=0.0.0.0).
ZEN_PROXY = os.environ.get("ZEN_PROXY", "").strip()
ZEN_RELAY_BASE = os.environ.get("ZEN_RELAY_BASE", "").strip().rstrip("/")

# Models offered by the Cline free tier + legacy paid models
# Model name mapping: bridge name -> upstream Cline API name
# Some models are only available via Cline product surfaces and need
# the upstream name to work through the bridge.
MODEL_NAME_MAP = {
    # empty: every slug below is served under its own name.
}

FREE_MODELS = [
    "cline-free/deepseek-v4.1-flash",
    "cline-free/mimo-v2.6-flash",
    "cline-free/muse-spark-1.3-contributor",
    "poolside/laguna-s-2.1:free",
]
LEGACY_MODELS = [
    "cline-pass/kimi-k3",
    "cline-pass/deepseek-v4.1-flash",
    "cline-pass/glm-5.3-flash",
    "cline-pass/glm-5.3",
    "cline-pass/mimo-v2.6-flash",
    "cline-pass/mimo-v2.6-pro",
    "cline-pass/qwen3.8-max",
]


def log(msg):
    line = "[%s] %s" % (datetime.now().isoformat(timespec="seconds"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ===========================================================================
# SECTION B: opencode Zen free tier (https://opencode.ai/zen/v1)
# ===========================================================================
# Recipe reverse-engineered 2026-02-16 by capturing opencode v2.0.30's own
# requests (opencode serve + local listener) and replaying them with curl.
# The free tier is anonymous client attestation - no API key, no OAuth:
#
#   User-Agent: opencode/latest/<ver>/cli       (version not pinned)
#   x-opencode-client: cli
#   x-opencode-session: ses_<9hex>ffe<14alnum>  (exact format, value free)
#   Authorization: Bearer public                (or omit; or a Console key)
#   body: stream: true
#   body: tools[] contains a tool NAMED "read"  (schema/description free)
#
# Hard gates (each verified to hard-fail when violated):
#   - stream:false                          -> FreeTierError
#   - no tool named "read" in tools         -> FreeTierError
#   - missing/misformatted session id       -> FreeTierError
#   - foreign Authorization token           -> FreeTierError
#     (Hermes/pi send "Bearer bridge-managed-oauth" - MUST be stripped here)
#   - sibling session headers (x-opencode-session-id, x-session-affinity,
#     x-session-id) with values != x-opencode-session -> FAIL (never sent)
#
# FreeTierError is ALSO emitted for transient shared-pool admission failures
# (same id+recipe flips PASS<->FAIL across minutes; parallel + rapid bursts
# pass). => retry with backoff; ZEN_KEYS console keys give per-account quota.
# ===========================================================================

ZEN_TOOL_GATE = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Compatibility placeholder. Do not call.",
        "parameters": {"type": "object", "properties": {}},
    },
}

# Last known-good catalog (verified 2026-02-16). Fallback only - the live
# /models fetch + churn tracking in zen-models-state.json is the source of
# truth, so new free models surface automatically and removals get logged.
# Verified working 2026-10-21 (churn-swept). Removed since first wiring:
#   muse-spark-1.2/1.3-contributor-free, space-bunny-free, cat-jam-free,
#   ll-alpha-free   -> upstream 410 ModelDeprecated / unavailable
# Deliberately NOT listed (still returned by the live /models endpoint but
# unusable through the attestation path, so they stay pass-through only):
#   jev-1.13-free              -> 400 ModelProtocolUnsupported (rejects tools;
#                                 the 'read' tool is mandatory for free tier)
#   longcat-2.5-preview-free   -> 429 "Endpoint is unavailable" (degraded)
ZEN_STATIC_CATALOG = [
    "big-pickle",
    "beam-free",
    "mimo-v2.6-flash-free",
    "nemotron-3.5-super-vl-free",
    "nemotron-3.5-lightning-free",
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "minimax-m3.1-flash-free",
]

# jev-1.13-free rejects tool definitions (400 ModelProtocolUnsupported), but the
# free tier requires a tool named "read" -> structurally incompatible, not a
# transient failure. Everything else is left visible even when degraded.
ZEN_EXCLUDE = {"jev-1.13-free"}

ZEN_STATE_FILE = os.path.join(BRIDGE_DIR, "zen-models-state.json")
_ZEN_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_zen_catalog_cache = {"ts": 0.0, "models": []}


def _zen_mint_session():
    """Session ids must match opencode's format: ses_<9hex>ffe<14alnum>."""
    hexpart = "%09x" % random.getrandbits(36)
    tail = "".join(random.choice(_ZEN_ALNUM) for _ in range(14))
    return "ses_%sffe%s" % (hexpart, tail)


class ZenKeyRotator:
    """Round-robin + cooldown over optional OpenCode Console API keys."""

    def __init__(self, keys):
        self._keys = list(keys)
        self._lock = threading.Lock()
        self._counter = 0
        self._cooldowns = {}

    def has_keys(self):
        return bool(self._keys)

    def next_key(self):
        with self._lock:
            n = len(self._keys)
            if n == 0:
                return None
            now = time.time()
            for _ in range(n):
                idx = self._counter % n
                self._counter += 1
                key = self._keys[idx]
                if now >= self._cooldowns.get(key, 0):
                    return key
            return None  # all cooling down -> caller falls back to anonymous

    def mark_failure(self, key, perm=False):
        delay = PERM_FAIL_TIMEOUT if perm else COOLDOWN_SECONDS
        self._cooldowns[key] = time.time() + delay
        log("zen key ...%s cooling down for %ds%s" % (
            key[-4:], delay, " (perm)" if perm else ""))

    def ok_count(self):
        now = time.time()
        return sum(1 for k in self._keys if now >= self._cooldowns.get(k, 0))


ZEN_ROTATOR = ZenKeyRotator(ZEN_KEYS)


class ZenRetriable(Exception):
    """Soft zen failure (flaky pool admission / 200-wrapped error) - retry."""


def _zen_prepare(raw, model):
    """Shape a client request into the attested zen form.

    Returns (upstream_body_bytes, client_want_stream, injected_read).
    """
    body = json.loads(raw)
    body["model"] = model  # strip the zen/ routing prefix before forwarding
    want_stream = bool(body.get("stream"))
    body["stream"] = True  # free tier hard-rejects stream:false
    if not isinstance(body.get("stream_options"), dict):
        body["stream_options"] = {"include_usage": True}
    tools = body.get("tools")
    has_read = isinstance(tools, list) and any(
        isinstance(t, dict) and (t.get("function") or {}).get("name") == "read"
        for t in tools
    )
    injected = False
    if not has_read:
        body["tools"] = (tools if isinstance(tools, list) else []) + [ZEN_TOOL_GATE]
        injected = True
    return json.dumps(body).encode(), want_stream, injected


def _zen_opener():
    """urllib opener honouring ZEN_PROXY (None -> default direct opener)."""
    if not ZEN_PROXY:
        return None
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": ZEN_PROXY, "https": ZEN_PROXY}))


def _open_zen(path, raw_body, token, session_id, method="POST", url_base=None):
    req = urllib.request.Request(
        (url_base or ZEN_BASE_URL) + path,
        data=raw_body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "User-Agent": "opencode/latest/%s/cli" % ZEN_CLIENT_VERSION,
            "x-opencode-client": "cli",
            "x-opencode-session": session_id,
        },
        method=method,
    )
    opener = _zen_opener()
    if opener is not None:
        return opener.open(req, timeout=REQUEST_TIMEOUT)
    return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)


def _zen_sse_collect(resp, model):
    """Accumulate an SSE stream and synthesize one chat.completion object.

    The free tier rejects stream:false, so non-streaming clients get a
    synthesized response. Merges delta.content, delta.reasoning_content and
    delta.tool_calls (by index); keeps usage when present.
    """
    content_parts = []
    reasoning_parts = []
    tool_calls = {}
    finish = None
    usage = None
    mid = None
    created = None
    buf = b""
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                evt = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                continue
            if not isinstance(evt, dict):
                continue
            mid = mid or evt.get("id")
            created = created or evt.get("created")
            if evt.get("usage"):
                usage = evt["usage"]
            for ch in evt.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content_parts.append(d["content"])
                if d.get("reasoning_content"):
                    reasoning_parts.append(d["reasoning_content"])
                for tc in d.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(idx, {
                        "id": None, "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    out = {
        "id": mid or "chatcmpl-zen-bridge",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish or "stop",
        }],
    }
    if usage:
        out["usage"] = usage
    return json.dumps(out).encode("utf-8")


def _zen_load_state():
    try:
        with open(ZEN_STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"models": {}}


def _zen_save_state(state):
    try:
        tmp = ZEN_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, ZEN_STATE_FILE)
    except OSError as exc:
        log("zen state save failed: %s" % exc)


def _zen_track_churn(live_ids):
    """Diff the live catalog against zen-models-state.json.

    New models and removals are logged, and first_seen / last_seen /
    gone_since are kept per model, so catalog churn is auditable and new
    free models appear in /v1/models on the next fetch without a restart.
    Removed ids stay accepted on POST (pass-through - upstream decides).
    """
    state = _zen_load_state()
    seen = state.setdefault("models", {})
    now = int(time.time())
    live = set(live_ids)
    changed = False
    for mid in live_ids:
        ent = seen.get(mid)
        if ent is None:
            seen[mid] = {"first_seen": now, "last_seen": now}
            log("zen catalog: NEW model %s" % mid)
            changed = True
        else:
            if ent.get("gone_since"):
                log("zen catalog: model %s returned (gone since %s)" % (
                    mid, datetime.fromtimestamp(ent["gone_since"]).date()))
                ent.pop("gone_since", None)
                changed = True
            if now - int(ent.get("last_seen", 0)) > 60:
                ent["last_seen"] = now
                changed = True
    for mid, ent in seen.items():
        if mid not in live and not ent.get("gone_since"):
            ent["gone_since"] = now
            log("zen catalog: REMOVED model %s (first seen %s)" % (
                mid, datetime.fromtimestamp(int(ent.get("first_seen", now))).date()))
            changed = True
    if changed:
        _zen_save_state(state)


def _zen_fetch_live_catalog():
    token = ZEN_ROTATOR.next_key() or "public"
    if ZEN_RELAY_BASE:
        # Relay mode: the peer bridge owns the real catalog and advertises it
        # as zen/<id>, so mirror that (strip the prefix back off).
        with urllib.request.urlopen(ZEN_RELAY_BASE + "/models", timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        return [m["id"][4:] for m in payload.get("data", [])
                if isinstance(m, dict) and str(m.get("id", "")).startswith("zen/")]
    req = urllib.request.Request(
        ZEN_BASE_URL + "/models",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": "opencode/latest/%s/cli" % ZEN_CLIENT_VERSION,
            "x-opencode-client": "cli",
            "x-opencode-session": _zen_mint_session(),
        },
        method="GET",
    )
    opener = _zen_opener()
    if opener is not None:
        with opener.open(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    else:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    return [m["id"] for m in payload.get("data", [])
            if isinstance(m, dict) and m.get("id")]


def _zen_catalog():
    """Live free-model catalog: 15-min cache, static fallback, churn-tracked."""
    now = time.time()
    if _zen_catalog_cache["models"] and now - _zen_catalog_cache["ts"] < ZEN_CATALOG_TTL:
        return list(_zen_catalog_cache["models"])
    try:
        ids = _zen_fetch_live_catalog()
    except Exception as exc:
        log("zen catalog fetch failed: %s" % exc)
        return list(_zen_catalog_cache["models"] or ZEN_STATIC_CATALOG)
    # ZEN_EXCLUDE: models upstream still lists but which cannot work through
    # the attestation path. Hidden from /v1/models so downstream clients don't
    # offer a guaranteed-to-fail option; still accepted on POST (pass-through).
    filtered = sorted(
        i for i in ids
        if i != "test" and i not in ZEN_EXCLUDE
        and (i.endswith("-free") or i in ("big-pickle",))
    )
    _zen_track_churn(filtered)
    _zen_catalog_cache["ts"] = now
    _zen_catalog_cache["models"] = filtered
    return list(filtered)


def _zen_health():
    return {
        "enabled": ZEN_ENABLED,
        "tier": "console-keys" if ZEN_ROTATOR.has_keys() else "anonymous",
        "keys_total": len(ZEN_KEYS),
        "keys_ok": ZEN_ROTATOR.ok_count(),
        "catalog_size": len(_zen_catalog_cache["models"]),
        "catalog_age_s": (int(time.time() - _zen_catalog_cache["ts"])
                          if _zen_catalog_cache["ts"] else None),
        "upstream": ZEN_BASE_URL,
        "proxy": bool(ZEN_PROXY),
        "relay": ZEN_RELAY_BASE or None,
    }


def _zen_self_check():
    """Live probe through the full attestation path (used by --zen-check)."""
    catalog = _zen_catalog()
    model = ("glm-5.3-flash-free" if "glm-5.3-flash-free" in catalog
             else (catalog[0] if catalog else "glm-5.3-flash-free"))
    raw = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_completion_tokens": 8,
    }).encode()
    # Two attempts with a short pause: the anonymous shared pool emits
    # transient 429/FreeTierError and a single-attempt probe would be noisy.
    for attempt in range(2):
        try:
            upstream_raw, _, _ = _zen_prepare(raw, model)
            resp = _open_zen("/chat/completions", upstream_raw,
                             ZEN_ROTATOR.next_key() or "public", _zen_mint_session())
            head = resp.read(64)
            if head.lstrip().startswith(b"data:"):
                return True
        except Exception as exc:
            print("zen probe attempt %d error: %s" % (attempt + 1, exc))
        time.sleep(3)
    return False


def _jwt_payload(token):
    """Decode a JWT payload segment (no verification - we only read client_id)."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(urlsafe_b64decode(seg.encode()))
    except Exception:
        return {}


# ----------------------------- account store (legacy providers.json) -------
class AccountStore:
    """Manages OAuth tokens for one ClinePass account (one provider key in providers.json)."""

    def __init__(self, path, key):
        self.path = path
        self.key = key  # e.g. "cline-pass", "cline-pass-2"
        self.lock = threading.Lock()
        self._mtime = 0.0
        self.data = {}
        self._load()

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            self.data = json.load(fh)
        self._mtime = os.path.getmtime(self.path)

    def _reload_if_changed(self):
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
            raise RuntimeError("No provider entry '%s' found in providers.json" % self.key)
        auth = entry.get("settings", {}).get("auth")
        if not auth or not auth.get("refreshToken"):
            raise RuntimeError(
                "No OAuth tokens in '%s' - re-auth required" % self.key
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
        has_rt = bool(auth.get("refreshToken"))
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
                "" if not is_invalid_grant else " - TOKEN REVOKED, re-auth required",
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
        token_prefix = "workos:"

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


# ----------------------------- account store (new secrets.json v4+) --------
class SecretsAccountStore:
    """Manages OAuth tokens from Cline VS Code extension v4.0+ secrets.json.

    The new format stores one account at a time under the key
    'cline:clineAccountId' as a JSON-encoded string with fields:
      idToken, refreshToken, userInfo, expiresAt (seconds), provider, startedAt
    """

    SECRETS_KEY = "cline:clineAccountId"

    def __init__(self, path):
        self.path = path
        self.key = "cline-secrets"  # internal label for round-robin
        self.lock = threading.Lock()
        self._mtime = 0.0
        self._raw = {}
        self._load()

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            self._raw = json.load(fh)
        self._mtime = os.path.getmtime(self.path)

    def _reload_if_changed(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime != self._mtime:
            try:
                self._load()
                log("secrets.json changed on disk; reloaded tokens")
            except Exception as exc:
                log("reload failed for secrets.json (%s); keeping in-memory copy" % exc)

    def _auth_data(self):
        """Parse the secrets.json entry and return a normalized auth dict.

        Falls back to .bak files if the primary secrets.json was wiped
        (e.g. by a VS Code update that revokes the Cline session).
        """
        val = self._raw.get(self.SECRETS_KEY)
        if val:
            data = json.loads(val)  # value is a JSON string
            if data.get("refreshToken"):
                return data
        # Primary missing/invalid — try .bak files
        data = self._try_restore_from_backup()
        if data and data.get("refreshToken"):
            return data
        raise RuntimeError(
            "No '%s' key in secrets.json - re-auth via Cline extension" % self.SECRETS_KEY
        )

    def _try_restore_from_backup(self):
        """Attempt to load auth data from .bak files when primary is empty/invalid."""
        import glob
        for bak in sorted(glob.glob(self.path + ".bak.*"), reverse=True):
            try:
                with open(bak, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                val = raw.get(self.SECRETS_KEY)
                if val:
                    data = json.loads(val)
                    if data.get("refreshToken"):
                        # Restore this backup as the primary
                        self._raw = raw
                        self._write_atomic()
                        self._mtime = os.path.getmtime(self.path)
                        log("secrets.json restored from backup: %s" % bak)
                        return data
            except Exception:
                continue
        return None

    def email(self):
        try:
            data = self._auth_data()
            return data.get("userInfo", {}).get("email", "unknown")
        except Exception:
            return "unknown"

    def status(self):
        self._reload_if_changed()
        data = self._auth_data()
        exp_s = int(data.get("expiresAt") or 0)
        remaining = int(exp_s - time.time())
        email = data.get("userInfo", {}).get("email", "unknown")
        has_rt = bool(data.get("refreshToken"))
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
            data = self._auth_data()
            exp_s = int(data.get("expiresAt") or 0)
            now = time.time()
            if force_refresh or now >= exp_s - REFRESH_MARGIN_SECONDS:
                self._refresh(data)
                data = self._auth_data()
            tok = data["idToken"]
            if not tok.startswith("workos:"):
                tok = "workos:" + tok
            return tok

    def _refresh(self, data):
        # Use the idToken (raw JWT) to extract client_id
        token_for_jwt = data.get("idToken", "")
        client_id = WORKOS_CLIENT_ID or _jwt_payload(token_for_jwt).get("client_id")
        if not client_id:
            raise RuntimeError("Could not determine WorkOS client_id from idToken")
        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": data["refreshToken"],
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
                "" if not is_invalid_grant else " - TOKEN REVOKED, re-auth required",
            )
            log(err_msg)
            if is_invalid_grant:
                raise RuntimeError("REVOKED:" + err_msg)
            raise RuntimeError(err_msg)
        except Exception as exc:
            raise RuntimeError("WorkOS refresh failed for %s: %s" % (self.key, exc))

        new_access = payload.get("access_token")
        new_refresh = payload.get("refresh_token") or data["refreshToken"]
        if not new_access:
            raise RuntimeError("WorkOS refresh returned no access_token")
        expires_in = int(payload.get("expires_in") or 3600)

        # Update secrets.json in-place
        self._load()
        val = self._raw.get(self.SECRETS_KEY)
        if not val:
            raise RuntimeError("secrets.json lost '%s' during refresh" % self.SECRETS_KEY)
        auth = json.loads(val)
        auth["idToken"] = new_access  # no workos: prefix in secrets.json
        auth["refreshToken"] = new_refresh
        auth["expiresAt"] = int(time.time()) + expires_in
        self._raw[self.SECRETS_KEY] = json.dumps(auth)
        self._write_atomic()
        log("Token refreshed OK for %s (expires_in=%ss)" % (self.key, expires_in))
        # Persist fresh token back to providers.json as durable fallback
        self._persist_to_providers(auth, new_access, new_refresh)

    def _persist_to_providers(self, auth, access_token, refresh_token):
        """Write the refreshed token back to providers.json so the legacy
        AccountStore path can survive a secrets.json wipe."""
        try:
            prov_path = CLINE_PROVIDERS_JSON
            if not os.path.exists(prov_path):
                return
            with open(prov_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            email = auth.get("userInfo", {}).get("email", "")
            provs = data.get("providers", {})
            for key, prov in provs.items():
                if not (key == "cline" or key.startswith("cline-pass")):
                    continue
                p_email = prov.get("settings", {}).get("auth", {}).get(
                    "metadata", {}).get("userInfo", {}).get("email", "")
                if p_email == email:
                    prov["settings"]["auth"]["accessToken"] = "workos:" + access_token
                    prov["settings"]["auth"]["refreshToken"] = refresh_token
                    prov["settings"]["auth"]["expiresAt"] = auth["expiresAt"] * 1000
                    prov["settings"]["auth"]["metadata"]["userInfo"] = auth["userInfo"]
                    log("providers.json updated for %s (%s)" % (key, email))
                    break
            with open(prov_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except Exception as exc:
            log("providers.json persist failed: %s" % exc)

    def _write_atomic(self):
        backup = "%s.bak.%s" % (self.path, datetime.now().strftime("%Y%m%d_%H%M%S"))
        try:
            shutil.copy2(self.path, backup)
        except OSError:
            pass
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._raw, fh, indent=2)
        os.replace(tmp, self.path)
        self._mtime = os.path.getmtime(self.path)


# ----------------------------- multi-account manager -----------------------
class MultiAccountStore:
    """Discovers all accounts from secrets.json (new) + providers.json (legacy)."""

    def __init__(self, secrets_path, providers_path):
        self.secrets_path = secrets_path
        self.providers_path = providers_path
        # RLock: _is_cooling_down() is called while next_account() holds this
        # lock, so the cooldown guards below must be re-entrant (B2 audit fix).
        self._lock = threading.RLock()
        self._counter = 0
        self._cooldowns = {}  # key -> cooldown_until (unix timestamp)
        self._accounts = {}   # key -> AccountStore or SecretsAccountStore
        self._discover()
        self._validate_parked()

    def _discover(self):
        # 1. Try secrets.json (new format, one account)
        if self.secrets_path and os.path.exists(self.secrets_path):
            try:
                store = SecretsAccountStore(self.secrets_path)
                data = store._auth_data()
                email = data.get("userInfo", {}).get("email", "?")
                self._accounts[store.key] = store
                log("discovered secrets.json account: %s (%s)" % (store.key, email))
            except Exception as exc:
                log("secrets.json: %s" % exc)

        # 2. Try providers.json (legacy format, multiple accounts)
        if self.providers_path and os.path.exists(self.providers_path):
            try:
                with open(self.providers_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as exc:
                log("providers.json read error: %s" % exc)
                data = {}

            provs = data.get("providers", {})
            for key in sorted(provs):
                if not (key == "cline" or key.startswith("cline-pass")):
                    continue
                auth = provs[key].get("settings", {}).get("auth", {})
                if auth.get("refreshToken"):
                    try:
                        store = AccountStore(self.providers_path, key)
                        self._accounts[key] = store
                        log("discovered providers.json account: %s (%s)" % (
                            key, auth.get("metadata", {}).get("userInfo", {}).get("email", "?")))
                    except Exception as exc:
                        log("skipping %s: %s" % (key, exc))

        if not self._accounts:
            raise RuntimeError(
                "No accounts found in secrets.json or providers.json - "
                "re-auth via Cline VS Code extension"
            )

    def _validate_parked(self):
        """E1/E2 audit fix: a typo'd BRIDGE_PARKED_ACCOUNTS silently defeats the
        burn-priority policy - warn loudly instead. Also log the effective set."""
        if not PARKED_ACCOUNTS:
            return
        unknown = [k for k in PARKED_ACCOUNTS if k not in self._accounts]
        for k in unknown:
            log("WARNING: BRIDGE_PARKED_ACCOUNTS entry %r matches no discovered "
                "account key - ignored. Valid keys: %s"
                % (k, ", ".join(sorted(self._accounts))))
        known = sorted(k for k in PARKED_ACCOUNTS if k in self._accounts)
        if known:
            log("parked accounts (LAST RESORT only): %s" % ", ".join(known))
        active = [k for k in self._accounts if k not in PARKED_ACCOUNTS]
        if not active:
            log("WARNING: every account is parked - burn-priority policy is "
                "VOID, plain rotation applies (E2)")

    def _is_cooling_down(self, key):
        with self._lock:
            until = self._cooldowns.get(key, 0)
            if time.time() < until:
                return True
            if until:
                del self._cooldowns[key]
            return False

    def next_account(self):
        with self._lock:
            keys = sorted(self._accounts.keys())
            if not keys:
                raise RuntimeError("No accounts available")
            # Parked accounts (BRIDGE_PARKED_ACCOUNTS) are a LAST RESORT: rotate
            # over non-parked accounts first so their quota windows burn first,
            # and only fall through to parked ones when every active is cooling.
            active = [k for k in keys if k not in PARKED_ACCOUNTS]
            parked = [k for k in keys if k in PARKED_ACCOUNTS]
            for pool in (active, parked):
                if not pool:
                    continue
                # Advance the counter ONCE per selection and scan from that spot:
                # a counter stepped per examined slot aliases across pools of
                # unequal size and can pin a reserve account forever (B1 fix).
                start = self._counter % len(pool)
                for i in range(len(pool)):
                    key = pool[(start + i) % len(pool)]
                    if not self._is_cooling_down(key):
                        self._counter += 1
                        if key in PARKED_ACCOUNTS:
                            log("PARKED FALLBACK: all %d active account(s) "
                                "cooling, serving reserve %s" % (len(active), key))
                        return key, self._accounts[key]
            # Everything is cooling: soonest-to-recover, but never spend the
            # reserve while an active account is closer to recovery (B3 fix).
            for group in (active, parked):
                if group:
                    key = min(group, key=lambda k: self._cooldowns.get(k, 0))
                    return key, self._accounts[key]
            key = min(keys, key=lambda k: self._cooldowns.get(k, 0))
            return key, self._accounts[key]

    def access_token(self, key, force_refresh=False):
        store = self._accounts.get(key)
        if not store:
            raise RuntimeError("Unknown account key: %s" % key)
        return store.access_token(force_refresh=force_refresh)

    def mark_failure(self, key, perm=False):
        delay = PERM_FAIL_TIMEOUT if perm else COOLDOWN_SECONDS
        with self._lock:
            self._cooldowns[key] = time.time() + delay
        log("account %s cooling down for %ds%s" % (key, delay, " (REVOKED)" if perm else ""))

    def status(self):
        result = []
        for key in sorted(self._accounts.keys()):
            try:
                st = self._accounts[key].status()
                st["cooling_down"] = self._is_cooling_down(key)
                st["parked"] = key in PARKED_ACCOUNTS
                result.append(st)
            except Exception as exc:
                result.append({"key": key, "email": "unknown", "ok": False,
                               "error": str(exc),
                               "cooling_down": self._is_cooling_down(key),
                               "parked": key in PARKED_ACCOUNTS})
        return result

    @property
    def data(self):
        try:
            with open(self.providers_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {"providers": {}}


STORE = None  # set in main()


def _models_payload():
    """Synthetic /v1/models list (upstream /models 404s)."""
    models = []
    seen = set()

    # REMOVED 2026-10-01 (Windows): globalState.json act/plan model surfacing
    # advertised cline-free/longcat-2.0 (live 404) - GUI-selection artifact, not a
    # catalog source. globalState.json absent on the Kali boxes anyway.

    # Try models from providers.json
    try:
        provs = STORE.data.get("providers", {})
        for key, entry in provs.items():
            if not (key == "cline" or key.startswith("cline-pass")):
                continue
            mid = entry.get("settings", {}).get("model")
            if mid and mid not in seen:
                seen.add(mid)
                models.append(mid)
    except Exception:
        pass

    # Add free-tier and legacy fallbacks
    for mid in FREE_MODELS + LEGACY_MODELS:
        if mid not in seen:
            seen.add(mid)
            models.append(mid)

    data = [
        {"id": m, "object": "model", "created": 0, "owned_by": "cline-pass"}
        for m in models
    ]
    if ZEN_ENABLED:
        for zm in _zen_catalog():
            data.append({"id": "zen/" + zm, "object": "model", "created": 0,
                         "owned_by": "opencode-zen"})
    return {"object": "list", "data": data}


def _open_upstream(path, raw_body, token):
    req = urllib.request.Request(
        UPSTREAM_BASE + path,
        data=raw_body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "User-Agent": "Cline/3.0.39",
            "X-Title": "Cline",
            "HTTP-Referer": "https://cline.bot",
            "X-CLIENT-TYPE": "cline-sdk",
            "X-CLIENT-VERSION": "3.0.39",
            "X-PLATFORM": "cli",
            "X-PLATFORM-VERSION": "3.0.39",
            "X-CORE-VERSION": "3.0.39",
            "X-IS-MULTIROOT": "false",
            "X-Task-ID": "bridge-session",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)


def _unwrap_data_envelope(body):
    """Return a completion body unwrapped from the upstream `{"data": {...}}` envelope.

    Cline's upstream wraps NON-streaming completions in `{"data": {...}}`. OpenAI's
    SDK parses that into `ChatCompletion(choices=None)`, and any strict consumer then
    rejects it ("Expected object with .choices[0].message"). SSE is already the standard
    shape and is relayed untouched. Anything that is not a JSON dict with a nested dict
    ``data`` carrying ``choices`` is passed through byte-for-byte (error bodies, etc.).
    """
    try:
        parsed = json.loads(body)
        inner = parsed.get("data") if isinstance(parsed, dict) else None
        if isinstance(inner, dict) and isinstance(inner.get("choices"), list):
            return json.dumps(inner).encode()
    except Exception:
        pass
    return body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClinePassBridge/3.0"

    def log_message(self, fmt, *args):
        log("http: " + (fmt % args))

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------- Section B: opencode Zen ---------------------
    def _handle_zen(self, model, raw):
        """Route zen/<model> requests to opencode Zen. Never touches Cline state."""
        if not ZEN_ENABLED:
            self._send_json(404, {"error": {"message": "zen section disabled",
                                            "type": "disabled"}})
            return
        if ZEN_RELAY_BASE:
            # Relay mode: hand the completion to a peer bridge (clean egress).
            # It owns attestation/tiering, so forward the body untouched except
            # for re-adding the zen/ prefix it routes on.
            try:
                rbody = json.loads(raw)
                rbody["model"] = "zen/" + model
                self._zen_relay_forward(model, json.dumps(rbody).encode())
            except Exception as exc:
                log("zen/%s relay error: %s" % (model, exc))
                self._send_json(502, {"error": {"message": "zen relay failed: %s" % exc,
                                                "type": "upstream_error"}})
            return
        if model not in _zen_catalog():
            # Unknown to the live catalog: brand-new upstream model not yet
            # cached, or a removed one. Pass through anyway - upstream decides.
            log("zen/%s not in live catalog; passing through" % model)
        try:
            upstream_raw, want_stream, injected = _zen_prepare(raw, model)
        except Exception as exc:
            self._send_json(400, {"error": {"message": "invalid request body: %s" % exc,
                                            "type": "invalid_request"}})
            return
        if injected:
            log("zen/%s: injected compatibility 'read' tool" % model)

        # Tier 1: console keys (round-robin). Tier 2: anonymous attestation.
        attempts = []
        seen_tokens = set()
        for _ in range(len(ZEN_KEYS) + 1):
            key = ZEN_ROTATOR.next_key()
            if key is None or key in seen_tokens:
                break
            seen_tokens.add(key)
            attempts.append(("key", key))
        attempts.extend(("anon", "public") for _ in range(MAX_RETRIES))

        last_code = 503
        last_body = b'{"error":{"message":"zen unavailable","type":"upstream_error"}}'
        retried = 0
        for kind, token in attempts:
            try:
                resp = _open_zen("/chat/completions", upstream_raw, token,
                                 _zen_mint_session())
                ctype = resp.headers.get("Content-Type", "")
                if "text/event-stream" not in ctype:
                    # FreeTierError can arrive as a 200-wrapped JSON error.
                    body = resp.read()
                    if b'"error"' in body:
                        raise ZenRetriable(body[:200].decode("utf-8", "replace"))
                    # Unexpected non-SSE success: relay verbatim.
                    self.send_response(resp.status)
                    self.send_header("Content-Type", ctype or "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                log("zen/%s served (%s tier)" % (model, kind))
                self._zen_relay(resp, model, want_stream)
                return
            except ZenRetriable as exc:
                log("zen/%s soft failure (%s tier): %s" % (model, kind, exc))
                retried += 1
            except urllib.error.HTTPError as exc:
                body = exc.read()
                log("zen/%s upstream HTTP %s (%s tier): %s" % (
                    model, exc.code, kind, body[:200].decode("utf-8", "replace")))
                if kind == "key":
                    ZEN_ROTATOR.mark_failure(token, perm=(exc.code == 401))
                if b"FreeTierError" in body or exc.code in (
                        401, 402, 403, 429, 500, 502, 503, 529):
                    last_code, last_body = exc.code, body
                    retried += 1
                else:
                    # Genuine client/model error (400/404/422...): surface as-is.
                    self.send_response(exc.code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            except Exception as exc:
                log("zen/%s upstream error (%s tier): %s" % (model, kind, exc))
                last_code, last_body = 502, json.dumps({"error": {
                    "message": "zen upstream unreachable: %s" % exc,
                    "type": "upstream_error"}}).encode()
                retried += 1
            if retried:
                time.sleep(RETRY_BACKOFF[min(retried - 1, len(RETRY_BACKOFF) - 1)])

        self.send_response(last_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(last_body)))
        self.end_headers()
        self.wfile.write(last_body)

    def _zen_relay_forward(self, model, body):
        """POST to the peer bridge and copy its response through verbatim."""
        req = urllib.request.Request(
            ZEN_RELAY_BASE + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + (ZEN_KEYS[0] if ZEN_KEYS else "relay"),
                     "Accept": "*/*"},
            method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
        except urllib.error.HTTPError as exc:
            eb = exc.read()
            log("zen/%s relay peer HTTP %s: %s" % (model, exc.code,
                eb[:160].decode("utf-8", "replace")))
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(eb)))
            self.end_headers()
            self.wfile.write(eb)
            return
        ctype = resp.headers.get("Content-Type", "text/event-stream")
        log("zen/%s served via relay %s" % (model, ZEN_RELAY_BASE))
        if "text/event-stream" in ctype:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
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
            body_out = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype or "application/json")
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)

    def _zen_relay(self, resp, model, want_stream):
        if want_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
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
            body = _zen_sse_collect(resp, "zen/" + model)
            log("zen/%s synthesized non-stream response (%d bytes)" % (model, len(body)))
            self.send_response(200)
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
                    "zen": _zen_health(),
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
        # --- Section B: opencode Zen free tier (zen/<model>) ----------------
        # Branches BEFORE any Cline account logic; zen failures never touch
        # Cline cooldowns/state. Foreign Authorization (Hermes/pi send
        # "Bearer bridge-managed-oauth") is replaced inside _open_zen.
        try:
            _zen_model = json.loads(raw).get("model", "")
        except Exception:
            _zen_model = ""
        if isinstance(_zen_model, str) and _zen_model.startswith("zen/"):
            self._handle_zen(_zen_model[4:], raw)
            return
        # --------------------------------------------------------------------
        # Remap model name if needed (e.g. cline-free/* -> cline-pass/*)
        try:
            req_body = json.loads(raw)
            model = req_body.get("model", "")
            mapped = MODEL_NAME_MAP.get(model, model)
            if mapped != model:
                req_body["model"] = mapped
                raw = json.dumps(req_body).encode()
                log("model remapped: %s -> %s" % (model, mapped))
        except Exception:
            pass
        try:
            want_stream = bool(json.loads(raw).get("stream", True))
        except Exception:
            want_stream = True

        resp = None
        tried_accounts = set()
        account_count = len(STORE._accounts)

        for attempt in range(MAX_RETRIES + account_count + 1):
            try:
                acct_key, acct_store = STORE.next_account()
            except RuntimeError as exc:
                self._send_json(503, {
                    "error": {
                        "message": "cline-pass auth unavailable: %s" % exc,
                        "type": "auth_error",
                        "hint": "Re-auth via Cline VS Code extension.",
                    }
                })
                return

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
                            "hint": "Re-auth via Cline VS Code extension.",
                        }
                    })
                    return
                continue

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
                                    "hint": "Re-auth via Cline VS Code extension.",
                                }
                            })
                            return
                        continue
                if exc.code in (429, 500, 502, 503, 529):
                    log("upstream %s for %s" % (exc.code, acct_key))
                    STORE.mark_failure(acct_key)
                    tried_accounts.add(acct_key)
                    if len(tried_accounts) >= account_count:
                        self.send_response(exc.code)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    continue
                self.send_response(exc.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as exc:
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
        head = resp.read(1024)
        probe = head.lstrip()[:7]
        # Trust the PAYLOAD, not the request guess: `want_stream` defaults True when the
        # client omits `stream`, but upstream still answers with a completed JSON body in
        # that case -- routing it down the SSE branch relayed the upstream `{"data": ...}`
        # wrapper raw, which OpenAI SDK clients parse as ChatCompletion(choices=None).
        is_sse = probe.startswith((b"data:", b"event:", b"retry:")) or (
            "text/event-stream" in ctype and not probe.startswith(b"{"))
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype)
        if is_sse:
            self.protocol_version = "HTTP/1.0"
            self.close_connection = True
            self.end_headers()
            try:
                self.wfile.write(head)
                self.wfile.flush()
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            body = _unwrap_data_envelope(head + resp.read())
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main():
    global STORE
    if "--zen-check" in sys.argv:
        ok = _zen_self_check()
        print("zen probe: %s" % ("PASS" if ok else "FAIL"))
        sys.exit(0 if ok else 1)
    STORE = MultiAccountStore(CLINE_SECRETS_JSON, CLINE_PROVIDERS_JSON)

    if "--check" in sys.argv:
        for st in STORE.status():
            print("auth OK: %s (%s) (access token expires in %ds)" % (
                st["key"], st["email"], st["expires_in_seconds"]))
        return
    if "--refresh-now" in sys.argv:
        for key in sorted(STORE._accounts.keys()):
            STORE.access_token(key, force_refresh=True)
            st = STORE._accounts[key].status()
            print("refresh OK: %s (%s) (new token expires in %ds)" % (
                key, st["email"], st["expires_in_seconds"]))
        return

    # Refuse to double-start: SO_REUSEADDR lets a second instance bind the
    # port silently. If /health answers at all - even with an error status -
    # another bridge is alive on this port; bail out.
    try:
        urllib.request.urlopen(
            "http://%s:%d/health" % (LISTEN_HOST, LISTEN_PORT), timeout=3)
        log("FATAL: another bridge instance already serves %s:%d; refusing to start" % (
            LISTEN_HOST, LISTEN_PORT))
        sys.exit(1)
    except urllib.error.HTTPError as _he:
        log("FATAL: %s:%d answered HTTP %s - another bridge instance is alive; "
            "refusing to start" % (LISTEN_HOST, LISTEN_PORT, _he.code))
        sys.exit(1)
    except Exception:
        pass  # connection refused etc. -> port is free

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log("ClinePass v3 multi-account bridge listening on http://%s:%d/v1 -> %s" % (
        LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE))
    log("zen section: %s (tier=%s, console keys=%d, upstream=%s)" % (
        "enabled" if ZEN_ENABLED else "DISABLED",
        "console-keys" if ZEN_ROTATOR.has_keys() else "anonymous",
        len(ZEN_KEYS), ZEN_BASE_URL))
    for st in STORE.status():
        log("account: %s (%s) | access token expires in %ds" % (
            st["key"], st["email"], st["expires_in_seconds"]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
