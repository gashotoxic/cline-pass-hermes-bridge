# ClinePass Bridge — Linux Setup (Account 2 as Main)

## What you're getting

- `providers-linux-account2.json` — fresh OAuth tokens for nibsstudents101@gmail.com
- `cline_pass_bridge.py` — the bridge script (multi-account v2, works on Linux as-is)
- `token_monitor.py` — health check script

Your account 2 tokens were exported from the Windows PC on 2026-07-22 with ~50 min
of access-token life left. The bridge auto-refreshes on startup using the refresh
token, so expiry on arrival is not a problem — as long as the refresh token is valid
(it is, as of export).

---

## Q: "My tokens expired and it needs new tokens" — what happened?

Two possibilities:

1. **Only the access token expired** (normal — they live 1 hour).
   The bridge refreshes these automatically via WorkOS. If the bridge wasn't running
   when you checked, the file just looks stale. Start the bridge and it self-heals.

2. **The refresh token died** ("invalid_grant / Session has already ended").
   This is the real failure. Fix: re-authenticate (see Step 2 below).

The fastest way to know which one you have:

```bash
python3 cline_pass_bridge.py --check
```

- "auth OK ... expires in Ns" → access token only, bridge handles it
- "REVOKED" / "invalid_grant" → refresh token dead, re-auth needed

---

## Setup (Linux)

### Step 1 — Files

```bash
mkdir -p ~/cline-pass-hermes-bridge
# copy cline_pass_bridge.py and token_monitor.py there
mkdir -p ~/.cline/data/settings
cp providers-linux-account2.json ~/.cline/data/settings/providers.json
```

IMPORTANT: on the Linux PC the provider key is `cline-pass` (not `cline-pass-2`).
The bridge discovers any key starting with `cline-pass` and treats it as an account.
With one account, it serves every request — that account IS your main.

### Step 2 — Re-auth (only if refresh token is dead)

Preferred on Linux: use the Cline CLI if it runs on that machine:

```bash
npm install -g cline
cline auth cline        # opens browser, signs in via WorkOS
```

This writes fresh tokens straight into `~/.cline/data/settings/providers.json`
under the `cline-pass` key — exactly where the bridge reads them.

If the Cline CLI won't run on that PC (old CPU, missing AVX2 — same issue as the
Windows machine), use the VS Code Cline extension instead:
1. Install VS Code + Cline extension on the Linux PC
2. Sign in with ClinePass using nibsstudents101@gmail.com
3. Tokens land in `~/.cline/data/secrets.json`
4. Ask the Hermes agent on that PC: "extract tokens from secrets.json into
   providers.json" (it has the same extraction procedure documented in
   E:\clinepass-bridge\REFERENCE.md on the Windows side — or just paste it the
   JSON block from this package)

### Step 3 — Run the bridge

```bash
cd ~/cline-pass-hermes-bridge
python3 cline_pass_bridge.py
# verify:
curl http://127.0.0.1:8317/health
```

Requirements: Python 3.8+, stdlib only, no pip installs.

### Step 4 — Auto-start on boot (Linux equivalent of the Windows VBS)

Option A — systemd user service (recommended):

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/clinepass-bridge.service <<'EOF'
[Unit]
Description=ClinePass Hermes Bridge
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 %h/cline-pass-hermes-bridge/cline_pass_bridge.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now clinepass-bridge
loginctl enable-linger $USER   # keeps it running after logout
```

Option B — crontab (simpler, no systemd):

```bash
crontab -e
# add:
@reboot /usr/bin/python3 $HOME/cline-pass-hermes-bridge/cline_pass_bridge.py >> $HOME/cline-pass-hermes-bridge/bridge.log 2>&1
```

NOTE: unlike Windows, `Restart=on-failure` in systemd is SAFE here — it's a proper
service manager with backoff, not the Task Scheduler popup disaster we had.

### Step 5 — Hermes config on the Linux PC

In that PC's `~/.hermes/config.yaml` (or `~/.config/hermes/config.yaml`):

```yaml
model:
  default: cline-pass/kimi-k3
  provider: custom:cline-pass

custom_providers:
  - name: cline-pass
    base_url: http://127.0.0.1:8317/v1
    api_key: bridge-managed-oauth
    model: cline-pass/kimi-k3
```

Do NOT set `model.base_url` or `model.api_key` at the top level — that's what
caused silent fallback switching on the Windows side.

---

## Q: "When we auth, will the OAuth token move to where the bridge moved?"

No. Tokens never move between machines:

- Each PC has its OWN providers.json with its OWN token pair
- Signing in on the Linux PC creates a NEW, INDEPENDENT WorkOS session for
  nibsstudents101@gmail.com — it does NOT touch the Windows PC's tokens
- Both PCs can use the same account simultaneously (round-robin on Windows,
  main on Linux) — WorkOS allows concurrent sessions
- Each bridge refreshes its own tokens independently every hour
- The only shared limit: Cline's per-account rate/quota. Two machines using
  nibsstudents101@gmail.com at once share that account's usage allowance.

If you ever sign OUT on one PC, only that PC's session dies.

---

## Questions for you (the Linux-side operator) — answer these when asking for help

1. What distro and Python version? (`cat /etc/os-release; python3 --version`)
2. What CPU? (`lscpu | grep -i avx` — needed to know if Cline CLI will crash)
3. What does `python3 cline_pass_bridge.py --check` output? (paste it)
4. Is the Cline CLI installed? (`cline --version` — does it run or crash?)
5. Is VS Code + Cline extension an option on that machine?
6. What does `curl http://127.0.0.1:8317/health` return right now?
7. Paste the last 20 lines of bridge.log if the bridge starts but fails:
   `tail -20 ~/cline-pass-hermes-bridge/bridge.log`

---

## Troubleshooting quick map

| Symptom | Look at | Fix |
|---------|---------|-----|
| "tokens expired" | `--check` output | If REVOKED → Step 2 re-auth |
| connection refused | is bridge running? `ss -tlnp \| grep 8317` | start bridge, set up auto-start |
| 401 from upstream | bridge.log | token stale — bridge retries once, then re-auth |
| 429 rate limit | bridge.log | account quota — wait, or that's the shared-usage limit |
| Hermes uses wrong model | config.yaml | remove top-level base_url/api_key from model section |
| cline CLI segfaults | CPU lacks AVX2 | use VS Code extension path instead |
