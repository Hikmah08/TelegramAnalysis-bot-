# CLOUDFLARE PRODUCTION DEPLOYMENT GUIDE

Step-by-step instructions for deploying the ICT/SMC Multi-User Trading Platform to Cloudflare Free Tier using Wrangler and Cloudflare Dashboard.

---

## 1. Prerequisites & Account Setup

1. Sign up for a free Cloudflare account at [dash.cloudflare.com](https://dash.cloudflare.com).
2. Install Node.js (v18+) and Wrangler CLI:
   ```bash
   npm install -g wrangler
   ```
3. Authenticate Wrangler with your Cloudflare account:
   ```bash
   wrangler login
   ```

---

## 2. Cloudflare D1 Database Setup

1. Create the D1 serverless SQLite database:
   ```bash
   wrangler d1 create trading-bot-db
   ```
   *Note the `database_name` and `database_id` returned in the terminal output.*

2. Apply the schema migration:
   ```bash
   wrangler d1 execute trading-bot-db --file=./cloudflare/schema.sql
   ```

---

## 3. Cloudflare R2 Storage (Historical Datasets)

1. Create a free R2 bucket for backtest CSVs:
   ```bash
   wrangler r2 bucket create trading-bot-datasets
   ```

---

## 4. Configure Secrets

Set production secrets securely into Cloudflare Workers (do not commit secrets to Git):
```bash
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_ADMIN_IDS
wrangler secret put TELEGRAM_WEBHOOK_SECRET
wrangler secret put DASHBOARD_SECRET_KEY
```

---

## 5. Deploy Cloudflare Worker

1. Navigate to the `cloudflare` worker directory:
   ```bash
   cd cloudflare
   wrangler deploy
   ```
2. Your worker will be live at: `https://trading-bot-worker.<your-subdomain>.workers.dev`

---

## 6. Configure Telegram Webhook

Point Telegram BotFather webhooks to your Cloudflare Worker URL:
```bash
curl -X POST "https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/setWebhook" \
     -H "Content-Type: application/json" \
     -d '{
       "url": "https://trading-bot-worker.<your-subdomain>.workers.dev/telegram/webhook",
       "secret_token": "<YOUR_TELEGRAM_WEBHOOK_SECRET>",
       "allowed_updates": ["message", "callback_query"]
     }'
```

Verify webhook status:
```bash
curl "https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

---

## 7. Cloudflare Pages Dashboard Deployment

1. Deploy the dashboard to Cloudflare Pages:
   ```bash
   cd cloudflare/dashboard
   npx wrangler pages deploy dist --project-name=trading-bot-dashboard
   ```
2. The dashboard will be accessible globally with free automatic SSL at:
   `https://trading-bot-dashboard.pages.dev`

---

## 8. Rollback and Disaster Recovery

If you ever wish to roll back to the self-hosted Linux VPS / Docker setup:
1. Export D1 data:
   ```bash
   wrangler d1 export trading-bot-db --output=./backup_recovery.sql
   ```
2. Convert back to local SQLite:
   ```bash
   sqlite3 trading_bot.db < ./backup_recovery.sql
   ```
3. Remove Telegram webhook to resume long polling:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/deleteWebhook"
   ```
4. Start Docker Compose:
   ```bash
   docker compose up -d
   ```
