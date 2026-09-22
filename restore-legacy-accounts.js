// Restore cline-pass-2 / cline-pass-3 accounts (fresh tokens from probe) into live providers.json.
const fs = require("fs");
const BACKUP = "C:/Users/Gasho/.cline/data/settings/providers.json.bak.20260916_104829";
const LIVE = "C:/Users/Gasho/.cline/data/settings/providers.json";
const FRESH = "C:/Users/Gasho/cline-pass-hermes-bridge/.probe-ok.json";

const mask = (s, n = 8) =>
  s == null ? String(s) : typeof s === "string" ? (s.length > n ? s.slice(0, n) + "…" : s) : JSON.stringify(s);

const live = JSON.parse(fs.readFileSync(LIVE, "utf8"));
const bakData = JSON.parse(fs.readFileSync(BACKUP, "utf8"));
const fresh = JSON.parse(fs.readFileSync(FRESH, "utf8"));
const freshByKey = new Map(fresh.map((r) => [r.key, r]));

// safety: only touch if live doesn't already have an auth-blocked cline-pass-2/3
for (const key of ["cline-pass-2", "cline-pass-3"]) {
  const existing = (live.providers[key] || {}).settings || {};
  if (existing.auth && existing.auth.refreshToken) {
    console.log(`ABORT: ${key} already has auth on disk`);
    process.exit(2);
  }
}

const ts = new Date().toISOString().replace(/[:.]/g, "-");
fs.copyFileSync(LIVE, LIVE + ".restore-" + ts);
console.log("Backed up live providers.json -> providers.json.restore-" + ts);

const nowMs = Date.now();
for (const key of ["cline-pass-2", "cline-pass-3"]) {
  const f = freshByKey.get(key);
  if (!f) {
    console.log(`SKIP ${key}: no fresh token staged`);
    continue;
  }
  const bakEntry = (bakData.providers[key] || {});
  const bakSettings = bakEntry.settings || {};
  const bakAuth = bakSettings.auth || {};
  const oldMetadata = bakAuth.metadata || {};

  const auth = {
    accessToken: "workos:" + f.access_token,
    refreshToken: f.refresh_token,
    expiresAt: nowMs + 3600 * 1000,
    accountId: bakAuth.accountId,
    metadata: oldMetadata,
  };

  const settings = {
    provider: bakSettings.provider || "cline-pass",
    model: bakSettings.model || "cline-pass/" + f.email.split("@")[0] || "cline-pass/kimi-k3",
    reasoning: bakSettings.reasoning || { effort: "xhigh", budgetTokens: 1471 },
    auth,
  };
  if (bakSettings.sap) settings.sap = bakSettings.sap;

  live.providers[key] = {
    settings,
    updatedAt: new Date().toISOString(),
    tokenSource: "oauth",
  };

  const ui = oldMetadata.userInfo || {};
  console.log(
    `RESTORED ${key} -> ${ui.email || "?"} | access(workos:${mask(f.access_token, 12)}...) rt:${mask(f.refresh_token, 6)}... exp:+3600s | model:${settings.model}`
  );
}

fs.writeFileSync(LIVE, JSON.stringify(live, null, 2));
console.log("Wrote live providers.json with restored accounts.");
