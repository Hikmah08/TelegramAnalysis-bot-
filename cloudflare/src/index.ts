import { Env } from './types';
import { handleTelegramUpdate, hashDashboardToken, registerTelegramCommands } from './telegram';
import { runPositionMonitoring } from './monitor';
import { fetchCurrentPrice, MarketDataError } from './market_data';
import { handleDashboardRequest } from './dashboard.js';
import { handleAdminRequest } from './admin.js';

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

    const adminResponse = await handleAdminRequest(request, env, hashDashboardToken);
    if (adminResponse) return adminResponse;

    const dashboardResponse = await handleDashboardRequest(request, env, hashDashboardToken);
    if (dashboardResponse) return dashboardResponse;

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
        if (env.TELEGRAM_BOT_TOKEN) {
          ctx.waitUntil(registerTelegramCommands(
            env.TELEGRAM_BOT_TOKEN,
            env.TELEGRAM_ADMIN_IDS || ''
          ));
        }
        return await handleTelegramUpdate(update, env);
      } catch (err: any) {
        console.error('[Telegram] Webhook update processing failed');
        return new Response(JSON.stringify({ error: err.message }), {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    }

    if (path === '/api/cron/trigger' && request.method === 'POST') {
      return new Response('Not Found', { status: 404 });
    }

    // Market Price Test
    if (path.startsWith('/api/market/quote/')) {
      const sym = path.replace('/api/market/quote/', '');
      try {
        const quote = await fetchCurrentPrice(sym, env.TWELVE_DATA_API_KEY);
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
