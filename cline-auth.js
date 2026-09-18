#!/usr/bin/env node
/**
 * ClinePass Device Auth - Standalone (no native binary needed)
 * 
 * Run: node cline-auth.js
 * Then visit the URL shown and enter the code.
 * Tokens are saved to ~/.cline/data/settings/providers.json
 */

const https = require('https');
const fs = require('fs');
const path = require('path');
const os = require('os');

const PROVIDERS_PATH = path.join(os.homedir(), '.cline', 'data', 'settings', 'providers.json');
const WORKOS_CLIENT_ID = 'client_01K3A541FN8TA3EPPHTD2325AR';
const WORKOS_API_BASE = 'https://api.workos.com';
const CLINE_API_BASE = 'https://api.cline.bot/api/v1';

function post(urlStr, body, headers = {}) {
    return new Promise((resolve, reject) => {
        const u = new URL(urlStr);
        const data = typeof body === 'string' ? body : JSON.stringify(body);
        const options = {
            hostname: u.hostname,
            path: u.pathname,
            method: 'POST',
            headers: {
                'Content-Type': typeof body === 'string' ? 'application/x-www-form-urlencoded' : 'application/json',
                'Content-Length': Buffer.byteLength(data),
                'User-Agent': 'cline-cli/3.0.51',
                ...headers,
            },
            timeout: 30000,
        };
        const req = https.request(options, (res) => {
            let body = '';
            res.on('data', c => body += c);
            res.on('end', () => {
                try { resolve({ status: res.statusCode, body: JSON.parse(body) }); }
                catch(e) { resolve({ status: res.statusCode, body }); }
            });
        });
        req.on('error', reject);
        req.on('timeout', () => { req.destroy(); reject(new Error('Request timeout')); });
        req.write(data);
        req.end();
    });
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function main() {
    console.log('=== ClinePass Device Auth ===\n');

    // Step 1: Get device code
    console.log('Requesting device code...');
    const deviceAuth = await post(`${WORKOS_API_BASE}/user_management/authorize/device`,
        `client_id=${WORKOS_CLIENT_ID}`);

    if (deviceAuth.status !== 200) {
        console.error('Device auth failed:', deviceAuth.status, JSON.stringify(deviceAuth.body));
        process.exit(1);
    }

    const { device_code, user_code, verification_uri, verification_uri_complete, expires_in, interval } = deviceAuth.body;

    console.log('\n========================================');
    console.log('  ACTION REQUIRED');
    console.log('========================================');
    console.log(`  Visit: ${verification_uri_complete || verification_uri}`);
    console.log(`  Code:  ${user_code}`);
    console.log(`  Expires in: ${expires_in}s`);
    console.log('========================================\n');
    console.log('Waiting for authentication... (polling every', interval || 5, 'seconds)');

    // Step 2: Poll for tokens
    const deadline = Date.now() + (expires_in * 1000);
    let pollInterval = Math.max(1, interval || 5);
    let tokens = null;

    while (Date.now() < deadline) {
        await sleep(pollInterval * 1000);

        let authResult;
        try {
            authResult = await post(`${WORKOS_API_BASE}/user_management/authenticate`,
                `grant_type=urn:ietf:params:oauth:grant-type:device_code&device_code=${device_code}&client_id=${WORKOS_CLIENT_ID}`);
        } catch (e) {
            // transient network timeout on slow links — retry, don't die
            process.stdout.write('T');
            continue;
        }

        if (authResult.status === 200) {
            tokens = authResult.body;
            console.log('\nAuthentication successful!');
            break;
        }

        const err = authResult.body?.error;
        if (err === 'authorization_pending') {
            process.stdout.write('.');
            continue;
        } else if (err === 'slow_down') {
            pollInterval += 1;
            continue;
        } else if (err === 'access_denied' || err === 'expired_token' || err === 'invalid_grant') {
            console.error('\nAuth denied or expired:', err);
            process.exit(1);
        } else {
            console.error('\nUnexpected error:', authResult.status, JSON.stringify(authResult.body));
            process.exit(1);
        }
    }

    if (!tokens) {
        console.error('\nTimed out waiting for authentication');
        process.exit(1);
    }

    // Step 3: Register with Cline API
    console.log('\nRegistering with Cline API...');
    const registerResult = await post(`${CLINE_API_BASE}/auth/register`, {
        accessToken: tokens.access_token,
        refreshToken: tokens.refresh_token,
    });

    if (registerResult.status !== 200) {
        console.error('Registration failed:', registerResult.status, JSON.stringify(registerResult.body));
        process.exit(1);
    }

    const creds = registerResult.body.data;
    const userInfo = creds.userInfo || {};
    const email = userInfo.email || 'unknown';

    console.log(`\nAuthenticated as: ${email}`);

    // Step 4: Save to providers.json
    const now = Date.now();
    const newProvider = {
        id: 'cline-pass',
        settings: {
            provider: 'cline-pass',
            auth: {
                accessToken: creds.accessToken,
                refreshToken: creds.refreshToken,
                expiresAt: new Date(creds.expiresAt).getTime(),
                accountId: userInfo.clineUserId || creds.accountId,
                metadata: {
                    sessionStartedAtMs: now,
                    tokenType: creds.tokenType || 'Bearer',
                    userInfo: {
                        subject: userInfo.subject,
                        clineUserId: userInfo.clineUserId,
                        email: email,
                        firstName: userInfo.firstName || '',
                        lastName: userInfo.lastName || '',
                        accounts: userInfo.accounts || null,
                        name: userInfo.name || email,
                    },
                    provider: 'cline',
                },
            },
            model: 'cline-pass/deepseek-v4-flash',
            reasoning: { enabled: true, effort: 'high' },
        },
        updatedAt: new Date().toISOString(),
        tokenSource: 'oauth',
    };

    // Read existing providers.json
    let providers = { version: 1, lastUsedProvider: 'cline-pass', providers: {} };
    if (fs.existsSync(PROVIDERS_PATH)) {
        try { providers = JSON.parse(fs.readFileSync(PROVIDERS_PATH, 'utf8')); } catch(e) {}
    }

    // Find next available slot
    let slot = 'cline-pass';
    let i = 1;
    while (providers.providers[slot]) { i++; slot = `cline-pass-${i}`; }
    newProvider.id = slot;
    newProvider.settings.provider = slot;

    providers.providers[slot] = newProvider;
    providers.lastUsedProvider = slot;

    fs.mkdirSync(path.dirname(PROVIDERS_PATH), { recursive: true });
    fs.writeFileSync(PROVIDERS_PATH, JSON.stringify(providers, null, 2));

    console.log(`\nSaved to providers.json as: ${slot}`);
    console.log(`Email: ${email}`);
    console.log(`Access token expires: ${creds.expiresAt}`);
    console.log('\nDone! Restart the bridge to pick up the new account.');
}

main().catch(e => {
    console.error('Fatal error:', e.message);
    if (e.code) console.error('Code:', e.code);
    process.exit(1);
});
