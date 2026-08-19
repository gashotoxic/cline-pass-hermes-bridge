# Bridge Resilience & Account Switching Guide

## How the bridge handles account switches

The bridge discovers auth tokens from TWO sources (checked in order):

1. **`~/.cline/data/secrets.json`** — Cline v4.0+ format. Only ONE account.
2. **`~/.cline/data/settings/providers.json`** — Legacy format. Can hold MULTIPLE accounts.

When you log out of Cline and log in as a different account:
- Cline v4+ **overwrites** `secrets.json` with the new account
- The bridge auto-detects the change (watches file mtime) and picks up the new account
- **No bridge restart needed** — it just works

## The revocation problem (what happened on 2026-07-29)

VS Code updated → Cline session revoked → Cline wiped `secrets.json` to `{}` →
bridge found empty file → "no account succeeded" → 502 errors.

**Two fixes are now in place:**

### Fix 1: .bak auto-restore
When `secrets.json` is wiped/empty, the bridge scans `secrets.json.bak.*` files
(newest first) and restores the first one with a valid refresh token.
The restored file is written back as the active `secrets.json`.

### Fix 2: providers.json persist
When a token is refreshed via `secrets.json`, the bridge also writes the fresh
token back to `providers.json` under the matching `cline-pass` account entry.
This means even if `secrets.json` is wiped, the legacy `AccountStore` path
can pick up the token from `providers.json`.

## Model name mapping (cline-free vs cline-pass)

Some models are server-locked to Cline product surfaces and return 403 when
called through the bridge. The bridge now remaps these automatically:

| Bridge model name | Upstream model name | Status |
|-------------------|---------------------|--------|
| `cline-free/glm-5.2` | `cline-pass/glm-5.2` | Remapped |
| `cline-pass/kimi-k3` | `cline-pass/kimi-k3` | Direct |
| `poolside/laguna-s-2.1:free` | `poolside/laguna-s-2.1:free` | Direct |
| `minimax/minimax-m3` | `minimax/minimax-m3` | Direct |
| `xiaomi/mimo-v2.5-pro` | `xiaomi/mimo-v2.5-pro` | Direct |
| `stepfun/step-3.7-flash` | `stepfun/step-3.7-flash` | Direct |

## Token efficiency for large codebases

| Model | Context | Max Output | Reasoning Tax | Best For |
|-------|---------|------------|---------------|----------|
| `poolside/laguna-s-2.1:free` | 262k | 32k | 0% | Large files, delegation |
| `minimax/minimax-m3` | ? | ? | 0% | Large files, delegation |
| `cline-pass/glm-5.2` | 1M | 128k | ~77% | Complex reasoning (wasteful) |
| `cline-pass/kimi-k3` | ? | ? | ~81% | Complex reasoning (wasteful) |

**Recommendation:** Use `poolside/laguna-s-2.1:free` or `minimax/minimax-m3`
for delegation/subagents and large codebase work. They give 100% content output
with no reasoning token overhead.

## If the bridge still fails after account switch

1. Check `~/.cline/data/secrets.json` — should contain the new account
2. Check `~/.cline/data/settings/providers.json` — should have `cline-pass` entry with valid token
3. Check `bridge.log` for which account is being used
4. If both sources are dead, re-auth in Cline VS Code extension

## Key files

| File | Purpose |
|------|---------|
| `~/.cline/data/secrets.json` | Active auth (Cline v4+) |
| `~/.cline/data/settings/providers.json` | Legacy auth (multi-account) |
| `~/cline-pass-hermes-bridge/bridge.log` | Bridge activity log |
| `~/cline-pass-hermes-bridge/cline_pass_bridge.py` | Bridge source |
| `~/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/cline-pass-bridge.vbs` | Auto-start |

## Auto-start

The bridge starts via VBS in Windows Startup folder. Do NOT manually restart
unless the process is actually dead — the bridge auto-reloads tokens from disk.
After `taskkill`, the VBS auto-restarts the bridge with a new PID.

## Fixed 2026-08-04: delegation children failing "Invalid API response: response.choices is None"

**Symptom:** `delegate_task` children always failed with "Invalid API response after 3
retries: response time ~45s" — but the parent (interactive) worked fine.

**Root cause:** the bridge defaulted `want_stream = True` when a request omitted the
`stream` field (line ~705). Delegated children (Hermes's `direct_api_call` non-streaming
path) omit `stream`. The bridge then treated the request as SSE, yet the upstream Cline
gateway (which also saw no stream flag) returned plain JSON. The bridge relayed JSON
bytes through the SSE path → the child's consumer couldn't parse it →
`response.choices is None`.

**Fix (two changes in `cline_pass_bridge.py`):**
1. `want_stream = bool(json.loads(raw).get("stream", False))` — OpenAI spec default is
   FALSE. Mirrors client intent; upstream JSON stays JSON.
2. Unwrap the upstream non-streaming envelope: the Cline gateway wraps non-streaming
   responses in `{"data": {chat.completion}, "success": true}`. Standard OpenAI clients
   (Hermes, raw SDK) expect top-level `choices`. The non-streaming relay branch now
   extracts `payload["data"]` when `choices` is missing at top level.

**Diagnostics added:** the relay logs `relay: req_stream=... want_stream=... ctype=...
is_sse=...` and `relay stream=... sse=... ctype=... unwrapped=... bodylen=...` per
chat-completion request — check `bridge.log` for these when debugging client/upstream
format mismatches.

**Files:** `cline_pass_bridge.py` (patched), `cline_pass_bridge.py.bak-20260804` (pre-fix backup).
**Restart:** kill the pythonw process listening on 8317, then `start-bridge.vbs`.

---

## 2026-08-05 — Decoupled bridge from Hermes venv (no new venv needed)

**Problem:** Every `hermes update` on Windows stalled at the `hermes.exe` replacement
because the bridge (`pythonw.exe` booted from Hermes' own venv) locked the venv files.
Windows won't rename/delete an in-use interpreter, so the updater deferred to reboot.

**Fix:** `start-bridge.vbs` now points at uv's standalone Python
(`C:\Users\Gasho\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe`)
instead of the Hermes venv. The bridge uses **only stdlib** (json, os, shutil, sys,
threading, time, urllib, base64, datetime, http.server) — no third-party packages, so
no new venv was created; zero bloat, zero duplicated deps.

**Backup:** `start-bridge.vbs.bak-20260805` (pre-change launcher).

**Effect:** The bridge no longer locks Hermes' venv, so `hermes update` can swap the
launcher inline without stopping the bridge or rebooting. **Take effect:** next time
the bridge is restarted via `start-bridge.vbs`. Any bridge process still running from
the old venv path should be replaced at the next natural restart.
