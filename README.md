# ClinePass OAuth Bridge for Hermes Agent (Multi-Account)

Use **one or more ClinePass subscriptions** with [Hermes Agent](https://github.com/NousResearch/hermes-agent)
— or any OpenAI-compatible client — through the **Cline CLI's OAuth login**.
No API key is created, copied, or stored anywhere.

A tiny localhost bridge reuses the OAuth session that `cline auth cline` already
created on your machine, keeps access tokens fresh, and exposes a standard
OpenAI-compatible endpoint at `http://127.0.0.1:8317/v1` — **with built-in
round-robin across multiple ClinePass accounts.**

## Features

- **OAuth, not API keys** — reuses your existing Cline CLI sign-in (WorkOS AuthKit)
- **Multi-account round-robin** — use N ClinePass subscriptions; requests alternate
  across accounts automatically for 2-5x effective throughput
- **Per-account failover** — 401/429 on one account? Marked for cooldown, next
  request goes to the next healthy account transparently
- **Automatic token refresh** — access tokens live only 1 hour; the bridge refreshes
  them via WorkOS before every request and writes rotated tokens back so the Cline
  CLI stays in sync
- **Failure resilience** — 401 → force refresh + retry; 429/5xx → backoff retry;
  revoked tokens detected and bypassed for 1h; clear re-login instructions
- **Watchdog script** — `Start-Bridge.ps1` heals the bridge and can auto-start it
  at logon; Hermes can fail over to a backup model if the bridge is down
- **Zero dependencies** — Python 3.8+ standard library only. No `pip install`
- **Streaming + tool calling** — full SSE passthrough, works with Hermes tools

> [!CAUTION]
> Using a subscription's OAuth session outside the vendor's own client may be
> against the provider's Terms of Service. This project only *reads* the tokens
> your own Cline CLI created. Review Cline's terms and use at your own discretion.

## Requirements

| Requirement | Check |
|---|---|
| Windows (Linux/macOS work via env override) | — |
| Python 3.8+ | `python --version` |
| Cline CLI with a **ClinePass** subscription | `npm install -g cline` |
| Signed in once | `cline auth cline` (or VS Code Cline extension) |

> **Note:** If the Cline CLI binary crashes on older CPUs (missing AVX2/BMI2), use
> the [VS Code Cline extension](https://marketplace.visualstudio.com/items?itemName=saoudrizwan.claude-dev)
> instead — it runs as JavaScript. Sign in with ClinePass there, then the bridge
> reads the tokens from the same `providers.json` file.

## Setup

**1. Sign in with the Cline CLI** (browser opens; pick your ClinePass account):

```powershell
cline auth cline
```

This stores OAuth tokens in `~/.cline/data/settings/providers.json`. You never
touch them — the bridge does.

**2. Clone and start the bridge:**

```powershell
git clone https://github.com/gashotoxic/cline-pass-hermes-bridge.git
cd cline-pass-hermes-bridge
.\Install.ps1            # checks prerequisites, starts the bridge
# optional, auto-start at every logon:
.\Start-Bridge.ps1 -Install
```

You should see:

```
Bridge started and healthy.
```

Verify: `curl http://127.0.0.1:8317/health` → `{"ok": true, ...}`

**3. Add the provider to Hermes** — edit
`%LOCALAPPDATA%\hermes\config.yaml` (Hermes home: `~/.hermes/config.yaml` on
Linux/macOS) under `custom_providers`. A ready-to-paste version (including
default-model and failover blocks) lives in
[`hermes-config-example.yaml`](hermes-config-example.yaml):

```yaml
custom_providers:
  - name: cline-pass
    base_url: http://127.0.0.1:8317/v1
    api_key: bridge-managed-oauth   # dummy value; the bridge injects real auth
    model: cline-pass/kimi-k3
    api_mode: chat_completions
```

**4. Use it:**

```powershell
hermes -z "Refactor this function" --provider custom:cline-pass -m cline-pass/kimi-k3
```

Or run `hermes model` to make ClinePass your default model.

**5. Verify the whole chain** (checks Python, Cline CLI, OAuth session,
Hermes config, bridge health, and a live completion):

```powershell
.\Test-Setup.ps1
# [PASS] Python 3.8+ installed
# [PASS] Cline CLI installed
# [PASS] Cline OAuth session (cline auth cline)
# [PASS] Hermes config.yaml valid + cline-pass provider
# [PASS] Bridge healthy on 127.0.0.1:8317
# [PASS] Live ClinePass completion via bridge
# ALL CHECKS PASSED - harness is ready.
```

Each failed check prints its own fix. Re-run until everything is green.

### Multi-Account Setup (N subscriptions)

To use multiple ClinePass accounts for round-robin rotation:

1. Sign in with each account (via VS Code Cline extension or `cline auth cline`):
   - Each sign-in creates an OAuth session in `~/.cline/data/secrets.json`
   - No Cline CLI needed — the VS Code extension works identically

2. Add each account to `~/.cline/data/settings/providers.json` with a unique key:
   - `cline-pass` — first account
   - `cline-pass-2` — second account
   - `cline-pass-3` — third account
   - (any key starting with `cline-pass` is autodetected)

   Each entry must have the same structure:
   ```json
   {
     "settings": {
       "provider": "cline-pass",
       "auth": {
         "accessToken": "workos:eyJ...",
         "refreshToken": "bq4K...",
         "expiresAt": 1784610935764,
         "accountId": "usr-...",
         "metadata": { "userInfo": { "email": "..." } }
       },
       "model": "cline-pass/kimi-k3"
     },
     "tokenSource": "oauth"
   }
   ```

3. Restart the bridge — it auto-discovers all accounts:
   ```
   discovered account: cline-pass (alice@gmail.com)
   discovered account: cline-pass-2 (bob@gmail.com)
   ClinePass multi-account bridge listening on http://127.0.0.1:8317/v1
   ```

4. Verify both accounts are healthy:
   ```bash
   curl http://127.0.0.1:8317/health
   ```

   Returns:
   ```json
   {
     "ok": true,
     "accounts": [
       {"key": "cline-pass", "email": "alice@gmail.com", "ok": true},
       {"key": "cline-pass-2", "email": "bob@gmail.com", "ok": true}
     ]
   }
   ```

5. No changes needed in Hermes config — the bridge handles rotation transparently.

**How it works:** Request 1 → account 1, Request 2 → account 2, Request 3 → account 1...
If an account 401s or 429s, it's skipped for 60s and the next healthy account handles the request. Revoked tokens are skipped for 1 hour.

**To add more accounts later:** repeat steps 1-3. No bridge restart needed if file-change detection picks it up (or restart for immediate effect).

### Optional: automatic failover

If the bridge is down or ClinePass rate-limits, Hermes can fall back to another
provider automatically:

```yaml
fallback_model:
  provider: openrouter            # any provider Hermes supports
  model: anthropic/claude-sonnet-4
```

## How It Works

```
Hermes (custom provider)          cline_pass_bridge.py (v2)        api.cline.bot
        │   POST /v1/chat/completions      │                                ▲
        │ ───────────────────────────────► │  round-robin across N accounts  │
        │                                  │ ──── account[0] ──────────────►│
        │                                  │ ──── account[1] ──────────────►│
        │   SSE stream / JSON              │ ──── account[N] ──────────────►│
        │ ◄─────────────────────────────── │ ◄──────────────────────────────│
                                           │   per-account refresh via WorkOS
        ~/.cline/data/settings/            │ ─◄─── each account─────────────► WorkOS
        providers.json  ◄── read + write ──┘        /user_management/authenticate
```

1. `cline auth cline` (or VS Code Cline extension) performs the browser OAuth flow (WorkOS).
   Tokens for the primary account go into `~/.cline/data/settings/providers.json`
   under key `cline-pass`.
2. **For multiple accounts**, each additional sign-in creates an OAuth session. Merge
   the tokens into the same `providers.json` under keys `cline-pass-2`, `cline-pass-3`, etc.
   The bridge auto-discovers **any** key starting with `cline-pass`.
3. WorkOS access tokens expire after **1 hour**. Each account's `AccountStore` checks
   expiry before every request and refreshes independently via WorkOS with its own
   `refresh_token` grant. Refreshed tokens are written back to `providers.json`
   (with a timestamped backup).
4. Requests are round-robinned across all healthy accounts. If one account 401s or
   429s, it goes into a cooldown period (60s transient, 1h for revoked tokens) and
   the next account handles the request.
5. Requests are forwarded to `https://api.cline.bot/api/v1/chat/completions`
   unchanged — streaming, tool calls, and reasoning all pass through.
6. `GET /v1/models` is served locally (upstream has no models endpoint) from the
   model IDs found in your Cline config, e.g. `cline-pass/kimi-k3`,
   `cline-pass/glm-5.2`.

> **Technical note:** `api.cline.bot` expects the bearer token with the Cline
> CLI's `workos:` scheme prefix (`Authorization: Bearer workos:eyJ...`). A raw
> JWT is rejected with 401. The bridge normalizes this automatically.

## Failure handling

| Situation | Behavior |
|---|---|
| Access token expired | Refreshed transparently per-account before the request |
| Upstream `401` | One forced refresh + retry; if still failing, skip to next account |
| Upstream `429/5xx` | Skip to next account immediately; all accounts exhausted → pass-through error |
| Refresh token revoked | Detected (`invalid_grant`), account cooling down for 1h; other accounts take over |
| One account always fails | Automatically routes to healthy accounts; dead account bypassed for 1h |
| Bridge not running | Hermes `fallback_model` (if configured) takes over |
| Reboot | `.\\Start-Bridge.ps1`, or install once with `.\\Start-Bridge.ps1 -Install` |

## Configuration

Environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_PORT` | `8317` | Listen port |
| `BRIDGE_HOST` | `127.0.0.1` | Listen host |
| `CLINE_PROVIDERS_JSON` | `~/.cline/data/settings/providers.json` | Cline token store location |
| `CLINE_API_BASE` | `https://api.cline.bot/api/v1` | Upstream API |
| `REFRESH_MARGIN` | `180` | Seconds before expiry to refresh |
| `BRIDGE_LOG` | `./bridge.log` | Log file path |

## Use with other clients

Anything that speaks OpenAI Chat Completions works:

```bash
curl http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer anything" \
  -H "Content-Type: application/json" \
  -d '{"model":"cline-pass/kimi-k3","messages":[{"role":"user","content":"hi"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8317/v1", api_key="anything")
print(client.chat.completions.create(
    model="cline-pass/kimi-k3",
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

## Troubleshooting

- **`No Cline OAuth tokens found`** → run `cline auth cline` first.
- **`token refresh failed ... 401`** → your session was revoked (password change,
  sign-out elsewhere). Run `cline auth cline` again; no bridge restart needed.
- **Port already in use** → another bridge is running (`Start-Bridge.ps1` is a
  no-op when healthy) or set `BRIDGE_PORT` and update Hermes' `base_url`.
- **Empty reply with reasoning models** → raise `max_tokens`; reasoning tokens
  count against it.
- **Hermes says `Unknown provider 'custom:cline-pass'` and/or warns that
  config.yaml can't be parsed** → the file's encoding got corrupted (a classic
  when it is edited with tools that rewrite it as Windows-1252: multibyte emoji
  in the personalities section turn into control characters and Hermes silently
  ignores ALL config). Restore the newest `config.yaml.bak.*` next to it and
  re-apply your block with a UTF-8-safe editor (VS Code). Never edit it with
  Notepad or PowerShell 5.1 `Set-Content`/`Get-Content` round-trips.
- **Health endpoint** now returns all accounts:
  ```json
  {
    "ok": true,
    "all_healthy": true,
    "accounts": [
      {"key": "cline-pass", "email": "alice@gmail.com", "ok": true, "cooling_down": false},
      {"key": "cline-pass-2", "email": "bob@gmail.com", "ok": true, "cooling_down": false}
    ]
  }
  ```
- **Logs** → `bridge.log` next to the script; `--check` and `--refresh-now` are
  handy CLI probes:
  `python cline_pass_bridge.py --check`

## Security

- Tokens only travel to `api.workos.com` (refresh) and `api.cline.bot`
  (inference). The bridge binds to localhost only.
- Nothing is persisted by the bridge except the token write-back into the Cline
  CLI's own `providers.json` (a `.bak` is kept per refresh).

## Credits

Inspired by:

- [opencode-google-antigravity-auth](https://github.com/shekohex/opencode-google-antigravity-auth) — the Antigravity OAuth plugin for opencode
- [opencode-gemini-auth](https://github.com/jenslys/opencode-gemini-auth) — original Gemini OAuth implementation
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — API translation reference

## License

MIT — see [LICENSE](LICENSE).

