# ClinePass → Hermes Agent: OAuth Subscription Bridge

> ## Quick start (fresh machine)
>
> ```powershell
> git clone https://github.com/gashotoxic/cline-pass-hermes-bridge.git
> cd cline-pass-hermes-bridge
>
> # 1. Sign in with the Cline CLI (browser OAuth - creates the tokens we reuse)
> npm install -g cline
> cline auth cline            # choose your ClinePass account
>
> # 2. Start the bridge (auto-refreshes the OAuth token, serves :8317/v1)
> .\Start-Bridge.ps1          # add -Install once to auto-start at logon
>
> # 3. Point any OpenAI-compatible client at it
> #    base_url: http://127.0.0.1:8317/v1   api_key: anything (dummy)
> ```
>
> Hermes Agent example: add to `config.yaml` under `custom_providers`:
> ```yaml
>   - name: cline-pass
>     base_url: http://127.0.0.1:8317/v1
>     api_key: bridge-managed-oauth   # dummy; real OAuth token is injected by the bridge
>     model: cline-pass/kimi-k3
>     api_mode: chat_completions
> ```
> then: `hermes -z "hello" --provider custom:cline-pass -m cline-pass/kimi-k3`
>
> Requires: Windows, Python 3.8+ (stdlib only), Cline CLI signed in with an
> active ClinePass subscription. Linux/macOS work too — set
> `CLINE_PROVIDERS_JSON` if your Cline data dir differs.
>
> ---
>


**Goal.** Use a **ClinePass subscription** (Cline's $9.99/mo plan for open coding
models) with the **Hermes Agent** CLI — via the subscription's **OAuth login**,
*not* an API key — following the same pattern as
[`opencode-google-antigravity-auth`](https://github.com/shekohex/opencode-google-antigravity-auth)
/ `antigravity-claude-proxy`, including automatic recovery when auth fails.

**Status: working and verified end-to-end** (2026-07-20). Hermes replied
`HERMES VIA CLINEPASS OK` through the bridge using model `cline-pass/kimi-k3`.

---

## Architecture

```
┌──────────────┐   OpenAI-compatible    ┌─────────────────────────┐   Bearer workos:<JWT>   ┌──────────────────────┐
│ Hermes Agent │ ─────────────────────► │ cline_pass_bridge.py    │ ──────────────────────► │ api.cline.bot        │
│ custom prov. │  http://127.0.0.1:8317 │ localhost OAuth bridge  │  /api/v1/chat/completions│ (ClinePass backend) │
└──────────────┘                        └──────────┬──────────────┘                         └──────────────────────┘
                                                   │ reads / writes back                            ▲
                                                   ▼                                                │ refresh_token grant
                                        ~/.cline/data/settings/                     ┌───────────────┴──────────┐
                                        providers.json  (tokens) ◄────────────────► │ WorkOS user_management   │
                                                   ▲                                │ /authenticate            │
                                                   │ written by                     └──────────────────────────┘
                                        `cline auth cline` (browser OAuth, one time)
```

* The **Cline CLI** performs the browser OAuth flow (WorkOS AuthKit) and stores
  `accessToken` / `refreshToken` / `expiresAt` in
  `C:\Users\ADMIN\.cline\data\settings\providers.json` (provider `cline-pass`).
* The **bridge** (this folder) reads those tokens, keeps the access token fresh
  (WorkOS tokens live **1 hour**), and forwards Hermes' requests to
  `https://api.cline.bot/api/v1/chat/completions`.
* **Hermes** sees a plain OpenAI-compatible endpoint at
  `http://127.0.0.1:8317/v1` — the `api_key` in its config is a dummy; the real
  auth is injected by the bridge from the OAuth store.

---

## Key findings from the investigation (why it is built this way)

1. **ClinePass auth = WorkOS OAuth.** Signing in with `cline auth cline` stores
   a WorkOS-issued RS256 JWT in `providers.json`. The JWT's `iss` is
   `https://api.workos.com/user_management/client_01K3A541FN8TA3EPPHTD2325AR`.
2. **Access tokens expire after 1 hour** (`exp - iat = 3600`). A static copy of
   the token in Hermes would die within the hour → a refresh bridge is
   mandatory. Refresh = `POST https://api.workos.com/user_management/authenticate`
   with `{grant_type: "refresh_token", client_id, refresh_token}`. The client_id
   is public and is read out of the JWT payload itself. Refresh tokens **rotate**
   on every use, so the bridge writes the new pair back to `providers.json`
   (with a timestamped backup) so the Cline CLI keeps working too.
3. **`api.cline.bot` requires a `workos:` prefix on the bearer token.**
   The CLI stores the token as `workos:eyJhbG...`. Sending the raw JWT gives
   `401 Unauthorized`. The bridge normalizes this on every read and write.
   (This cost one debugging round — the raw-JWT attempt failed with 401.)
4. **The Cline API is OpenAI-compatible**: `POST /api/v1/chat/completions`,
   SSE streaming, tool calling, `provider/model` model IDs. ClinePass models
   use IDs like `cline-pass/kimi-k3` and `cline-pass/glm-5.2` (the same IDs the
   Cline CLI stores in its config). `GET /api/v1/models` returns 404, so the
   bridge serves a synthetic `/v1/models` built from the Cline config.
5. **Hermes supports custom OpenAI-compatible providers** natively via
   `custom_providers` in `C:\Users\ADMIN\AppData\Local\hermes\config.yaml`
   (`name`, `base_url`, `api_key`, `model`, `api_mode: chat_completions`), and
   an automatic `fallback_model` used on 429/529/503/connection errors.

---

## Files in this folder

| File | Purpose |
|---|---|
| `cline_pass_bridge.py` | The bridge. Stdlib-only Python 3, no pip installs. |
| `Start-Bridge.ps1` | Launcher + watchdog: no-op if healthy; otherwise restarts the bridge, forces a token refresh, and tells you when a manual re-login is needed. `-Install` registers a logon Scheduled Task (`ClinePassHermesBridge`). |
| `bridge.log` | Runtime log (requests, refreshes, retries). |
| `stdout.log` / `stderr.log` | Process output when started with redirected streams. |
| `hermes-test.log` | Proof of the end-to-end test (`HERMES VIA CLINEPASS OK`). |
| `README.md` | This report. |

## Daily use

1. **Start the bridge** (once per boot, or auto-start if installed):
   ```powershell
   H:\cline-pass-hermes-bridge\Start-Bridge.ps1
   ```
2. **Use it from Hermes**:
   ```powershell
   # one-shot
   hermes -z "your prompt" --provider custom:cline-pass -m cline-pass/kimi-k3
   # or pick it interactively
   hermes model
   ```
   To make ClinePass the *default* model, set in `hermes model` (or config.yaml):
   `model.default: cline-pass/kimi-k3`, `model.provider: cline-pass`,
   `model.base_url: http://127.0.0.1:8317/v1`.
3. **Health check**: `curl http://127.0.0.1:8317/health`
   → `{"ok": true, "email": "...", "expires_in_seconds": ...}`

## What was changed on this machine

1. `C:\Users\ADMIN\AppData\Local\hermes\config.yaml` (backup:
   `config.yaml.bak.bridge_<timestamp>` in the same folder):
   * added `custom_providers` entry `cline-pass` → `http://127.0.0.1:8317/v1`
     with dummy key `bridge-managed-oauth`, model `cline-pass/kimi-k3`;
   * added an active `fallback_model` (xiaomi `mimo-v2.5-pro`, key from
     `.env` `XIAOMI_API_KEY`) so Hermes fails over automatically if the
     bridge is down or ClinePass errors (429/529/503/connection).
2. `providers.json` (Cline CLI) gets its `cline-pass`/`cline` token triple
   (`accessToken`, `refreshToken`, `expiresAt`) rewritten by the bridge on
   every refresh; a `providers.json.bak.<timestamp>` backup is kept next to it.

## Failure handling (the "if it fails" part)

| Failure | What happens |
|---|---|
| Access token expired | Bridge refreshes silently via WorkOS before forwarding. |
| Upstream 401 | Bridge force-refreshes once and retries the request. |
| Upstream 429/5xx | Bridge retries with backoff (1s → 2s → 4s). |
| Bridge down / unreachable | Hermes' `fallback_model` (xiaomi) takes over automatically. |
| Refresh token rejected (revoked / logged out elsewhere) | Bridge returns a JSON `auth_error` telling you to run `cline auth cline`. `Start-Bridge.ps1` prints the same hint. After re-login the bridge picks up the new tokens automatically (it re-reads the file on change) — no restart needed. |
| Machine reboot | Re-run `Start-Bridge.ps1`, or install the logon task once: `Start-Bridge.ps1 -Install`. |

Useful manual commands:
```powershell
python H:\cline-pass-hermes-bridge\cline_pass_bridge.py --check         # token status
python H:\cline-pass-hermes-bridge\cline_pass_bridge.py --refresh-now   # force refresh
Get-Content H:\cline-pass-hermes-bridge\bridge.log -Tail 20 -Wait       # live log
```


---

## How this was set up (step-by-step record)

1. **Identified the moving parts.** Hermes Agent (NousResearch) lives at
   `%LOCALAPPDATA%\hermes` with `config.yaml` + `auth.json`; Cline CLI 3.0.46
   (npm global) stores OAuth state under `~\.cline\data\`.
2. **Read the reference implementation** (`opencode-google-antigravity-auth`):
   same idea — reuse a first-party OAuth login in a third-party agent, with
   automatic token refresh and endpoint fallback.
3. **Found the existing ClinePass session** in
   `~\.cline\data\settings\providers.json`: provider `cline-pass` with a WorkOS
   access token (`workos:`-prefixed JWT), refresh token, 1-hour expiry, and the
   model IDs `cline-pass/kimi-k3`, `cline-pass/glm-5.2`.
4. **Verified the API shape** from docs.cline.bot: OpenAI-compatible
   `POST https://api.cline.bot/api/v1/chat/completions`, Bearer auth; account
   OAuth tokens are accepted (same as the CLI uses).
5. **Proved the token works** with a direct REST call (200 OK, cost 0 under the
   ClinePass subscription).
6. **Built the bridge** (`cline_pass_bridge.py`): token store + WorkOS refresh
   + atomic write-back + OpenAI-compatible proxy with retry/fallback logic.
7. **Debugged the one real gotcha**: refreshed tokens were rejected with 401
   because `api.cline.bot` requires the `workos:` prefix that the CLI stores.
   Fixed by always persisting/sending the prefix (verified: prefixed → 200).
8. **Tested**: `--check`, `--refresh-now` (rotation + write-back), `/health`,
   `/v1/models`, non-streaming and SSE-streaming chat completions — all pass.
9. **Wired Hermes**: `custom_providers` entry + `fallback_model` failover;
   validated the YAML with Hermes' own venv Python.
10. **End-to-end proof**: `hermes -z "Reply with exactly: HERMES VIA CLINEPASS OK"
    --provider custom:cline-pass -m cline-pass/kimi-k3` → output exactly
    `HERMES VIA CLINEPASS OK` (see `hermes-test.log`; bridge log shows the 200).

## Security notes

* OAuth tokens never leave this machine except to `api.workos.com` (refresh)
  and `api.cline.bot` (inference). The bridge binds to `127.0.0.1` only.
* No tokens or API keys are written into this README; they remain in the
  tools' own stores (`providers.json`, `config.yaml`, `.env`).
* Note: `config.yaml` / `providers.json` contain plaintext credentials — keep
  them out of backups/shares. Consider rotating the chutes/cloudflare keys if
  this machine's logs have been shared.
* Heads-up on terms of service: using a subscription's OAuth session outside
  the vendor's own client can be against the provider's ToS (the Antigravity
  plugin README carries the same warning, and there are reports of account
  enforcement for that service). Using your own ClinePass session with a local
  client is your call — review Cline's terms and use at your own discretion.

