import { Env, PaperPosition } from './types';
import { fetchCurrentPrice } from './market_data';
import { calculatePositionMetrics, evaluateConservativeExit } from './lifecycle';
import { sendTelegramMessage } from './telegram';

export async function runPositionMonitoring(env: Env): Promise<{
  monitoredCount: number;
  closedCount: number;
  errors: string[];
}> {
  const errors: string[] = [];
  let closedCount = 0;

  // 1. Fetch all OPEN positions across all users
  const openPositions = await env.DB.prepare(
    'SELECT * FROM paper_positions WHERE status = "OPEN"'
  ).all<PaperPosition>();

  const positions = openPositions.results || [];
  if (positions.length === 0) {
    return { monitoredCount: 0, closedCount: 0, errors: [] };
  }

  // 2. Group unique symbols to minimize external price requests
  const symbolMap = new Map<string, any>();
  for (const pos of positions) {
    if (!symbolMap.has(pos.symbol)) {
      const quote = await fetchCurrentPrice(pos.symbol);
      symbolMap.set(pos.symbol, quote);
    }
  }

  const nowIso = new Date().toISOString();

  // 3. Process each open position
  for (const pos of positions) {
    try {
      const quote = symbolMap.get(pos.symbol);
      if (!quote || quote.price === undefined || isNaN(quote.price)) {
        errors.push(`Could not fetch price for ${pos.symbol}`);
        continue;
      }

      const metrics = calculatePositionMetrics(pos, quote.price, quote.high, quote.low);
      const exitEval = evaluateConservativeExit(pos, metrics);

      if (exitEval.shouldClose) {
        // Atomic closure & balance update
        const exitStatus = exitEval.exitReason === 'STOP_LOSS' ? 'CLOSED_SL' : 'CLOSED_TP';

        await env.DB.batch([
          env.DB.prepare(
            `UPDATE paper_positions 
             SET status = ?, exit_time = ?, exit_price = ?, exit_reason = ?, pnl = ?, current_price = ?, last_price_time = ?
             WHERE position_id = ?`
          ).bind(exitStatus, nowIso, exitEval.exitPrice, exitEval.exitReason, exitEval.realizedPnl, quote.price, nowIso, pos.position_id),

          env.DB.prepare(
            `UPDATE user_balances 
             SET current_balance = current_balance + ?, realized_pnl = realized_pnl + ?,
                 total_trades = total_trades + 1,
                 winning_trades = winning_trades + (CASE WHEN ? > 0 THEN 1 ELSE 0 END),
                 losing_trades = losing_trades + (CASE WHEN ? <= 0 THEN 1 ELSE 0 END),
                 updated_at = ?
             WHERE user_id = ?`
          ).bind(exitEval.realizedPnl, exitEval.realizedPnl, exitEval.realizedPnl, exitEval.realizedPnl, nowIso, pos.user_id),

          env.DB.prepare(
            `INSERT INTO balance_transactions (user_id, position_id, amount, balance_after, transaction_type, description, created_at)
             VALUES (?, ?, ?, (SELECT current_balance FROM user_balances WHERE user_id = ?), 'POSITION_PNL', ?, ?)`
          ).bind(pos.user_id, pos.position_id, exitEval.realizedPnl, pos.user_id, `Auto closure: ${exitEval.exitReason}`, nowIso),

          // Enqueue outbox notification
          env.DB.prepare(
            `INSERT INTO outbox_notifications (user_id, chat_id, event_type, payload, status, created_at)
             VALUES (?, (SELECT chat_id FROM users WHERE user_id = ?), 'TRADE_CLOSED', ?, 'PENDING', ?)`
          ).bind(
            pos.user_id,
            pos.user_id,
            JSON.stringify({
              position_id: pos.position_id,
              symbol: pos.symbol,
              direction: pos.direction,
              exit_reason: exitEval.exitReason,
              exit_price: exitEval.exitPrice,
              pnl: exitEval.realizedPnl,
            }),
            nowIso
          ),
        ]);

        closedCount++;
      } else {
        // Simply update mark price and progress metrics
        await env.DB.prepare(
          `UPDATE paper_positions 
           SET current_price = ?, unrealized_pnl = ?, r_multiple = ?, target_progress_pct = ?, last_price_time = ?
           WHERE position_id = ?`
        ).bind(metrics.current_price, metrics.unrealized_pnl, metrics.r_multiple, metrics.target_progress_pct, nowIso, pos.position_id).run();
      }
    } catch (err: any) {
      errors.push(`Error processing position ${pos.position_id}: ${err.message}`);
    }
  }

  // 4. Dispatch pending notifications from outbox
  if (env.TELEGRAM_BOT_TOKEN) {
    try {
      const outbox = await env.DB.prepare(
        'SELECT * FROM outbox_notifications WHERE status = "PENDING" LIMIT 20'
      ).all();

      for (const item of (outbox.results || []) as any[]) {
        try {
          const payload = JSON.parse(item.payload);
          const pnlText = `${payload.pnl >= 0 ? '+' : ''}$${Number(payload.pnl).toFixed(2)}`;
          const emoji = payload.exit_reason === 'TAKE_PROFIT' ? '🎯' : '🛑';
          const msg = `${emoji} <b>Position #${payload.position_id} Closed (${payload.exit_reason})</b>\n\n` +
            `• Symbol: <code>${payload.symbol}</code>\n` +
            `• Direction: <b>${payload.direction}</b>\n` +
            `• Exit Price: <b>${payload.exit_price}</b>\n` +
            `• P&L: <b>${pnlText}</b>`;

          const success = await sendTelegramMessage(env.TELEGRAM_BOT_TOKEN, item.chat_id, msg);
          if (success) {
            await env.DB.prepare(
              'UPDATE outbox_notifications SET status = "SENT", sent_at = ? WHERE id = ?'
            ).bind(new Date().toISOString(), item.id).run();
          }
        } catch (e) {
          console.error(`Outbox item ${item.id} error:`, e);
        }
      }
    } catch (err: any) {
      errors.push(`Outbox dispatch failed: ${err.message}`);
    }
  }

  return {
    monitoredCount: positions.length,
    closedCount,
    errors,
  };
}
