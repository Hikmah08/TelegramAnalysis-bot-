import assert from 'node:assert/strict';
import test from 'node:test';
import { handleAdminRequest } from '../src/admin.js';

const TOKEN = 'admin-one-time-token';
const TOKEN_HASH = 'hashed-admin-token';

class AdminDb {
  constructor() {
    this.users = new Map([
      ['1001', { user_id: '1001', username: 'operator', first_name: 'Operator', role: 'ADMIN', status: 'ACTIVE', registered_at: '2026-11-01T10:00:00.000Z', last_active_at: new Date().toISOString() }],
      ['2002', { user_id: '2002', username: 'trader', first_name: 'Trader', role: 'USER', status: 'ACTIVE', registered_at: '2026-11-05T10:00:00.000Z', last_active_at: new Date().toISOString() }],
      ['3003', { user_id: '3003', username: 'paused', first_name: 'Paused', role: 'USER', status: 'SUSPENDED', registered_at: '2026-11-06T10:00:00.000Z', last_active_at: '2026-11-07T10:00:00.000Z' }],
    ]);
    this.tokens = new Map();
    this.sessions = new Map();
    this.activity = [
      { user_id: '2002', command_name: '/start', event_type: 'USER_JOINED', created_at: new Date().toISOString() },
      { user_id: '2002', command_name: '/analyze', event_type: 'COMMAND_USED', created_at: new Date().toISOString() },
    ];
    this.audit = [];
    this.positions = new Map([['position-1', { user_id: '2002', entry_price: 10, pnl: 5 }]]);
    this.queries = [];
  }

  prepare(query) {
    const db = this;
    return {
      async all() {
        return this.bind().all();
      },
      bind(...values) {
        return {
          query,
          params: values,
          async first() {
            db.queries.push({ query, values });
            if (query.includes('FROM dashboard_sessions s')) {
              const [sessionId, now, ...ids] = values;
              const session = db.sessions.get(sessionId);
              const user = session && db.users.get(session.user_id);
              return session?.expires_at > now && user?.status === 'ACTIVE' && user?.role === 'ADMIN' && ids.includes(user.user_id)
                ? { user_id: user.user_id }
                : null;
            }
            if (query.includes('FROM dashboard_auth_tokens t')) {
              const [hash, now, ...ids] = values;
              const token = db.tokens.get(hash);
              const user = token && db.users.get(token.user_id);
              return token?.is_used === 0 && token.expires_at > now && user?.status === 'ACTIVE'
                && user?.role === 'ADMIN' && ids.includes(user.user_id)
                ? { user_id: user.user_id }
                : null;
            }
            if (query.includes('UPDATE dashboard_auth_tokens')) {
              const [hash, now, ...ids] = values;
              const token = db.tokens.get(hash);
              const user = token && db.users.get(token.user_id);
              if (!token || token.is_used !== 0 || token.expires_at <= now || user?.status !== 'ACTIVE'
                || user?.role !== 'ADMIN' || !ids.includes(user.user_id)) return null;
              token.is_used = 1;
              return { user_id: token.user_id };
            }
            if (query.includes('AS total_users')) {
              const [activeSince, today, ...adminIds] = values;
              const publicUsers = [...db.users.values()].filter(user => !adminIds.includes(user.user_id));
              return {
                total_users: publicUsers.length,
                active_users: publicUsers.filter(user => user.status === 'ACTIVE' && user.last_active_at >= activeSince).length,
                new_today: publicUsers.filter(user => user.registered_at.slice(0, 10) === today).length,
                suspended_users: publicUsers.filter(user => user.status === 'SUSPENDED').length,
              };
            }
            if (query.includes('SELECT user_id, status FROM users')) {
              const user = db.users.get(values[0]);
              return user ? { user_id: user.user_id, status: user.status, role: user.role } : null;
            }
            return null;
          },
          async all() {
            db.queries.push({ query, values });
            if (query.includes('FROM bot_activity a JOIN users')) {
              const adminIds = values;
              return {
                results: [...db.activity].filter(event => !adminIds.includes(event.user_id)).reverse().slice(0, 20).map(event => ({
                  ...event,
                  username: db.users.get(event.user_id)?.username || null,
                  first_name: db.users.get(event.user_id)?.first_name || null,
                })),
              };
            }
            if (query.includes('FROM bot_activity') && query.includes('GROUP BY command_name')) {
              const counts = new Map();
              for (const event of db.activity) {
                if (event.event_type === 'COMMAND_USED' && event.command_name && !values.includes(event.user_id)) {
                  counts.set(event.command_name, (counts.get(event.command_name) || 0) + 1);
                }
              }
              return { results: [...counts].map(([command_name, uses]) => ({ command_name, uses })) };
            }
            if (query.includes('FROM users WHERE')) {
              const adminIds = values.slice(0, 1);
              const users = [...db.users.values()].filter(user => !adminIds.includes(user.user_id));
              const statusIndex = values.findIndex(value => value === 'ACTIVE' || value === 'SUSPENDED');
              const filteredByStatus = statusIndex >= 0 ? users.filter(user => user.status === values[statusIndex]) : users;
              const search = values.find(value => typeof value === 'string' && value.startsWith('%'));
              const filtered = search
                ? filteredByStatus.filter(user => [user.username, user.first_name, user.user_id].some(value => value?.toLowerCase().includes(search.slice(1, -1).toLowerCase())))
                : filteredByStatus;
              return {
                results: filtered.map(({ user_id, username, first_name, registered_at, last_active_at, status }) => ({
                  user_id, username, first_name, registered_at, last_active_at, status,
                })),
              };
            }
            return { results: [] };
          },
          async run() {
            db.queries.push({ query, values });
            if (query.startsWith('INSERT INTO dashboard_sessions')) {
              const [session_id, user_id, created_at, expires_at] = values;
              db.sessions.set(session_id, { session_id, user_id, created_at, expires_at });
            }
            return { success: true, meta: { changes: 1 } };
          },
        };
      },
    };
  }

  async batch(statements) {
    this.beforeBatch?.();
    const first = statements[0];
    this.queries.push(...statements.map(statement => ({ query: statement.query, values: statement.params })));
    const [status, targetId, expected, ...guardIds] = first.params;
    const target = this.users.get(targetId);
    if (!target || target.status !== expected) return [{ meta: { changes: 0 } }];
    if (status === 'SUSPENDED' && target.role === 'ADMIN') {
      const adminIds = guardIds.slice(0, guardIds.length / 2);
      const activeAdmins = [...this.users.values()].filter(user =>
        user.role === 'ADMIN' && user.status === 'ACTIVE' && adminIds.includes(user.user_id));
      if (activeAdmins.length <= 1) return [{ meta: { changes: 0 } }];
    }

    target.status = status;
    const auditStatement = statements[1];
    const [adminId, auditTargetId, action, createdAt] = auditStatement.params;
    this.audit.push({ admin_user_id: adminId, target_user_id: auditTargetId, action, created_at: createdAt });
    this.activity.push({
      user_id: targetId,
      command_name: null,
      event_type: action,
      created_at: createdAt,
    });
    if (status === 'SUSPENDED') {
      for (const [id, session] of this.sessions) if (session.user_id === targetId) this.sessions.delete(id);
      for (const [hash, token] of this.tokens) if (token.user_id === targetId) this.tokens.delete(hash);
    }
    return [{ meta: { changes: 1 } }];
  }
}

function createEnv() {
  return {
    DB: new AdminDb(),
    TELEGRAM_ADMIN_IDS: '1001',
    ASSETS: {
      async fetch(request) {
        return new Response(`admin page ${new URL(request.url).pathname}`, {
          headers: { 'Content-Type': 'text/html' },
        });
      },
    },
  };
}

const hashToken = async token => token === TOKEN ? TOKEN_HASH : `wrong:${token}`;

function request(path, { method = 'GET', body, sessionId } = {}) {
  const headers = new Headers();
  if (body !== undefined) headers.set('Content-Type', 'application/json');
  if (sessionId) headers.set('Cookie', `session_id=${sessionId}`);
  return new Request(`https://worker.example${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function addAdminSession(env, userId = '1001') {
  const sessionId = 'a'.repeat(64);
  env.DB.sessions.set(sessionId, {
    session_id: sessionId,
    user_id: userId,
    expires_at: new Date(Date.now() + 60_000).toISOString(),
  });
  return sessionId;
}

test('only active, allowlisted ADMIN users can open the admin page and APIs', async () => {
  const env = createEnv();
  const response = await handleAdminRequest(request('/admin'), env, hashToken);
  assert.equal(response.status, 403);
  assert.equal((await handleAdminRequest(request('/admin?token=invalid'), env, hashToken)).status, 403);

  const publicUserSession = 'b'.repeat(64);
  env.DB.sessions.set(publicUserSession, {
    session_id: publicUserSession,
    user_id: '2002',
    expires_at: new Date(Date.now() + 60_000).toISOString(),
  });
  assert.equal((await handleAdminRequest(request('/admin', { sessionId: publicUserSession }), env, hashToken)).status, 403);
  assert.equal((await handleAdminRequest(request('/api/admin/overview', { sessionId: publicUserSession }), env, hashToken)).status, 403);
  assert.equal((await handleAdminRequest(request('/admin.html'), env, hashToken)).status, 403);
  assert.equal((await handleAdminRequest(request('/%61dmin.html'), env, hashToken)).status, 403);

  env.DB.users.set('9999', {
    user_id: '9999', role: 'ADMIN', status: 'ACTIVE', registered_at: new Date().toISOString(),
    last_active_at: new Date().toISOString(),
  });
  env.DB.sessions.set('d'.repeat(64), {
    session_id: 'd'.repeat(64), user_id: '9999', expires_at: new Date(Date.now() + 60_000).toISOString(),
  });
  assert.equal((await handleAdminRequest(request('/api/admin/overview', { sessionId: 'd'.repeat(64) }), env, hashToken)).status, 403);

  const adminSession = addAdminSession(env);
  const page = await handleAdminRequest(request('/admin', { sessionId: adminSession }), env, hashToken);
  assert.equal(page.status, 200);
  assert.match(await page.text(), /admin page \/admin\.html/);
  assert.equal((await handleAdminRequest(request('/api/admin/overview', { sessionId: adminSession }), env, hashToken)).status, 200);
});

test('admin token exchange is one-time, hashed, and issues only a secure cookie', async () => {
  const env = createEnv();
  env.DB.tokens.set(TOKEN_HASH, {
    token_hash: TOKEN_HASH,
    user_id: '1001',
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    is_used: 0,
  });

  const response = await handleAdminRequest(
    request('/api/admin/auth/token', { method: 'POST', body: { token: TOKEN } }),
    env,
    hashToken
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  const cookie = response.headers.get('Set-Cookie');
  assert.match(cookie, /^session_id=[a-f0-9]{64};/);
  assert.match(cookie, /Path=\/; HttpOnly; Secure; SameSite=Lax/);
  assert.equal(env.DB.tokens.get(TOKEN_HASH).is_used, 1);

  const reuse = await handleAdminRequest(
    request('/api/admin/auth/token', { method: 'POST', body: { token: TOKEN } }),
    env,
    hashToken
  );
  assert.equal(reuse.status, 401);

  env.DB.tokens.set('wrong:public-token', {
    token_hash: 'wrong:public-token',
    user_id: '2002',
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    is_used: 0,
  });
  const normalUserToken = await handleAdminRequest(
    request('/api/admin/auth/token', { method: 'POST', body: { token: 'public-token' } }),
    env,
    hashToken
  );
  assert.equal(normalUserToken.status, 401);
});

test('admin endpoints expose operational metadata only', async () => {
  const env = createEnv();
  env.DB.activity.push({
    user_id: '1001', command_name: '/admin', event_type: 'COMMAND_USED', created_at: new Date().toISOString(),
  });
  const sessionId = addAdminSession(env);
  const overviewResponse = await handleAdminRequest(request('/api/admin/overview', { sessionId }), env, hashToken);
  const overview = await overviewResponse.json();
  assert.equal(overview.total_users, 2);
  assert.ok(overview.recent_activity.some(event => event.event_type === 'COMMAND_USED' && event.command_name === '/analyze'));
  assert.ok(overview.command_usage.some(command => command.command_name === '/analyze'));
  const usersResponse = await handleAdminRequest(request('/api/admin/users?status=ALL&q=paused', { sessionId }), env, hashToken);
  const users = await usersResponse.json();
  assert.equal(users.users.length, 1);
  assert.deepEqual(Object.keys(users.users[0]).sort(), [
    'first_name', 'last_active_at', 'registered_at', 'status', 'user_id', 'username',
  ]);
  const serialized = JSON.stringify({ overview, users });
  for (const privateField of ['balance', 'equity', 'positions', 'entry_price', 'stop_loss', 'take_profit', 'pnl', 'watchlist', 'analysis']) {
    assert.equal(serialized.includes(privateField), false, `admin response must not include ${privateField}`);
  }
  assert.ok(env.DB.queries.every(({ query }) => !/user_balances|paper_positions|signals_queue|user_watchlist/i.test(query)));
});

test('suspension revokes sessions and tokens, audits the action, and preserves trading records', async () => {
  const env = createEnv();
  const sessionId = addAdminSession(env);
  env.DB.sessions.set('c'.repeat(64), {
    session_id: 'c'.repeat(64), user_id: '2002', expires_at: new Date(Date.now() + 60_000).toISOString(),
  });
  env.DB.tokens.set('trader-token-hash', {
    token_hash: 'trader-token-hash', user_id: '2002', is_used: 0,
    expires_at: new Date(Date.now() + 60_000).toISOString(),
  });
  const originalPosition = structuredClone(env.DB.positions.get('position-1'));

  const response = await handleAdminRequest(
    request('/api/admin/users/2002/suspend', { method: 'POST', sessionId }),
    env,
    hashToken
  );
  assert.equal(response.status, 200);
  assert.equal(env.DB.users.get('2002').status, 'SUSPENDED');
  assert.equal([...env.DB.sessions.values()].some(session => session.user_id === '2002'), false);
  assert.equal([...env.DB.tokens.values()].some(token => token.user_id === '2002'), false);
  assert.deepEqual(env.DB.positions.get('position-1'), originalPosition);
  assert.equal(env.DB.audit.at(-1).action, 'USER_SUSPENDED');

  const reactivated = await handleAdminRequest(
    request('/api/admin/users/2002/reactivate', { method: 'POST', sessionId }),
    env,
    hashToken
  );
  assert.equal(reactivated.status, 200);
  assert.equal(env.DB.users.get('2002').status, 'ACTIVE');
  assert.equal(env.DB.sessions.has('c'.repeat(64)), false);
  assert.equal(env.DB.audit.at(-1).action, 'USER_REACTIVATED');
  assert.deepEqual(env.DB.positions.get('position-1'), originalPosition);
});

test('administrator self-suspension is refused', async () => {
  const env = createEnv();
  const sessionId = addAdminSession(env);
  const response = await handleAdminRequest(
    request('/api/admin/users/1001/suspend', { method: 'POST', sessionId }),
    env,
    hashToken
  );
  assert.equal(response.status, 403);
  assert.equal(env.DB.users.get('1001').status, 'ACTIVE');
});

test('last authorized administrator cannot be suspended during a concurrent admin change', async () => {
  const env = createEnv();
  env.TELEGRAM_ADMIN_IDS = '1001,1002';
  env.DB.users.set('1002', {
    user_id: '1002', username: 'second-admin', first_name: 'Second',
    role: 'ADMIN', status: 'ACTIVE', registered_at: new Date().toISOString(),
    last_active_at: new Date().toISOString(),
  });
  const sessionId = addAdminSession(env, '1001');
  env.DB.beforeBatch = () => {
    env.DB.users.get('1001').status = 'SUSPENDED';
  };
  const response = await handleAdminRequest(
    request('/api/admin/users/1002/suspend', { method: 'POST', sessionId }),
    env,
    hashToken
  );
  assert.equal(response.status, 409);
  assert.equal(env.DB.users.get('1002').status, 'ACTIVE');
});
