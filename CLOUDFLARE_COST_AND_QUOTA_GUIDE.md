# CLOUDFLARE FREE-TIER COST & QUOTA ASSESSMENT

## 1. Cloudflare Free-Tier Quota Comparison

| Service | Free Tier Monthly Allowance | Bot Expected Monthly Consumption | Headroom / Safety Margin |
|---|---|---|---|
| **Workers** | 100,000 requests / day (~3,000,000 / mo) | ~43,200 Cron runs + ~5,000 Webhook events = **~48,200 reqs / mo** | **>98% Free Headroom** |
| **Worker CPU Time** | 10 ms per invocation | Lightweight price fetch & formula: ~2-4 ms | Within Limits |
| **D1 Database Storage** | 5 GB storage | SQLite database with 10k trades: **< 50 MB** | **>99% Free Headroom** |
| **D1 Rows Read** | 5,000,000 rows / day | ~50,000 rows / day | **99% Free Headroom** |
| **D1 Rows Written** | 100,000 rows / day | ~2,000 rows / day | **98% Free Headroom** |
| **R2 Storage** | 10 GB storage | Backtest CSVs: **~500 MB** | **95% Free Headroom** |
| **R2 Class A Ops (Write)**| 1,000,000 ops / month | ~500 ops / month | **>99% Free Headroom** |
| **R2 Class B Ops (Read)** | 10,000,000 ops / month | ~5,000 ops / month | **>99% Free Headroom** |
| **Pages (Hosting)** | Unlimited requests, 500 builds / mo | 1-5 deployments / mo | **Unlimited** |
| **Total Monthly Cost** | **$0.00 / month** | **$0.00 / month** | **100% Free** |

---

## 2. Cost Guardrails & Protection

1. **Usage-Based Spikes**:
   - Cloudflare Free Tier stops accepting requests when daily Worker limits are exceeded rather than silently charging credit cards.
2. **Cron Optimization**:
   - Setting Cron Triggers to `*/2 * * * *` (every 2 minutes) halves invocation count to ~21,600 / month, ensuring near zero consumption of daily quotas.
3. **No Domain Purchase Required**:
   - Free `*.workers.dev` and `*.pages.dev` domains provide instant TLS/HTTPS without purchasing a custom domain.
