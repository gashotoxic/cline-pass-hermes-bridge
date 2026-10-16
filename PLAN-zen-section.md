# PLAN — OpenCode Zen (free tier) in ClinePass Bridge — v2 (re-evaluated)

Status: RESEARCH COMPLETE / AWAITING OWNER CONFIRM (revised after adversarial re-evaluation)
Date: 2026-02-16
Target: `cline_pass_bridge.py` (port 8317) — **one bridge, two sections** (Section A Cline unchanged, Section B Zen new). No new bridge.

v2 changes: v1 claims "session id not validated" and "auth optional" were WRONG (caught in
re-evaluation). Corrected recipe, failure-mode analysis, and multi-account (round-robin) design added.

---

## 0. Answers to the open questions

**API key or auth flow for the free models?** NEITHER — anonymous client attestation (verified by
capture + replay). **But**: an OpenCode Console API key is the *account* unit for round-robin (§3).

**Two sections or new bridge?** One bridge, two sections; Section B routes only `zen/<id>`, fully
independent failure domain.

**2-3 accounts round-robin to dodge the rate limit?** YES — right pattern (same as Section A), with
one caveat: the free-tier gate is NOT a clean per-account rate limit (evidence in §2). Round-robin
buys you **dedicated per-account quota instead of the shared anonymous pool + higher ceiling**,
not immunity. Design in §3.

---

## 1. Verified gate recipe (the ONLY thing that passes)

```
POST https://opencode.ai/zen/v1/chat/completions
Content-Type: application/json
User-Agent: opencode/latest/2.0.30/cli          # version NOT pinned (9.9.9 passes)
x-opencode-client: cli
x-opencode-session: ses_<9hex>ffe<14alnum>      # exact format, value free
Authorization: Bearer public                    # or omit; or Bearer <console-key>

body: stream: true                              # stream:false ALWAYS rejected
      tools[] must contain a tool NAMED exactly "read"   # schema free, description free
```

**Hard gates (paced tests, cannot be noise):**

| Rule | Verdict |
|---|---|
| `x-opencode-session` present, format `ses_` + 9 hex + `ffe` + 14 alnum (26 after prefix) | REQUIRED. Wrong length/no-`ffe`/short/long tail → always FAIL |
| `x-opencode-client: cli` + opencode User-Agent | REQUIRED |
| `stream: true` | REQUIRED (stream:false → always FAIL) |
| tool named exactly `read` in `tools` (any schema, `type:"function"` optional) | REQUIRED (empty tools / `write`-only → always FAIL) |
| **Any Authorization other than absent/`Bearer public`/`Bearer <zen-key>` → FAIL** | CRITICAL: `Bearer bridge-managed-oauth` (what pi sends!) is REJECTED. Must strip/replace |
| Extra sibling session headers (`x-opencode-session-id`, `x-session-affinity`, `x-session-id`) | DO NOT SEND unless all values == the session id (mismatched values → FAIL) |
| `x-opencode-project` | Harmless if present. Unnecessary. |
| System prompt content, `max_tokens` vs `max_completion_tokens`, temperature/tool_choice/user extras | Irrelevant / pass through |

## 2. Failure-mode analysis (what the noise is, and is not)

Reprobed with pacing, parallelism, retries:
- 3 **parallel** requests (distinct session ids) → 3/3 PASS (no concurrency limit)
- **Same session id** back-to-back retry → PASS (no per-session throttle; ids are free to reuse)
- 6 rapid 1s-gap burst → 6/6 PASS once; other times 2-3 in a row FAIL with identical recipe
- **Same id FAILS at one minute and PASSES minutes later** (observed 2×) → not per-id, not deterministic

Conclusion: failures on the valid recipe are **flaky shared-pool admission** (capacity/canary of the
anonymous `public` pool), not a rate limit we can key around. Hence:
- retries + backoff fix reliability;
- accounts fix *quota identity* (and the failure mode plausibly worsens as the teaser pool saturates);
- if the pool is globally saturated, only keys/paid or patience help — retry budget must be generous.

## 3. Multi-account round-robin (the Section A pattern, applied to Zen)

Two auth tiers, same rotation machinery as `MultiAccountStore`:

- **Tier 1 — Zen keys** (round-robin primary): `ZEN_KEYS=key1,key2,key3` (OpenCode Console keys,
  free models $0; no Cline-style OAuth needed). Requests carry `Authorization: Bearer <key>`;
  attestation shaping still applied (harmless) but keys also unlock paid Zen models if ever wanted.
  Policy: round-robin; on 401/402/429/FreeTierError → `mark_failure(key)` + cooldown (60s) →
  next key; permanent 401 → cooldown 1h + log.
- **Tier 2 — anonymous attestation** (fallback when no key succeeds): the §1 recipe,
  `Bearer public`, fresh minted session id per attempt, retries (3× exponential backoff) on
  FreeTierError.
- Response relay identical in both tiers (SSE passthrough, or SSE→chat.completion synthesis for
  non-streaming clients — upstream rejects stream:false, so synthesis is unavoidable).

Expected behaviour: keys first = stable quota per account (2-3 keys ≈ 2-3× ceiling + failover);
anonymous = best-effort last resort. **Validation once key #1 exists:** during a flaky window, send
keyed and anonymous side-by-side; if keyed passes while anonymous flakes → per-account quota
confirmed, rotate with confidence.

## 4. Implementation spec (bridge)

1. **Config**: `ZEN_ENABLED` (default 1), `ZEN_KEYS` (comma-separated, optional), `ZEN_BASE_URL`
   (`https://opencode.ai/zen/v1`), `ZEN_CLIENT_VERSION` (2.0.30), `ZEN_SESSION_POOL`.
2. **Session id minting**: `ses_` + 9 random hex + `ffe` + 14 random alnum; fresh per attempt
   (reusable but fresh is cleaner); never forward client-supplied ids.
3. **Request shaping (zen route only)**: strip incoming `Authorization` (replace with
   `Bearer public` or key) — **pi/Hermes send `Bearer bridge-managed-oauth` which is fatal**;
   force `stream:true`; ensure a tool named `read` exists (inject placeholder if absent; if client
   already defines `read` — pi and Hermes do — leave it alone); drop mismatched sibling session
   headers; set UA + `x-opencode-client: cli` + `x-opencode-session`.
4. **Routing**: `do_POST` branches on `model.startswith("zen/")` BEFORE any Cline account logic;
   strip prefix. Zen failures never touch Cline stores/cooldowns.
5. **Response**: SSE passthrough when client asked stream; else accumulate SSE and synthesize one
   `chat.completion` (merge `delta.content` + `delta.reasoning_content` + `delta.tool_calls[index]`,
   final `finish_reason`, keep `usage` from `stream_options.include_usage`). Set `model` to the
   requested `zen/<id>`.
6. **Catalog**: `_zen_models()` live fetch of `/models` (key or `Bearer public`), 15-min cache,
   filter `*-free` + `{big-pickle}` allowlist (excl. `test`), static 13-id fallback on fetch failure.
   Append as `zen/<id>`, `owned_by: "opencode-zen"`. Not subject to Cline `_is_model_allowed` pruning.
7. **Health**: `/health` gains `"zen": {"enabled", "tier", "keys_total", "keys_ok"}`.
8. **Tool-call interception**: if model calls an injected `read` the client didn't define, return
   `{"error":"read is disabled in this configuration"}` as tool result (don't return empty).
9. **CLI**: `--zen-check` runs the §1 recipe probe and prints PASS/FAIL (post-update canary).

## 5. Rollout

1. `git commit` current state in the bridge repo (it IS a git repo; .bak files alone are sloppy).
2. Implement §4; restart bridge (note: port 8317 has TWO pythonw listeners, PIDs 11308+2248 —
   investigate/fix duplicate instance while restarting).
3. Smoke tests §6; `MAINTENANCE-GUIDE.md` + README architecture note.
4. pi `models.json`: append zen entries to `cline-bridge` block (`cost: 0`, `reasoning: true` for
   glm/deepseek/jev/longcat/mimo/nemotron; `false` for muse-spark/ll-alpha; conservative
   context/maxTokens 131072/32768 until measured). pi needs restart to pick up.

## 6. Verification

1. `/v1/models` lists `zen/glm-5.3-flash-free` etc.
2. Non-stream request → standard `chat.completion` with `choices` (synthesis path).
3. Stream request → SSE chunks (passthrough path).
4. **Request carrying `Authorization: Bearer bridge-managed-oauth` (pi's exact header) completes.**
5. Invalid-format session cannot occur (bridge mints); simulated FreeTierError retries then succeeds.
6. Cline regression: `cline:z-ai/glm-5.3-flash` untouched and passing; `/health` healthy.
7. `--zen-check` prints PASS.

## 7. Risks & fallbacks

- Gate heuristics can change (the `read`-tool marker is server-controlled). Fallback ladder:
  (1) keys already in place if gates break for anonymous; (2) `opencode serve` sidecar (Plan C:
  bridge forwards to local opencode server — gate-proof since it IS the official client; cost:
  extra daemon + the observed "Background service failed to start" headless flakiness).
- Free models "free for a limited time" + churn (live catalog + static fallback covers removals).
- `cat-jam-free` geo-blocked (NG/RU/CN/…): listed but expect upstream 403 there.
- Attestation replication is ToS-gray: personal-use interop; do not publish the bridge.
- Injected `read` may be called occasionally (mitigated: skip injection when client has `read`;
  intercept otherwise).

## 8. Open questions for owner

1. Create 1-3 OpenCode Console accounts (console.opencode.ai) for `ZEN_KEYS`? (recommended; $0)
2. Keep anonymous Tier 2 enabled at all, or keys-only once provisioned?
