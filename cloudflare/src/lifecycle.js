export function calculatePositionMetrics(
  position,
  currentPrice,
  candleHigh,
  candleLow
) {
  const isLong = position.direction.toUpperCase() === 'BULLISH';
  const entry = position.entry_price;
  const sl = position.stop_loss;
  const tp = position.take_profit;
  const size = position.position_size;

  let unrealizedPnl = 0.0;
  if (isLong) {
    unrealizedPnl = (currentPrice - entry) * size;
  } else {
    unrealizedPnl = (entry - currentPrice) * size;
  }

  const initialRisk = position.initial_risk > 0 
    ? position.initial_risk 
    : Math.abs(entry - sl) * size;

  let rMultiple = 0.0;
  if (initialRisk > 0) {
    rMultiple = unrealizedPnl / initialRisk;
  }

  let targetProgressPct = 0.0;
  if (isLong) {
    const targetDistance = tp - entry;
    if (Math.abs(targetDistance) > 1e-9) {
      targetProgressPct = ((currentPrice - entry) / targetDistance) * 100.0;
    }
  } else {
    const targetDistance = entry - tp;
    if (Math.abs(targetDistance) > 1e-9) {
      targetProgressPct = ((entry - currentPrice) / targetDistance) * 100.0;
    }
  }

  const evalHigh = candleHigh !== undefined ? Math.max(candleHigh, currentPrice) : currentPrice;
  const evalLow = candleLow !== undefined ? Math.min(candleLow, currentPrice) : currentPrice;

  let isTpHit = false;
  let isSlHit = false;

  if (isLong) {
    if (evalHigh >= tp) isTpHit = true;
    if (evalLow <= sl) isSlHit = true;
  } else {
    if (evalLow <= tp) isTpHit = true;
    if (evalHigh >= sl) isSlHit = true;
  }

  return {
    current_price: currentPrice,
    unrealized_pnl: Number(unrealizedPnl.toFixed(4)),
    r_multiple: Number(rMultiple.toFixed(2)),
    target_progress_pct: Number(targetProgressPct.toFixed(2)),
    is_tp_hit: isTpHit,
    is_sl_hit: isSlHit,
  };
}

export function evaluateConservativeExit(
  position,
  metrics
) {
  if (metrics.is_sl_hit) {
    const isLong = position.direction.toUpperCase() === 'BULLISH';
    const exitPrice = position.stop_loss;
    const realizedPnl = isLong 
      ? (exitPrice - position.entry_price) * position.position_size 
      : (position.entry_price - exitPrice) * position.position_size;
    return {
      shouldClose: true,
      exitPrice,
      exitReason: 'STOP_LOSS',
      realizedPnl: Number(realizedPnl.toFixed(4)),
    };
  }

  if (metrics.is_tp_hit) {
    const isLong = position.direction.toUpperCase() === 'BULLISH';
    const exitPrice = position.take_profit;
    const realizedPnl = isLong 
      ? (exitPrice - position.entry_price) * position.position_size 
      : (position.entry_price - exitPrice) * position.position_size;
    return {
      shouldClose: true,
      exitPrice,
      exitReason: 'TAKE_PROFIT',
      realizedPnl: Number(realizedPnl.toFixed(4)),
    };
  }

  return {
    shouldClose: false,
    exitPrice: 0,
    exitReason: null,
    realizedPnl: 0,
  };
}
