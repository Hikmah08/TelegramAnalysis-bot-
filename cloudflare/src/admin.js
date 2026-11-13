function jsonResponse(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-store',
      ...headers,
    },
  });
}

function adminIds(env) {
  return [...new Set((env.TELEGRAM_ADMIN_IDS || '')
    .split(',')
    .map(id => id.trim())
    .filter(id => /^\d+$/.test(id)))];
}

function sqlPlaceholders(count) {
  return Array(count).fill('?').join(', ');
}

function sessionCookie(sessionId) {
  return `session_id=${sessionId}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=604800`;
}

async function serveAdminAsset(request, env) {
  const assetUrl = new URL(request.url);
  assetUrl.pathname = '/admin.html';
  assetUrl.search = '';
  const assetResponse = await env.ASSETS.fetch(new Request(assetUrl, {
    method: 'GET',
    headers: request.headers,
  }));
  const headers = new Headers(assetResponse.headers);
  headers.set('Cache-Control', 'no-store');
  return new Response(assetResponse.body, {
    status: assetResponse.status,
    statusText: assetResponse.statusText,
    headers,
  });
}

function getSessionId(request) {
  const cookie = request.headers.get('Cookie') || '';
  return cookie.match(/(?:^|;\s*)session_id=([a-f0-9]{64})(?:;|$)/i)?.[1] || '';
}

async function authorizedAdmin(request, env) {
  const ids = adminIds(env);
  const sessionId = getSessionId(request);
  if (!ids.length || !sessionId) return null;

  const admin = await env.DB.prepare(
    `SELECT u.user_id
     FROM dashboard_sessions s
     JOIN users u ON u.user_id = s.user_id
     WHERE s.session_id = ? AND s.expires_at > ?
       AND u.status = 'ACTIVE' AND u.role = 'ADMIN'
       AND u.user_id IN (${sqlPlaceholders(ids.length)})`
  ).bind(sessionId, new Date().toISOString(), ...ids).first();

  return admin?.user_id || null;
}

async function hasValidAdminToken(token, env, hashDashboardToken) {
  const ids = adminIds(env);
  if (!ids.length) return false;
  const tokenHash = await hashDashboardToken(token);
  const admin = await env.DB.prepare(
    `SELECT t.user_id
     FROM dashboard_auth_tokens t
     JOIN users u ON u.user_id = t.user_id
     WHERE t.token_hash = ? AND t.is_used = 0 AND t.expires_at > ?
       AND u.status = 'ACTIVE' AND u.role = 'ADMIN'
       AND u.user_id IN (${sqlPlaceholders(ids.length)})`
  ).bind(tokenHash, new Date().toISOString(), ...ids).first();
  return Boolean(admin);
}

async function exchangeAdminToken(request, env, hashDashboardToken) {
  const ids = adminIds(env);
  if (!ids.length) return jsonResponse({ error: 'Admin access is not configured' }, 503);

  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ error: 'Invalid request body' }, 400);
  }
  const token = typeof body === 'object' && body !== null && 'token' in body
    ? body.token
    : undefined;
  if (typeof token !== 'string' || token.length === 0 || token.length > 256) {
    return jsonResponse({ error: 'Missing or invalid token' }, 400);
  }

  const now = new Date();
  const nowIso = now.toISOString();
  const tokenHash = await hashDashboardToken(token);
  const tokenRecord = await env.DB.prepare(
    `UPDATE dashboard_auth_tokens
     SET is_used = 1
     WHERE token_hash = ? AND is_used = 0 AND expires_at > ?
       AND EXISTS (
         SELECT 1 FROM users u
         WHERE u.user_id = dashboard_auth_tokens.user_id
           AND u.status = 'ACTIVE' AND u.role = 'ADMIN'
           AND u.user_id IN (${sqlPlaceholders(ids.length)})
       )
     RETURNING user_id`
  ).bind(tokenHash, nowIso, ...ids).first();
  if (!tokenRecord) return jsonResponse({ error: 'Invalid, expired, used, or unauthorized admin token' }, 401);

  const sessionId = crypto.randomUUID().replace(/-/g, '') + crypto.randomUUID().replace(/-/g, '');
  const expiresAt = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000).toISOString();
  await env.DB.prepare(
    'INSERT INTO dashboard_sessions (session_id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)'
  ).bind(sessionId, tokenRecord.user_id, nowIso, expiresAt).run();

  return jsonResponse({ ok: true }, 200, { 'Set-Cookie': sessionCookie(sessionId) });
}

async function overview(env) {
  const ids = adminIds(env);
  const today = new Date().toISOString().slice(0, 10);
  const activeSince = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000).toISOString();
  const totals = await env.DB.prepare(
    `SELECT COUNT(*) AS total_users,
       SUM(CASE WHEN status = 'ACTIVE' AND last_active_at >= ? THEN 1 ELSE 0 END) AS active_users,
       SUM(CASE WHEN substr(registered_at, 1, 10) = ? THEN 1 ELSE 0 END) AS new_today,
       SUM(CASE WHEN status = 'SUSPENDED' THEN 1 ELSE 0 END) AS suspended_users
     FROM users WHERE user_id NOT IN (${sqlPlaceholders(ids.length)})`
  ).bind(activeSince, today, ...ids).first();
  const recent = await env.DB.prepare(
    `SELECT a.user_id, u.username, u.first_name, a.event_type, a.command_name, a.created_at
     FROM bot_activity a JOIN users u ON u.user_id = a.user_id
     WHERE a.user_id NOT IN (${sqlPlaceholders(ids.length)})
     ORDER BY a.created_at DESC LIMIT 20`
  ).bind(...ids).all();
  const commands = await env.DB.prepare(
    `SELECT command_name, COUNT(*) AS uses
     FROM bot_activity
     WHERE event_type = 'COMMAND_USED' AND command_name IS NOT NULL
       AND user_id NOT IN (${sqlPlaceholders(ids.length)})
     GROUP BY command_name ORDER BY uses DESC, command_name ASC LIMIT 10`
  ).bind(...ids).all();

  return {
    total_users: Number(totals?.total_users) || 0,
    active_users: Number(totals?.active_users) || 0,
    new_today: Number(totals?.new_today) || 0,
    suspended_users: Number(totals?.suspended_users) || 0,
    recent_activity: recent.results || [],
    command_usage: commands.results || [],
  };
}

async function listUsers(url, env) {
  const ids = adminIds(env);
  const status = (url.searchParams.get('status') || 'ALL').toUpperCase();
  const search = (url.searchParams.get('q') || '').trim().slice(0, 100);
  if (!['ALL', 'ACTIVE', 'SUSPENDED'].includes(status)) {
    return jsonResponse({ error: 'Invalid status filter' }, 400);
  }

  const conditions = [`user_id NOT IN (${sqlPlaceholders(ids.length)})`];
  const params = [...ids];
  if (status !== 'ALL') {
    conditions.push('status = ?');
    params.push(status);
  }
  if (search) {
    conditions.push("(COALESCE(username, '') LIKE ? ESCAPE '\\' OR COALESCE(first_name, '') LIKE ? ESCAPE '\\' OR user_id LIKE ? ESCAPE '\\')");
    const escaped = search.replace(/[\\%_]/g, '\\$&');
    const pattern = `%${escaped}%`;
    params.push(pattern, pattern, pattern);
  }

  const result = await env.DB.prepare(
    `SELECT user_id, username, first_name, registered_at, last_active_at, status
     FROM users WHERE ${conditions.join(' AND ')}
     ORDER BY registered_at DESC LIMIT 200`
  ).bind(...params).all();
  return jsonResponse({ users: result.results || [] });
}

async function changeUserStatus(adminId, targetId, action, env) {
  if (!/^\d+$/.test(targetId)) return jsonResponse({ error: 'Invalid user identifier' }, 400);
  const ids = adminIds(env);
  const suspending = action === 'suspend';
  if (suspending && targetId === adminId) {
    return jsonResponse({ error: 'Administrators cannot suspend themselves' }, 403);
  }
  const newStatus = suspending ? 'SUSPENDED' : 'ACTIVE';
  const currentStatus = suspending ? 'ACTIVE' : 'SUSPENDED';
  const now = new Date().toISOString();
  const lastAdminGuard = suspending
    ? `AND (role != 'ADMIN' OR user_id NOT IN (${sqlPlaceholders(ids.length)})
       OR (SELECT COUNT(*) FROM users
           WHERE role = 'ADMIN' AND status = 'ACTIVE'
             AND user_id IN (${sqlPlaceholders(ids.length)})) > 1)`
    : '';
  const updateParams = suspending
    ? [newStatus, targetId, currentStatus, ...ids, ...ids]
    : [newStatus, targetId, currentStatus];

  const results = await env.DB.batch([
    env.DB.prepare(
      `UPDATE users SET status = ? WHERE user_id = ? AND status = ? ${lastAdminGuard}`
    ).bind(...updateParams),
    env.DB.prepare(
      `INSERT INTO admin_audit_log (admin_user_id, target_user_id, action, created_at)
       SELECT ?, ?, ?, ? WHERE changes() = 1`
    ).bind(adminId, targetId, suspending ? 'USER_SUSPENDED' : 'USER_REACTIVATED', now),
    env.DB.prepare(
      `INSERT INTO bot_activity (user_id, command_name, event_type, created_at)
       SELECT ?, NULL, ?, ?
       WHERE EXISTS (
         SELECT 1 FROM admin_audit_log
         WHERE admin_user_id = ? AND target_user_id = ? AND action = ? AND created_at = ?
       )`
    ).bind(targetId, suspending ? 'USER_SUSPENDED' : 'USER_REACTIVATED', now,
      adminId, targetId, suspending ? 'USER_SUSPENDED' : 'USER_REACTIVATED', now),
    ...(suspending ? [
      env.DB.prepare(
        `DELETE FROM dashboard_sessions
         WHERE user_id = ? AND EXISTS (
           SELECT 1 FROM admin_audit_log
           WHERE admin_user_id = ? AND target_user_id = ? AND action = 'USER_SUSPENDED' AND created_at = ?
         )`
      ).bind(targetId, adminId, targetId, now),
      env.DB.prepare(
        `DELETE FROM dashboard_auth_tokens
         WHERE user_id = ? AND EXISTS (
           SELECT 1 FROM admin_audit_log
           WHERE admin_user_id = ? AND target_user_id = ? AND action = 'USER_SUSPENDED' AND created_at = ?
         )`
      ).bind(targetId, adminId, targetId, now),
    ] : []),
  ]);
  if (results[0]?.meta?.changes !== 1) {
    const target = await env.DB.prepare(
      'SELECT user_id, status FROM users WHERE user_id = ?'
    ).bind(targetId).first();
    if (!target) return jsonResponse({ error: 'User not found' }, 404);
    if (suspending && target.role === 'ADMIN') {
      return jsonResponse({ error: 'Cannot suspend the last authorized administrator' }, 409);
    }
    return jsonResponse({ error: `User is not currently ${currentStatus.toLowerCase()}` }, 409);
  }

  return jsonResponse({ ok: true, status: newStatus });
}

export async function handleAdminRequest(request, env, hashDashboardToken) {
  const url = new URL(request.url);
  let pathname;
  try {
    pathname = decodeURIComponent(url.pathname).replace(/\/+/g, '/');
  } catch {
    return new Response('Bad Request', { status: 400 });
  }

  if (pathname === '/admin.html' && request.method === 'GET') {
    if (!await authorizedAdmin(request, env)) return new Response('Forbidden', { status: 403 });
    return serveAdminAsset(request, env);
  }

  if (pathname === '/admin' && request.method === 'GET') {
    const token = url.searchParams.get('token');
    if (token) {
      if (!await hasValidAdminToken(token, env, hashDashboardToken)) {
        return new Response('Forbidden', { status: 403 });
      }
      return serveAdminAsset(request, env);
    }
    if (!await authorizedAdmin(request, env)) return new Response('Forbidden', { status: 403 });
    return serveAdminAsset(request, env);
  }

  if (pathname === '/api/admin/auth/token' && request.method === 'POST') {
    return exchangeAdminToken(request, env, hashDashboardToken);
  }

  if (!pathname.startsWith('/api/admin/')) return null;
  const adminId = await authorizedAdmin(request, env);
  if (!adminId) return jsonResponse({ error: 'Forbidden' }, 403);

  if (pathname === '/api/admin/overview' && request.method === 'GET') {
    return jsonResponse(await overview(env));
  }
  if (pathname === '/api/admin/users' && request.method === 'GET') {
    return listUsers(url, env);
  }

  const actionMatch = /^\/api\/admin\/users\/([^/]+)\/(suspend|reactivate)$/.exec(pathname);
  if (actionMatch && request.method === 'POST') {
    return changeUserStatus(adminId, decodeURIComponent(actionMatch[1]), actionMatch[2], env);
  }
  return jsonResponse({ error: 'Not found' }, 404);
}
