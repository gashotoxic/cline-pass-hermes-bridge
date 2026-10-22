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
| `ZEN_ENABLED` | `1` | `0` disables the zen section entirely |
| `ZEN_KEYS` | _(empty)_ | Comma-separated OpenCode Console API keys: enables round-robin per-account quota tier (anonymous attestation remains as fallback) |
| `ZEN_BASE_URL` | `https://opencode.ai/zen/v1` | Zen upstream |
| `ZEN_CLIENT_VERSION` | `2.0.30` | Version reported in the attestation User-Agent |
| `ZEN_CATALOG_TTL` | `900` | Seconds between live zen catalog refreshes |

## opencode Zen free tier (Section B, added 2026-02-16)

The bridge also fronts the [opencode Zen](https://opencode.ai/zen) gateway's
free model catalog under the `zen/` prefix, e.g. `zen/glm-5.3-flash-free`.
Same endpoint, same OpenAI shape:

```bash
curl http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer anything" \
  -H "Content-Type: application/json" \
  -d '{"model":"zen/glm-5.3-flash-free","messages":[{"role":"user","content":"hi"}]}'
```

How it works (reverse-engineered from opencode v2.0.30's own traffic): the
free tier is **anonymous client attestation**, not an API key. The bridge
replays the exact recipe upstream — opencode User-Agent, `x-opencode-client:
cli`, a session id in opencode's `ses_<9hex>ffe<14alnum>` format,
`stream: true`, and a tool named `read` (injected only when the client
didn't define one). Any foreign `Authorization` header from the client is
stripped and replaced. Non-streaming clients get a synthesized
`chat.completion` assembled from the upstream SSE stream.

- **Model catalog** is fetched live from `/zen/v1/models` every 15 min
  (cached), filtered to free models. New free models appear in `/v1/models`
  automatically on the next fetch; removals are logged. Churn history lives
  in `zen-models-state.json` (first_seen / last_seen / gone_since per model).
  If the live fetch fails, a static fallback list (verified 2026-10-21) is
  used. Unknown `zen/<id>` models are passed through anyway — upstream
  decides (same policy as removed Cline models).
- **`ZEN_EXCLUDE`** hides models that upstream still lists but that cannot
  work through the attestation path — currently `jev-1.13-free`, which
  rejects tool definitions (`400 ModelProtocolUnsupported`) while the free
  tier *requires* a tool named `read`. Hidden from `/v1/models`, still
  accepted on POST.
- **FreeTierError / 429s** happen on the anonymous shared pool (transient
  admission pressure). The bridge retries with backoff; for dedicated quota,
  create free OpenCode Console account(s) and set `ZEN_KEYS=key1,key2,...` —
  requests then round-robin per account with cooldown failover, and the
  anonymous recipe remains as the last-resort tier.
- **Probe**: `python cline_pass_bridge.py --zen-check` runs a live
  attested completion and prints PASS/FAIL.
- Some free models are geo-blocked upstream (e.g. the retired
  `cat-jam-free` for NG, RU, CN, BY, IR, KP, SY, VE, CU); they stay listed
  and simply error there.
- **IP-reputation flag** (distinct from rate limiting): upstream can return
  `403 FreeTierError: "...detected abusive traffic from this IP address"`.
  This is scoped to the **public egress IP** of the connection — it is not
  geographic (verified: two boxes in Nairobi, one flagged, one not) and not
  per-account. It is also **free-tier only**: paid/console-key requests from
  the same flagged IP return `401 AuthError: Missing API key`, i.e. they
  clear the network gate and fail at credential validation instead. The flag
  is usually temporary — re-probe later, or force a new dynamic IP.
  Do not confuse it with the second 403 message, `"free tier can only be
  used from within OpenCode"`, which means the *attestation recipe* broke
  (missing `read` tool / wrong headers), not the IP.
- Zen failures never touch Cline account state, cooldowns or status codes.

## Unblocking a flagged IP (free-tier reputation)

Symptom: `403 FreeTierError: "OpenCode's free models aren't available from
your current network because we've detected abusive traffic from this IP
address."` — the block is on the connection's **public egress IP**.

What does **not** work: waiting for a per-minute rate-limit window, changing
LAN IP, changing machine, changing account, or retrying harder (the bridge
already retries with backoff). What **does** work, cheapest first:

| # | Action | Notes |
|---|--------|-------|
| 1 | Re-probe later | These flags are frequently temporary — verify before doing anything expensive. `--zen-check`, or any `zen/*` request |
| 2 | Force a new dynamic IP | Router reboot / PPPoE reconnect / MAC clone; mobile-data toggle on cellular. Consumer ISP IPs rotate, and the flag follows the IP, not you |
| 3 | `ZEN_RELAY_BASE` | Route zen through a peer bridge on a clean connection. Verified working: this PC relaying through the Unwired box over Tailscale returned `RELAY-OK` and mirrored all 9 models |
| 4 | `ZEN_PROXY` | Point zen at an HTTP proxy with a different egress. Only zen traffic is affected; Cline keeps going direct |
| 5 | `ZEN_KEYS` | Console keys are a keyed tier: per-account quota instead of the anonymous pool's IP heuristics, and they unlock the paid catalog |
| 6 | Ask upstream | Long-lived flags on residential/CGNAT ranges are worth reporting to OpenCode support together with the egress IP |

Because ISP consumer IPs are often CGNAT-shared, every machine behind one
router inherits the verdict (verified: the Windows box and one Linux box share
`41.90.172.133` and are flagged or clear together, while a box on a different
ISP was unaffected at the same moment).

### Relay / proxy configuration

```bash
# Relay zen through another bridge instance that has a clean egress IP.
# The peer must listen on a reachable interface (BRIDGE_HOST=0.0.0.0).
ZEN_RELAY_BASE=http://100.105.117.30:8317/v1

# ...or send only zen traffic through an HTTP proxy.
ZEN_PROXY=http://user:pass@proxy-host:8080
```

Both are visible in `/health` under `zen.proxy` / `zen.relay`, and the log
shows `zen/<id> served via relay <base>` per request.

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


## Kali Linux Variant (v2 script)

The Kali machine runs a variant, `cline_pass_bridge_v2.py`, in this same repo:

- Multi-account OAuth round-robin (WorkOS tokens auto-refreshed, one-time-use — never test refresh tokens manually)
- Product-surface headers (`X-CLIENT-TYPE: cline-sdk`, `User-Agent: Cline/3.0.39`, ...) so `cline-free/*` models pass Cline's product-surface gate
- `curated` catalog tuple (26 models as of 2026-09-18, matching the Windows fleet — see the "ClinePass Bridge - Complete Setup & Configuration Guide" on Google Drive)
- Runs as a systemd user service: `systemctl --user restart clinepass-bridge` (needs `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`)
- Device-flow account auth without the Cline CLI: `node cline-auth.js` (5-minute window)
- See `LINUX-SETUP.md` and `MAINTENANCE-GUIDE.md` for the full Kali runbook

Note: Windows uses `cline_pass_bridge.py` (v3) + the PowerShell/VBS installers; Kali uses `cline_pass_bridge_v2.py` + systemd. Do not cross-apply line numbers or tuple names between them.

## Credits

Inspired by:

- [opencode-google-antigravity-auth](https://github.com/shekohex/opencode-google-antigravity-auth) — the Antigravity OAuth plugin for opencode
- [opencode-gemini-auth](https://github.com/jenslys/opencode-gemini-auth) — original Gemini OAuth implementation
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — API translation reference

## License

MIT — see [LICENSE](LICENSE).

