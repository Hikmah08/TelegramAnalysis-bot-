import { Env } from './types';
import { handleTelegramUpdate, hashDashboardToken } from './telegram';
import { runPositionMonitoring } from './monitor';
import { fetchCurrentPrice, MarketDataError } from './market_data';

export default {
  // 1. HTTP Request Handler (REST API, Webhook, Dashboard API)
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type, X-Telegram-Bot-Api-Secret-Token',
        },
      });
    }

    // Health check
    if (path === '/health' || path === '/api/health') {
      return new Response(
        JSON.stringify({
          status: 'ok',
          environment: env.ENVIRONMENT || 'production',
          time: new Date().toISOString(),
          platform: 'Cloudflare Workers (V8)',
        }),
        { headers: { 'Content-Type': 'application/json' } }
      );
    }

    // Telegram Webhook Endpoint
    if (path === '/telegram/webhook' && request.method === 'POST') {
      const webhookSecret = env.TELEGRAM_WEBHOOK_SECRET;
      console.info('[Telegram] Webhook secret diagnostic', {
        isString: typeof webhookSecret === 'string',
        isTruthy: Boolean(webhookSecret),
        length: typeof webhookSecret === 'string' ? webhookSecret.length : 0,
      });
      if (!webhookSecret) {
        return new Response('Telegram webhook secret is not configured', { status: 503 });
      }

      const secretHeader = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
      if (secretHeader !== webhookSecret) {
        return new Response('Unauthorized Webhook Request', { status: 403 });
      }

      try {
        const update = await request.json() as any;
        console.info('[Telegram] Webhook update parsed; forwarding to update handler');
        return await handleTelegramUpdate(update, env);
      } catch (err: any) {
        console.error('[Telegram] Webhook update processing failed');
        return new Response(JSON.stringify({ error: err.message }), {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    }

    // Dashboard REST API: Session Token Exchange
    if (path === '/api/auth/token' && request.method === 'POST') {
      try {
        const body = await request.json() as unknown;
        const token = typeof body === 'object' && body !== null && 'token' in body ? body.token : undefined;
        if (typeof token !== 'string' || token.length === 0) {
          return new Response(JSON.stringify({ error: 'Missing token' }), { status: 400 });
        }

        const nowIso = new Date().toISOString();
        const tokenHash = await hashDashboardToken(token);
        const tokenRecord = await env.DB.prepare(
          'UPDATE dashboard_auth_tokens SET is_used = 1 WHERE token_hash = ? AND is_used = 0 AND expires_at > ? RETURNING user_id'
        ).bind(tokenHash, nowIso).first<{ user_id: string }>();

        if (!tokenRecord) {
          return new Response(JSON.stringify({ error: 'Invalid or expired token' }), { status: 401 });
        }

        const sessionId = crypto.randomUUID().replace(/-/g, '') + crypto.randomUUID().replace(/-/g, '');
        const sessionExpiry = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString(); // 7 days

        await env.DB.prepare(
          'INSERT INTO dashboard_sessions (session_id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)'
        ).bind(sessionId, tokenRecord.user_id, nowIso, sessionExpiry).run();

        return new Response(JSON.stringify({ ok: true }), {
          headers: {
            'Content-Type': 'application/json',
            'Set-Cookie': `session_id=${sessionId}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=604800`,
          },
        });
      } catch (error) {
        console.error('Dashboard token exchange failed:', error);
        return new Response(JSON.stringify({ error: 'Unable to exchange dashboard token' }), { status: 500 });
      }
    }

    // Dashboard REST API: Authenticated User Data
    if (path === '/api/user/summary' && request.method === 'GET') {
      const cookie = request.headers.get('Cookie') || '';
      const sessionId = cookie.match(/(?:^|;\s*)session_id=([a-f0-9]{64})(?:;|$)/i)?.[1] || '';

      if (!sessionId) {
        return new Response(JSON.stringify({ error: 'Unauthorized' }), { status: 401 });
      }

      const nowIso = new Date().toISOString();
      const session = await env.DB.prepare(
        'SELECT * FROM dashboard_sessions WHERE session_id = ? AND expires_at > ?'
      ).bind(sessionId, nowIso).first() as any;

      if (!session) {
        return new Response(JSON.stringify({ error: 'Session expired or invalid' }), { status: 401 });
      }

      const userId = session.user_id;

      // Query isolated multi-user tables
      const balance = await env.DB.prepare('SELECT * FROM user_balances WHERE user_id = ?').bind(userId).first();
      const positions = await env.DB.prepare('SELECT * FROM paper_positions WHERE user_id = ? AND status = "OPEN"').bind(userId).all();
      const history = await env.DB.prepare('SELECT * FROM paper_positions WHERE user_id = ? AND status != "OPEN" ORDER BY exit_time DESC LIMIT 50').bind(userId).all();
      const watchlist = await env.DB.prepare('SELECT * FROM user_watchlist WHERE user_id = ? AND is_active = 1').bind(userId).all();

      return new Response(
        JSON.stringify({
          user_id: userId,
          balance: balance || { starting_balance: 10000, current_balance: 10000, realized_pnl: 0 },
          positions: positions.results || [],
          history: history.results || [],
          watchlist: watchlist.results || [],
          timestamp: nowIso,
        }),
        { headers: { 'Content-Type': 'application/json' } }
      );
    }

    if (path === '/api/cron/trigger' && request.method === 'POST') {
      return new Response('Not Found', { status: 404 });
    }

    // Market Price Test
    if (path.startsWith('/api/market/quote/')) {
      const sym = path.replace('/api/market/quote/', '');
      try {
        const quote = await fetchCurrentPrice(sym);
        return new Response(JSON.stringify(quote || { error: 'Not found' }), {
          headers: { 'Content-Type': 'application/json' },
        });
      } catch (error) {
        if (!(error instanceof MarketDataError)) {
          throw error;
        }
        return new Response(JSON.stringify({ error: error.message }), {
          status: 502,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    }

    return env.ASSETS.fetch(request);
  },

  // 2. Cron Trigger Handler (Automated Open Position Monitor)
  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    console.log(`[Cron Trigger] Executing position monitoring at ${new Date(event.scheduledTime).toISOString()}`);
    ctx.waitUntil(runPositionMonitoring(env));
  },
};
