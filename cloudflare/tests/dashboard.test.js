import assert from 'node:assert/strict';
import test from 'node:test';
import { handleDashboardRequest } from '../src/dashboard.js';

const TOKEN = 'one-time-dashboard-token';
const TOKEN_HASH = 'sha256-hash-for-test-token';

function createEnv({ tokenExpiresAt = new Date(Date.now() + 60_000).toISOString() } = {}) {
  const state = {
    token: {
      token_hash: TOKEN_HASH,
      user_id: 'user-42',
      expires_at: tokenExpiresAt,
      is_used: 0,
    },
    sessions: [],
    queries: [],
    assetRequests: [],
  };

  const env = {
    ASSETS: {
      async fetch(request) {
        state.assetRequests.push(new URL(request.url));
        return new Response(`dashboard asset: ${new URL(request.url).pathname}`, {
          headers: { 'Content-Type': 'text/html' },
        });
      },
    },
    DB: {
      prepare(query) {
        return {
          bind(...values) {
            return {
              async first() {
                state.queries.push({ query, values });
                if (query.startsWith('UPDATE dashboard_auth_tokens')) {
                  const [hash, now] = values;
                  if (state.token?.token_hash !== hash || state.token.is_used !== 0 || state.token.expires_at <= now) {
                    return null;
                  }
                  state.token.is_used = 1;
                  return { user_id: state.token.user_id };
                }
                if (query.startsWith('SELECT * FROM dashboard_sessions')) {
                  const [sessionId, now] = values;
                  return state.sessions.find(item => item.session_id === sessionId && item.expires_at > now) || null;
                }
                if (query.startsWith('SELECT * FROM user_balances')) return { current_balance: 11000, starting_balance: 10000 };
                return null;
              },
              async run() {
                state.queries.push({ query, values });
                if (query.startsWith('INSERT INTO dashboard_sessions')) {
                  const [session_id, user_id, created_at, expires_at] = values;
                  state.sessions.push({ session_id, user_id, created_at, expires_at });
                }
                return { success: true };
              },
              async all() {
                state.queries.push({ query, values });
                return { results: [] };
              },
            };
          },
        };
      },
    },
  };

  return { env, state };
}

function hashToken(token) {
  assert.equal(token, TOKEN);
  return Promise.resolve(TOKEN_HASH);
}

function request(path, { method = 'GET', body, cookie } = {}) {
  const headers = new Headers();
  if (body !== undefined) headers.set('Content-Type', 'application/json');
  if (cookie) headers.set('Cookie', cookie);
  return new Request(`https://worker.example${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

test('dashboard routes serve the existing page for login and dashboard navigation', async () => {
  const { env, state } = createEnv();
  for (const path of ['/auth/login?token=login-token', '/dashboard', '/dashboard/']) {
    const response = await handleDashboardRequest(request(path), env, hashToken);
    assert.equal(response.status, 200);
    assert.match(await response.text(), /dashboard asset: \/index\.html/);
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
  }
  assert.deepEqual(state.assetRequests.map(url => [url.pathname, url.search]), [
    ['/index.html', '?token=login-token'],
    ['/index.html', ''],
    ['/index.html', ''],
  ]);
});

test('valid access token is atomically consumed and creates a secure cookie session', async () => {
  const { env, state } = createEnv();
  const response = await handleDashboardRequest(
    request('/api/auth/token', { method: 'POST', body: { token: TOKEN } }),
    env,
    hashToken
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  const cookie = response.headers.get('Set-Cookie');
  assert.match(cookie, /^session_id=[a-f0-9]{64};/);
  assert.match(cookie, /HttpOnly/);
  assert.match(cookie, /Secure/);
  assert.match(cookie, /SameSite=Lax/);
  assert.match(cookie, /Path=\//);
  assert.equal(state.token.is_used, 1);
  assert.equal(state.sessions.length, 1);
  assert.equal(JSON.stringify({ ok: true }).includes(state.sessions[0].session_id), false);
});

test('invalid, expired, and already-used access tokens are rejected', async t => {
  await t.test('invalid', async () => {
    const { env } = createEnv();
    const response = await handleDashboardRequest(
      request('/api/auth/token', { method: 'POST', body: { token: 'wrong-token' } }),
      env,
      async () => 'wrong-hash'
    );
    assert.equal(response.status, 401);
  });

  await t.test('expired', async () => {
    const { env } = createEnv({ tokenExpiresAt: new Date(Date.now() - 60_000).toISOString() });
    const response = await handleDashboardRequest(
      request('/api/auth/token', { method: 'POST', body: { token: TOKEN } }),
      env,
      hashToken
    );
    assert.equal(response.status, 401);
  });

  await t.test('already used', async () => {
    const { env, state } = createEnv();
    state.token.is_used = 1;
    const response = await handleDashboardRequest(
      request('/api/auth/token', { method: 'POST', body: { token: TOKEN } }),
      env,
      hashToken
    );
    assert.equal(response.status, 401);
  });
});

test('concurrent token exchanges can create only one session', async () => {
  const { env, state } = createEnv();
  const exchange = () => handleDashboardRequest(
    request('/api/auth/token', { method: 'POST', body: { token: TOKEN } }),
    env,
    hashToken
  );
  const responses = await Promise.all([exchange(), exchange()]);
  assert.deepEqual(responses.map(response => response.status).sort(), [200, 401]);
  assert.equal(state.sessions.length, 1);
});

test('summary data requires a valid session and scopes all queries to that user', async () => {
  const { env, state } = createEnv();
  const unauthorized = await handleDashboardRequest(request('/api/user/summary'), env, hashToken);
  assert.equal(unauthorized.status, 401);

  state.sessions.push({
    session_id: 'a'.repeat(64),
    user_id: 'user-42',
    expires_at: new Date(Date.now() + 60_000).toISOString(),
  });
  const response = await handleDashboardRequest(
    request('/api/user/summary', { cookie: `session_id=${'a'.repeat(64)}` }),
    env,
    hashToken
  );
  assert.equal(response.status, 200);
  const data = await response.json();
  assert.equal(data.user_id, 'user-42');
  assert.equal(data.balance.current_balance, 11000);
  assert.ok(state.queries.filter(item => /user_balances|paper_positions|user_watchlist/.test(item.query))
    .every(item => item.values[0] === 'user-42'));
});
