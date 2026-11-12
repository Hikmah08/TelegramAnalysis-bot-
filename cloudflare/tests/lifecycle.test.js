import test from 'node:test';
import assert from 'node:assert/strict';
import { calculatePositionMetrics, evaluateConservativeExit } from '../src/lifecycle.js';

test('Lifecycle - Long Position Profit and Loss', () => {
  const position = {
    position_id: 1,
    user_id: 'user_123',
    symbol: 'BTCUSDT',
    direction: 'BULLISH',
    entry_price: 60000.0,
    stop_loss: 59000.0,
    take_profit: 63000.0,
    position_size: 0.1,
    status: 'OPEN',
    entry_time: '2026-11-04T12:00:00Z',
    exit_time: null,
    exit_price: null,
    exit_reason: null,
    pnl: 0.0,
    fee_paid: 0.0,
    current_price: 60000.0,
    unrealized_pnl: 0.0,
    r_multiple: 0.0,
    target_progress_pct: 0.0,
    last_price_time: null,
    initial_risk: 100.0, // (60000 - 59000) * 0.1 = 100
    last_notified_progress_milestone: 0.0,
    last_notified_r_milestone: 0.0,
  };

  // Test at +1.5R (61500)
  const metrics = calculatePositionMetrics(position, 61500.0);
  assert.equal(metrics.unrealized_pnl, 150.0); // (61500 - 60000) * 0.1
  assert.equal(metrics.r_multiple, 1.5);
  assert.equal(metrics.target_progress_pct, 50.0); // 1500 / 3000 = 50%
  assert.equal(metrics.is_tp_hit, false);
  assert.equal(metrics.is_sl_hit, false);
});

test('Lifecycle - Short Position Profit and Loss', () => {
  const position = {
    position_id: 2,
    user_id: 'user_123',
    symbol: 'ETHUSDT',
    direction: 'BEARISH',
    entry_price: 3000.0,
    stop_loss: 3100.0,
    take_profit: 2700.0,
    position_size: 1.0,
    status: 'OPEN',
    entry_time: '2026-11-04T12:00:00Z',
    exit_time: null,
    exit_price: null,
    exit_reason: null,
    pnl: 0.0,
    fee_paid: 0.0,
    current_price: 3000.0,
    unrealized_pnl: 0.0,
    r_multiple: 0.0,
    target_progress_pct: 0.0,
    last_price_time: null,
    initial_risk: 100.0,
    last_notified_progress_milestone: 0.0,
    last_notified_r_milestone: 0.0,
  };

  // Price drops to 2850 (profit for short)
  const metrics = calculatePositionMetrics(position, 2850.0);
  assert.equal(metrics.unrealized_pnl, 150.0);
  assert.equal(metrics.r_multiple, 1.5);
  assert.equal(metrics.target_progress_pct, 50.0);
  assert.equal(metrics.is_tp_hit, false);
  assert.equal(metrics.is_sl_hit, false);
});

test('Lifecycle - Conservative Exit (SL First Policy)', () => {
  const position = {
    position_id: 3,
    user_id: 'user_123',
    symbol: 'BTCUSDT',
    direction: 'BULLISH',
    entry_price: 60000.0,
    stop_loss: 59000.0,
    take_profit: 63000.0,
    position_size: 0.1,
    status: 'OPEN',
    entry_time: '2026-11-04T12:00:00Z',
    exit_time: null,
    exit_price: null,
    exit_reason: null,
    pnl: 0.0,
    fee_paid: 0.0,
    current_price: 60000.0,
    unrealized_pnl: 0.0,
    r_multiple: 0.0,
    target_progress_pct: 0.0,
    last_price_time: null,
    initial_risk: 100.0,
    last_notified_progress_milestone: 0.0,
    last_notified_r_milestone: 0.0,
  };

  // Ambiguous candle where High reaches 63500 (TP) AND Low reaches 58500 (SL)
  const metrics = calculatePositionMetrics(position, 60000.0, 63500.0, 58500.0);
  assert.equal(metrics.is_tp_hit, true);
  assert.equal(metrics.is_sl_hit, true);

  const exit = evaluateConservativeExit(position, metrics);
  // Must execute Stop Loss first per conservative policy
  assert.equal(exit.shouldClose, true);
  assert.equal(exit.exitReason, 'STOP_LOSS');
  assert.equal(exit.exitPrice, 59000.0);
  assert.equal(exit.realizedPnl, -100.0);
});
