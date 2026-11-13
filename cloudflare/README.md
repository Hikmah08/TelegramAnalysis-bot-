# Cloudflare Worker & Pages Deployment Guide

This directory houses the serverless cloud deployment of the ICT/SMC Trading Analysis Platform for Cloudflare Free Tier.

## Architecture

- **Compute**: Cloudflare Workers (TypeScript / V8)
  - `src/index.ts`: Worker entry point, REST API, health check, session verification.
  - `src/telegram.ts`: Telegram webhook endpoint (`POST /telegram/webhook`) handling user registration, access checks, minimal command activity, `/start`, `/help`, `/menu`, balance, positions, history, interactive watchlist, live `/price`, dashboard login, full multi-timeframe ICT/SMC analysis (`/analyze`), and interactive trade management.
  - `src/admin.js`: Separately authenticated `/admin` operations console and privacy-limited admin APIs. Admin API access requires an active `ADMIN` user whose Telegram ID is also present in `TELEGRAM_ADMIN_IDS`.
  - `src/monitor.ts`: Scheduled position monitoring executed via Cron Trigger (`* * * * *`).
  - `src/lifecycle.ts`: Paper trading P&L, R-multiple, target progress, and conservative SL/TP exit evaluations.
  - `src/strategy.ts`: Port of ICT/SMC swing identification, liquidity sweep detection, and FVG detection.
  - `src/market_data.ts`: Live price adapters for Binance, Coinbase, Yahoo Finance, and Twelve Data spot metals.
  - `src/ict_analysis.js`: Multi-timeframe candle pipeline (Binance, Coinbase Exchange, then Kraken for existing crypto/forex instruments; Twelve Data for catalogued XAU/USD and XAG/USD spot metals) for 4H, 1H, 15M, 5M, and 1M analysis. Kraken symbols use exchange-specific names (for example BTCUSDT maps to XBTUSDT); provider candles are validated, closed, ordered, and normalized before analysis. XAU/USDT is not substituted with XAU/USD and is reported as unsupported because Twelve Data's symbol search does not list that pair.
- **Database**: Cloudflare D1 (Serverless SQLite)
  - Managed via versioned migrations in `migrations/` and `schema.sql`.
- **Frontend**: Cloudflare Workers static assets
  - Mobile & desktop personal trading dashboard in `public/index.html`.
  - Separate operational admin console in `public/admin.html`; it exposes user metadata and usage only, never trading records.
- **Admin audit data**: `migrations/0002_admin_control.sql` adds the minimal command/user activity feed and administrator action audit log. It does not alter existing trading tables.

---

## Step-by-Step Deployment Instructions

### 1. Prerequisites
Ensure Node.js (v18+) is installed. Install Wrangler globally or use local `npx`:
```bash
npm install -g wrangler
```

### 2. Login to Cloudflare
Authenticate Wrangler with your Cloudflare account:
```bash
wrangler login
```

### 3. Create Cloudflare D1 Database
Create the production serverless SQLite database:
```bash
wrangler d1 create trading-bot-db
```
*Copy the `database_id` output by Wrangler and paste it into `wrangler.toml` under `[[d1_databases]]`.*

### 4. Apply Database Migrations
For a new database, apply the initial schema:
```bash
wrangler d1 execute trading-bot-db --remote --file=./schema.sql
```
For an existing database, apply the versioned admin migration without resetting user data:
```bash
wrangler d1 migrations apply trading-bot-db --remote
```

### 5. Set Production Secrets
Safely configure secret tokens without storing them in Git:
```bash
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_ADMIN_IDS
wrangler secret put TELEGRAM_WEBHOOK_SECRET
wrangler secret put DASHBOARD_SECRET_KEY
wrangler secret put TWELVE_DATA_API_KEY
```
`TELEGRAM_WEBHOOK_SECRET` is required. Keep its value available for the manual
webhook registration step below; the Worker rejects webhook requests when the
secret is missing or does not match.
Configure `TWELVE_DATA_API_KEY` as a Wrangler secret to enable verified
historical and latest spot prices for XAU/USD and XAG/USD. The bot does not
silently convert XAU/USDT to XAU/USD.

### 6. Run Automated Tests Locally
Verify the TypeScript and JavaScript modules pass all unit tests:
```bash
npm test
```

### 7. Deploy Worker & Dashboard
Deploy the worker and public dashboard assets to Cloudflare:
```bash
wrangler deploy
```
*Note your live worker URL: `https://trading-bot-worker.<subdomain>.workers.dev`.*

### 8. Set Up Telegram Webhook
After deployment, manually point Telegram's webhook to your Worker URL. This
project does not register the webhook automatically:
```bash
curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://trading-bot-worker.<subdomain>.workers.dev/telegram/webhook",
    "secret_token": "<YOUR_TELEGRAM_WEBHOOK_SECRET>",
    "allowed_updates": ["message", "callback_query"]
  }'
```

### 9. Verify Deployment
- Health check: `curl https://trading-bot-worker.<subdomain>.workers.dev/health`
- Telegram bot: Send `/start` in your bot chat.
- Dashboard: Send `/dashboard` in Telegram and open the returned login link.
- Admin console: Send `/admin` from an ID listed in `TELEGRAM_ADMIN_IDS` and open its one-time link. Admin sessions and APIs also verify the account's active `ADMIN` role on the server.
