# ClinePass Bridge — Maintenance & Repair Guide
## For Hermes Agents (MUGO, VICTOR, GIMA, SCOUT, AEGIS, JOBSCOUT)

---

## How This Thing Works (READ FIRST — Don't Skip)

The ClinePass bridge is a local HTTP proxy. It lets Hermes use Cline's premium models
without needing an API key — instead it uses OAuth tokens from Cline's WorkOS auth.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           HOW IT WORKS                                 │
│                                                                         │
│  You send a request to: http://127.0.0.1:8317/v1/chat/completions       │
│         │                                                               │
│         ▼                                                               │
│  ┌──────────────┐   Reads tokens from ~/.cline/data/settings/           │
│  │   Bridge v2   │   providers.json (any key starting with cline-pass*)  │
│  │  (Python)     │   Round-robins across ALL discovered accounts         │
│  │               │   Auto-refreshes expiring tokens (every ~1h)          │
│  │  Round-Robin  │   On 401/429: cooldowns that account, tries next      │
│  │  MultiAccount │   On dead refresh token: skips, logs "REVOKED"        │
│  └──────┬───────┘                                                       │
│         │  Each request goes to https://api.cline.bot/api/v1            │
│         │  Uses the CURRENT accessToken from providers.json              │
│         ▼                                                               │
│  ┌──────────────┐                                                       │
│  │ api.cline.bot │  Routes to Moonshot AI (Kimi K3), Novita, etc.        │
│  └──────────────┘                                                       │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key design decisions:**
- Round-robin by DEFAULT (not primary/failover). Two accounts = double throughput.
- Bridge reads providers.json FRESH on every request — you can replace the file without restarting.
- Bridge writes back refreshed tokens to providers.json automatically.
- If ONE account dies → cooldowns 60s → other takes all traffic. If BOTH die → 401 errors.
- The ONLY way to fix a dead refresh token is a FRESH export from a working machine.

---

## Current State (as of 2026-07-23)

### File Locations

| Item | Path | Purpose |
|------|------|---------|
| **Bridge script** | `~/.hermes/cline-pass-bridge/cline_pass_bridge_v2.py` | The daemon — DO NOT EDIT |
| **Active providers** | `~/.cline/data/settings/providers.json` | Token source of truth |
| **Hermes config** | `~/.hermes/config.yaml` | custom:cline-pass provider definition |
| **Bridge log** | `~/.hermes/cline-pass-bridge/bridge-fresh-start.log` | Runtime log |
| **Canonical backup** | `~/.hermes/cline-pass-bridge/providers-canonical-*.json` | Last known working state |
| **Shared docs** | `~/.hermes/SHARED/clinepass-bridge-reference.md` | This file + architecture ref |
| **Google Drive folder** | `ClinePass-Auth-Backup/` | Cross-machine token exchange |

### Accounts (round-robin)

| Key | Email | Status |
|-----|-------|--------|
| `cline-pass` | gashotechnologies@gmail.com | ✅ LIVE (refreshed Jul 23 01:37) |
| `cline-pass-2` | nibsstudents101@gmail.com | ✅ LIVE (fresh export Jul 23 22:52) |

### Hermes Config

```yaml
custom_providers:
  - name: cline-pass
    base_url: http://127.0.0.1:8317/v1
    api_key: bridge-managed-oauth
    model: cline-pass/kimi-k3

model:
  default: cline-pass/kimi-k3
  provider: custom:cline-pass

fallback_model:
  provider: crofai
  model: glm-5.2
  base_url: https://crof.ai/v1
```

---



## systemd Auto-Start (ACTIVE)

The bridge now runs under systemd user service. This is the preferred way to manage it.

```bash
# Status
systemctl --user status clinepass-bridge

# Start
systemctl --user start clinepass-bridge

# Stop
systemctl --user stop clinepass-bridge

# Restart (use this instead of pkill)
systemctl --user restart clinepass-bridge

# Enable on boot (already done)
systemctl --user enable clinepass-bridge

# Keep running after logout (already done)
loginctl enable-linger $USER
```

**Service file:** `~/.config/systemd/user/clinepass-bridge.service`

**IMPORTANT:** If you need to restart the bridge, use `systemctl --user restart clinepass-bridge`
instead of `pkill` + manual start. The systemd service handles process management.

**If systemd fails**, you can still run manually:
```bash
pkill -9 -f cline_pass_bridge_v2.py
python3 ~/.hermes/cline-pass-bridge/cline_pass_bridge_v2.py   >> ~/.hermes/cline-pass-bridge/bridge-fresh-start.log 2>&1 &
```

**All profiles have access** to the bridge via `custom:cline-pass` provider:
- default (MUGO) ✅
- gima ✅
- victor ✅
- jobscout ✅
- scout ✅
- aegis ✅

Each profile's config.yaml has the `custom_providers` entry pointing to `http://127.0.0.1:8317/v1`.

## The Golden Rule — DO NOT SKIP

**NEVER manually test refresh tokens with curl/HTTP requests. NEVER.**

WorkOS refresh tokens are **one-time-use**. Once consumed, the OLD token is dead
and a NEW one is issued in the response. If you consume it but don't save the new
one, that account is permanently dead until someone re-exports fresh tokens.

**What to do instead:**
1. Put the fresh providers.json in place
2. Start the bridge
3. Let the bridge do the first refresh (it saves the new token automatically)
4. Check `/health` — if it says ok=true, the account is fine

---

## Quick Status Check

Run this to see if everything is healthy RIGHT NOW:

```bash
# Bridge health
curl http://127.0.0.1:8317/health

# Expected output:
# {"ok": true, "all_healthy": true, "accounts": [...]}

# Quick API test
curl -s -X POST http://127.0.0.1:8317/v1/chat/completions   -H "Content-Type: application/json"   -d '{"model":"cline-pass/kimi-k3","messages":[{"role":"user","content":"say ALIVE"}],"max_tokens":200,"stream":false}'
# Expected: ALIVE in the content field
```

---

## Troubleshooting Guide

### Category A: Bridge Won't Start / Not Running

**Symptom:** `curl http://127.0.0.1:8317/health` → connection refused

**Check:**
```bash
# Is the process running?
pgrep -f cline_pass_bridge_v2

# Is the port in use?
ss -tlnp | grep 8317

# What does the log say?
tail -30 ~/.hermes/cline-pass-bridge/bridge-fresh-start.log
```

**Fix:**
```bash
# Kill any zombie process
pkill -9 -f cline_pass_bridge_v2.py

# Verify port is free
ss -tlnp | grep 8317

# Start fresh
python3 ~/.hermes/cline-pass-bridge/cline_pass_bridge_v2.py   >> ~/.hermes/cline-pass-bridge/bridge-fresh-start.log 2>&1 &

# Wait 3 seconds, then check health
sleep 3 && curl http://127.0.0.1:8317/health
```

**Common causes:**
- Port 8317 in use by a dead process (SIGKILL and restart)
- Python not found (check `python3 --version` — needs 3.8+)
- Log directory doesn't exist (`mkdir -p ~/.hermes/cline-pass-bridge`)
- Script file missing or corrupted (redownload from Drive)

---

### Category B: 401 Unauthorized (Access Token Expired)

**Symptom:** Bridge health shows ok=true but requests fail with 401

**Check:**
```bash
curl http://127.0.0.1:8317/health
# Look for "expires_in_seconds" — if negative, token is expired
# Look for "ok": true/false per account
```

**Fix — bridge auto-refreshes:**
1. Wait 5 seconds and retry the request — the bridge refreshes expired tokens automatically
2. If 401 persists, check the log for "REVOKED" or "Session has already ended"

**If auto-refresh fails:**
The refresh token is dead. The bridge will log:
```
WorkOS refresh failed for cline-pass: HTTP 400 {"error":"invalid_grant","error_description":"Session has already ended."} - TOKEN REVOKED
```

**Fix: get fresh tokens from the working machine (see Category E below)**

---

### Category C: One Account Dead, Other Works

**Symptom:** Health shows one account ok=true, another ok=false

**Check:**
```bash
curl http://127.0.0.1:8317/health
# Look for the specific account and its "cooling_down" status
```

**What happens:** The bridge cooldowns the dead account for 60s, then retries.
If it fails again, it stays in cooldown. The other account handles all traffic.

**Fix options:**
1. Wait — bridge retries every 60s, might recover
2. Check if the dead account's refresh token is truly dead (log shows "invalid_grant")
3. Get fresh export for that account from the working machine

---

### Category D: Both Accounts Dead

**Symptom:** All requests fail, health shows both accounts as dead/REVOKED

**This means:** Both WorkOS sessions were terminated server-side.

**Root causes:**
- Working machine logged out of Cline (kills all sessions)
- Working machine re-authed (old sessions die)
- WorkOS itself invalidated the sessions (rare, security event)

**Fix: Get fresh tokens from the working machine.**

**Procedure:**

1. On the WORKING machine (Windows), ensure BOTH accounts are signed in:
   - `cline auth cline` for each account
   - Or sign in via VS Code Cline extension

2. Export providers.json:
   ```bash
   # Windows PowerShell
   cp "$env:USERPROFILE\.cline\data\settings\providers.json"      "$env:USERPROFILE\Desktop\providers-export-$(Get-Date -Format 'yyyyMMdd_HHmmss').json"
   ```

3. Upload to Drive folder: **ClinePass-Auth-Backup/**
   Name: `providers-export-YYYYMMDD_HHMMSS.json`

4. On Kali, download the fresh file:
   ```python
   # In Hermes agent (execute_code):
   from google.oauth2.credentials import Credentials
   from googleapiclient.discovery import build
   from googleapiclient.http import MediaIoBaseDownload
   from pathlib import Path
   import io
   
   creds = Credentials.from_authorized_user_file(str(Path.home() / '.hermes' / 'google_token.json'))
   service = build('drive', 'v3', credentials=creds, cache_discovery=False)
   
   # List and find the fresh file
   folder_id = '1T2Kln3Qw7yZ7IkGajPc4uasuiZUeqfUR'
   r = service.files().list(
       q=f"'{folder_id}' in parents and modifiedTime > '...'",
       fields='files(id, name)',
       supportsAllDrives=True, includeItemsFromAllDrives=True
   ).execute()
   # Download, then replace providers.json
   ```

5. Rename keys in the downloaded file to match bridge's PROVIDER_PREFIX (`cline-pass`):
   - Working machine might use `cline` / `cline-2` instead of `cline-pass` / `cline-pass-2`
   - The bridge ONLY discovers keys starting with `cline-pass`
   - If keys are wrong, edit the JSON file to rename them BEFORE replacing providers.json

6. Replace providers.json and restart bridge:
   ```bash
   cp ~/.cline/data/settings/providers.json       ~/.cline/data/settings/providers.json.bak.$(date +%Y%m%d_%H%M%S)
   cp <downloaded_file> ~/.cline/data/settings/providers.json
   pkill -9 -f cline_pass_bridge_v2.py
   python3 ~/.hermes/cline-pass-bridge/cline_pass_bridge_v2.py      >> ~/.hermes/cline-pass-bridge/bridge-fresh-start.log 2>&1 &
   ```

7. Verify:
   ```bash
   sleep 5 && curl http://127.0.0.1:8317/health
   ```

---

### Category E: Providers.json Structure / Key Naming

**The bridge only discovers keys starting with `cline-pass`.**

If you download a file and it has keys like `cline`, `cline-2`, etc. — the bridge
will NOT find them. You must rename them.

**Valid provider keys:**
```
cline-pass          ← discovered ✅
cline-pass-2        ← discovered ✅
cline-pass-3        ← discovered ✅
cline-pass-anything ← discovered ✅
cline               ← NOT discovered ❌
cline-2             ← NOT discovered ❌
anything-else       ← NOT discovered ❌
```

**providers.json structure:**
```json
{
  "version": 1,
  "lastUsedProvider": "cline-pass",
  "providers": {
    "cline-pass": {
      "id": "cline-pass",
      "settings": {
        "provider": "cline-pass",
        "auth": {
          "accessToken": "workos:eyJ...",
          "refreshToken": "abc123...",
          "expiresAt": 1784716304254,
          "accountId": "usr-01KXX...",
          "metadata": {
            "sessionStartedAtMs": 1784499167228,
            "tokenType": "Bearer",
            "userInfo": {
              "email": "gashotechnologies@gmail.com",
              "displayName": "Gasho Tecch"
            }
          }
        },
        "model": "cline-pass/kimi-k3",
        "reasoning": { "enabled": true, "budgetTokens": 6000 }
      }
    },
    "cline-pass-2": {
      "id": "cline-pass-2",
      "settings": {
        "provider": "cline-pass-2",
        "auth": { ... same structure ... },
        "model": "cline-pass/kimi-k3",
        "reasoning": { "enabled": true, "budgetTokens": 6000 }
      }
    }
  }
}
```

---

### Category F: Empty Response / No Content

**Symptom:** Bridge returns 200 but `content` is empty string

**Check:** The model uses reasoning (thinking) tokens. If `max_tokens` is too low,
the model spends all tokens thinking and has nothing left for the response.

**Fix:** Increase `max_tokens` to at least 200, or set `reasoning.budget_tokens`:
```json
{
  "model": "cline-pass/kimi-k3",
  "messages": [...],
  "max_tokens": 200,
  "reasoning": { "budget_tokens": 100 }
}
```

---

### Category G: Model Not Found / Routing Issues

**Symptom:** Error about model not found or wrong model

**Check what model is configured:**
```bash
# In providers.json
cat ~/.cline/data/settings/providers.json | grep -A2 '"model"'

# In config.yaml
grep -A5 'model:' ~/.hermes/config.yaml
```

**Valid models through the bridge:**
- `cline-pass/kimi-k3` → routes to `moonshotai/kimi-k3` via Cline gateway
- The bridge passes the model name as-is to the upstream

**If model name is wrong:** Fix the `model` field in providers.json
and the `model.default` in config.yaml.

---

### Category H: Google Drive Access Issues

**Symptom:** Can't download files from Drive, 401/403 from Google API

**Check:**
```bash
# Test token
curl -s "https://oauth2.googleapis.com/tokeninfo?access_token=$(jq -r .token ~/.hermes/google_token.json)"
```

**If token expired:**
1. Generate a new auth URL:
   ```
   https://accounts.google.com/o/oauth2/v2/auth?client_id=242229947582-hhd08q54182hl0lqfcf32adun0to8cf7.apps.googleusercontent.com&redirect_uri=urn:ietf:wg:oauth:2.0:oob&response_type=code&scope=https://www.googleapis.com/auth/drive&access_type=offline&prompt=consent
   ```
2. Open in browser, authorize, copy the code
3. Exchange code for token (see google-auth skill)

---

## Step-by-Step Repair Procedures

### Full Reset (nuclear option)

```bash
# 1. Kill bridge
pkill -9 -f cline_pass_bridge_v2.py

# 2. Backup current state
cp ~/.cline/data/settings/providers.json    ~/.cline/data/settings/providers.json.bak.$(date +%Y%m%d_%H%M%S)

# 3. Restore from canonical backup
ls -la ~/.hermes/cline-pass-bridge/providers-canonical-*.json
# Pick the most recent one:
cp ~/.hermes/cline-pass-bridge/providers-canonical-YYYYMMDD_HHMMSS.json    ~/.cline/data/settings/providers.json

# 4. Restart bridge
python3 ~/.hermes/cline-pass-bridge/cline_pass_bridge_v2.py   >> ~/.hermes/cline-pass-bridge/bridge-fresh-start.log 2>&1 &

# 5. Verify
sleep 5 && curl http://127.0.0.1:8317/health
```

### Single-Account Recovery

```bash
# 1. Check which account is dead
curl http://127.0.0.1:8317/health

# 2. If only one account is dead, check its refresh token in providers.json
cat ~/.cline/data/settings/providers.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
for k,v in d['providers'].items():
    a=v['settings']['auth']
    print(f'{k}: {a["metadata"]["userInfo"]["email"]}')
"

# 3. Get fresh export for that account from the working machine
# 4. Edit providers.json to update ONLY that account's auth section
# 5. No bridge restart needed — it re-reads the file on each request
# 6. Verify: curl http://127.0.0.1:8317/health
```

### New Account Addition (adding a third account)

```bash
# 1. Ensure the new account is signed in on the working machine
# 2. Export providers.json from working machine
# 3. On Kali, download and edit providers.json:
#    - Add new entry with key "cline-pass-3"
#    - Fill in auth block from the export
#    - Copy "model" and "reasoning" from existing entries
# 4. No restart needed — bridge discovers on next request
# 5. Verify: curl http://127.0.0.1:8317/health
```

---

## File Reference

### Files That MUST NOT Be Modified

| File | Reason |
|------|--------|
| `cline_pass_bridge_v2.py` | Bridge daemon — editing breaks it. If corrupted, redownload from Drive. |
| `providers-canonical-*.json` | Recovery backups — don't edit, don't delete. |
| `google_token.json` | Google OAuth — don't edit, don't delete. |

### Files That Are Safe to Edit

| File | What you can change |
|------|-------------------|
| `~/.cline/data/settings/providers.json` | Add/remove/update account auth blocks. Bridge reads fresh each request. |
| `~/.hermes/config.yaml` | Model selection, fallback config. |
| `~/.hermes/cline-pass-bridge/bridge-fresh-start.log` | Log file — can be cleared if too large. |

### Backup Files

| File | Purpose |
|------|---------|
| `providers-canonical-*.json` | Last known working state (full providers.json) |
| `providers-before-fresh-*.json` | State before a providers.json swap |
| `providers-merged-*.json` | Merged state from multiple sources |
| `*.dead.json` | Archived files with consumed/dead tokens |

---

## Error Message Reference

| Error | Meaning | What To Do |
|-------|---------|-----------|
| `invalid_grant: Session has already ended` | WorkOS session terminated | Get fresh export from working machine |
| `invalid_grant: Refresh token already exchanged` | Token was already used once | Get fresh export from working machine |
| `Connection refused` on :8317 | Bridge not running | Start bridge: `python3 ~/.hermes/cline-pass-bridge/cline_pass_bridge_v2.py` |
| `HTTP 200` + empty content | max_tokens too low | Increase max_tokens to 200+ |
| `HTTP 401` from bridge | Access token expired | Wait 5s, retry — bridge auto-refreshes |
| `No accounts available` | No cline-pass* keys in providers.json | Check key names — must start with `cline-pass` |
| `REVOKED:` in log | Refresh token dead | Get fresh export from working machine |
| `cooling down for 60s` | Account failed, retrying later | Wait — if persists, check token health |
| `TimeoutError` on refresh | Network issue to WorkOS | Check internet, retry in 30s |

---

## WorkOS Token Lifecycle (IMPORTANT)

```
┌─────────────────────────────────────────────────────────┐
│                    TOKEN LIFECYCLE                       │
│                                                          │
│  1. User authenticates via OAuth (browser)              │
│     → WorkOS issues: accessToken + refreshToken          │
│     → accessToken expires in ~1 hour                     │
│     → refreshToken expires in ~30 days                   │
│                                                          │
│  2. Bridge detects accessToken expiring soon             │
│     → Sends refreshToken to WorkOS                       │
│     → Gets NEW accessToken + NEW refreshToken            │
│     → SAVES both back to providers.json                  │
│                                                          │
│  3. OLD refreshToken is now DEAD                         │
│     → Cannot be used again                               │
│     → If someone tries to use it: "already exchanged"    │
│                                                          │
│  4. This repeats every hour indefinitely                 │
│     → As long as the bridge is running, tokens stay live │
│     → If the bridge is off for >1h, next start refreshes │
│                                                          │
│  5. If the refreshToken is used from another machine:    │
│     → The old one dies                                   │
│     → Both machines CANNOT use the same session           │
│     → Each machine needs its OWN WorkOS session          │
│     → Sign in separately on each machine                 │
│                                                          │
│  ⚠️  NEVER manually test refresh tokens with curl!      │
│     → You consume the token, don't save the new one      │
│     → That account is permanently dead                   │
│     → Let the BRIDGE handle refreshes                    │
└─────────────────────────────────────────────────────────┘
```

---

## Google Drive Integration

Used to exchange tokens between machines (Kali ↔ Windows).

| Item | Value |
|------|-------|
| Folder | `ClinePass-Auth-Backup/` |
| Folder ID | `1T2Kln3Qw7yZ7IkGajPc4uasuiZUeqfUR` |
| Auth token | `~/.hermes/google_token.json` |
| Auth URL | See Category H above |
| Shared docs | `~/.hermes/SHARED/` |

**File naming convention:**
- `providers-export-YYYYMMDD_HHMMSS.json` — fresh export from working machine
- `providers-fresh-YYYYMMDD.json` — manually curated (keys renamed)
- `STATUS-FROM-*.md` — status updates between machines
- `REQUEST-FROM-*.md` — help requests between machines

---

## Answers from Working Machine (2026-07-22)

These are the answers from the Windows operator about the setup:

1. **Round-robin 50/50 vs primary+failover:** Bridge does round-robin by default.
   Both accounts get equal traffic. This is better — doubles throughput.

2. **Rate limits:** Per account (per WorkOS user). Two accounts = independent quotas.
   Round-robin doubles effective rate.

3. **Provider key names:** Windows uses `cline-pass` and `cline` (not `cline-pass-2`).
   The file they uploaded had keys renamed to `cline-pass` and `cline-pass-2`.
   Bridge's PROVIDER_PREFIX is `"cline-pass"` — only `cline-pass*` keys are discovered.

4. **secrets.json vs providers.json:** Use providers.json only. secrets.json is
   the VS Code extension's internal format — not used by the bridge.

5. **Re-auth if tokens die:** Tell the Windows machine, they re-auth and export.
   Takes ~2 minutes. Alternative: VS Code extension on Kali (slow on E8400).
   Worst case: API key from app.cline.bot/settings/api-keys.

6. **Auto-start:** Yes, use systemd. The guide has the service file.

7. **Naming:** The uploaded file used `cline-pass` (nibs) and `cline-pass-2` (gasho).
   Both start with `cline-pass` so both are discovered.

---

## Auto-Start Setup (systemd)

If you want the bridge to survive reboots:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/clinepass-bridge.service <<'EOF'
[Unit]
Description=ClinePass Hermes Bridge
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 %h/.hermes/cline-pass-bridge/cline_pass_bridge_v2.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now clinepass-bridge
loginctl enable-linger $USER

# Verify
systemctl --user status clinepass-bridge
```

NOTE: If you set this up, make sure to stop any manually-started bridge first:
`pkill -9 -f cline_pass_bridge_v2.py` (otherwise two instances fight for port 8317).

---

## CPU Compatibility Note

This machine (Kali) has an Intel E8400 (Core 2 Duo) — no SSE4.2 or AVX.
- The Cline CLI binary crashes with SIGILL (illegal instruction)
- Python works fine
- The bridge avoids needing the CLI binary entirely
- Token refresh uses direct HTTP calls to WorkOS (no crypto acceleration needed)

---

## Communication Protocol Between Machines

When something breaks:

1. **Upload a file** to `ClinePass-Auth-Backup/` on Drive:
   - Name: `REQUEST-FROM-<machine>-<date>.md`
   - Content: What's broken, what you need, questions

2. **Response expected** from the other machine:
   - Name: `RESPONSE-FROM-<machine>-<date>.md`
   - Content: Answers, files uploaded, instructions

3. **Status updates** after repair:
   - Name: `STATUS-FROM-<machine>-<date>.md`
   - Content: Current health, what's working, what's not

The Drive folder is the single communication channel — both machines check it.
