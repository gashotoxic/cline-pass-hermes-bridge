# ClinePass OAuth Bridge for Hermes Agent

Use your **ClinePass subscription** with [Hermes Agent](https://github.com/NousResearch/hermes-agent)
— or any OpenAI-compatible client — through the **Cline CLI's OAuth login**.
No API key is created, copied, or stored anywhere.

A tiny localhost bridge reuses the OAuth session that `cline auth cline` already
created on your machine, keeps the access token fresh, and exposes a standard
OpenAI-compatible endpoint at `http://127.0.0.1:8317/v1`.

## Features

- **OAuth, not API keys** — reuses your existing Cline CLI sign-in (WorkOS AuthKit)
- **Automatic token refresh** — access tokens live only 1 hour; the bridge refreshes
  them via WorkOS before every request and writes rotated tokens back so the Cline
  CLI keeps working too
- **Failure resilience** — 401 → force refresh + retry; 429/5xx → backoff retry;
  clear re-login instructions if the refresh token is ever revoked
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
| Signed in once | `cline auth cline` |

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
Linux/macOS) under `custom_providers`:

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
Hermes (custom provider)          cline_pass_bridge.py                api.cline.bot
        │   POST /v1/chat/completions      │                                ▲
        │ ───────────────────────────────► │  Bearer workos:<access_token>  │
        │                                  │ ──────────────────────────────►│
        │   SSE stream / JSON              │                                │
        │ ◄─────────────────────────────── │ ◄──────────────────────────────│
                                           │   refresh_token grant (hourly)
        ~/.cline/data/settings/            │ ──────────────────────────────► WorkOS
        providers.json  ◄── read + write ──┘        /user_management/authenticate
```

1. `cline auth cline` performs the browser OAuth flow (WorkOS). Tokens are stored
   by the Cline CLI — this project never asks you for credentials.
2. WorkOS access tokens expire after **1 hour**. The bridge checks expiry before
   every request and refreshes with the `refresh_token` grant when needed.
   Refresh tokens rotate, so new tokens are written back to `providers.json`
   (with a timestamped backup) to keep the Cline CLI signed in as well.
3. Requests are forwarded to `https://api.cline.bot/api/v1/chat/completions`
   unchanged — streaming, tool calls, and reasoning all pass through.
4. `GET /v1/models` is served locally (upstream has no models endpoint) from the
   model IDs found in your Cline config, e.g. `cline-pass/kimi-k3`,
   `cline-pass/glm-5.2`.

> **Technical note:** `api.cline.bot` expects the bearer token with the Cline
> CLI's `workos:` scheme prefix (`Authorization: Bearer workos:eyJ...`). A raw
> JWT is rejected with 401. The bridge normalizes this automatically.

## Failure handling

| Situation | Behavior |
|---|---|
| Access token expired | Refreshed transparently before the request |
| Upstream `401` | One forced refresh + retry |
| Upstream `429/5xx` | Up to 3 retries, backoff 1s → 2s → 4s |
| Bridge not running | Hermes `fallback_model` (if configured) takes over |
| Refresh token revoked | JSON error with instructions: run `cline auth cline`, then `.\Start-Bridge.ps1` — the bridge picks up new tokens automatically, no restart needed |
| Reboot | `.\Start-Bridge.ps1`, or install once with `.\Start-Bridge.ps1 -Install` |

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

