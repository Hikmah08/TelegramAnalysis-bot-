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

async function serveDashboardAsset(request, env, pathname) {
  const assetUrl = new URL(request.url);
  assetUrl.pathname = pathname;
  if (pathname !== '/index.html') assetUrl.search = '';

  const assetRequest = new Request(assetUrl, {
    method: 'GET',
    headers: request.headers,
  });
  const assetResponse = await env.ASSETS.fetch(assetRequest);
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

export async function handleDashboardRequest(request, env, hashDashboardToken) {
  const url = new URL(request.url);
  const { pathname } = url;

  if (request.method === 'GET' && pathname === '/auth/login') {
    return serveDashboardAsset(request, env, '/index.html');
  }

  if (request.method === 'GET' && (pathname === '/dashboard' || pathname === '/dashboard/')) {
    return serveDashboardAsset(request, env, '/index.html');
  }

  if (pathname === '/api/auth/token' && request.method === 'POST') {
    try {
      const body = await request.json();
      const token = typeof body === 'object' && body !== null && 'token' in body
        ? body.token
        : undefined;
      if (typeof token !== 'string' || token.length === 0 || token.length > 256) {
        return jsonResponse({ error: 'Missing or invalid token' }, 400);
      }

      const nowIso = new Date().toISOString();
      const tokenHash = await hashDashboardToken(token);
      const tokenRecord = await env.DB.prepare(
        'UPDATE dashboard_auth_tokens SET is_used = 1 WHERE token_hash = ? AND is_used = 0 AND expires_at > ? RETURNING user_id'
      ).bind(tokenHash, nowIso).first();

      if (!tokenRecord) {
        return jsonResponse({ error: 'Invalid or expired token' }, 401);
      }

      const sessionId = crypto.randomUUID().replace(/-/g, '') + crypto.randomUUID().replace(/-/g, '');
      const sessionExpiry = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString();
      await env.DB.prepare(
        'INSERT INTO dashboard_sessions (session_id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)'
      ).bind(sessionId, tokenRecord.user_id, nowIso, sessionExpiry).run();

      return jsonResponse({ ok: true }, 200, {
        'Set-Cookie': `session_id=${sessionId}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=604800`,
      });
    } catch (error) {
      console.error('[Dashboard] Token exchange failed', {
        errorType: error instanceof Error ? error.name : 'UnknownError',
      });
      return jsonResponse({ error: 'Unable to exchange dashboard token' }, 500);
    }
  }

  if (pathname === '/api/user/summary' && request.method === 'GET') {
    const sessionId = getSessionId(request);
    if (!sessionId) return jsonResponse({ error: 'Unauthorized' }, 401);

    const nowIso = new Date().toISOString();
    const session = await env.DB.prepare(
      'SELECT * FROM dashboard_sessions WHERE session_id = ? AND expires_at > ?'
    ).bind(sessionId, nowIso).first();
    if (!session) return jsonResponse({ error: 'Session expired or invalid' }, 401);

    const userId = session.user_id;
    const balance = await env.DB.prepare(
      'SELECT * FROM user_balances WHERE user_id = ?'
    ).bind(userId).first();
    const positions = await env.DB.prepare(
      'SELECT * FROM paper_positions WHERE user_id = ? AND status = "OPEN"'
    ).bind(userId).all();
    const history = await env.DB.prepare(
      'SELECT * FROM paper_positions WHERE user_id = ? AND status != "OPEN" ORDER BY exit_time DESC LIMIT 50'
    ).bind(userId).all();
    const watchlist = await env.DB.prepare(
      'SELECT * FROM user_watchlist WHERE user_id = ? AND is_active = 1'
    ).bind(userId).all();

    return jsonResponse({
      user_id: userId,
      balance: balance || null,
      positions: positions.results || [],
      history: history.results || [],
      watchlist: watchlist.results || [],
      timestamp: nowIso,
    }, 200, {
      'Cache-Control': 'private, no-store',
    });
  }

  return null;
}
