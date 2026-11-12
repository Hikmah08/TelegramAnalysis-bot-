export const Direction = {
  BULLISH: 'BULLISH',
  BEARISH: 'BEARISH',
};

export const SwingType = {
  SWING_HIGH: 'SWING_HIGH',
  SWING_LOW: 'SWING_LOW',
};

export function identifySwings(candles, leftBars = 3, rightBars = 3) {
  const swings = [];
  const n = candles.length;

  for (let i = leftBars; i < n - rightBars; i++) {
    const currentHigh = candles[i].high;
    const currentLow = candles[i].low;

    let isHigh = true;
    for (let j = i - leftBars; j <= i + rightBars; j++) {
      if (j !== i && candles[j].high >= currentHigh) {
        isHigh = false;
        break;
      }
    }

    if (isHigh) {
      swings.push({
        swingType: SwingType.SWING_HIGH,
        price: currentHigh,
        candleIndex: i,
        timestamp: candles[i].timestamp,
        confirmedAtIndex: i + rightBars,
      });
    }

    let isLow = true;
    for (let j = i - leftBars; j <= i + rightBars; j++) {
      if (j !== i && candles[j].low <= currentLow) {
        isLow = false;
        break;
      }
    }

    if (isLow) {
      swings.push({
        swingType: SwingType.SWING_LOW,
        price: currentLow,
        candleIndex: i,
        timestamp: candles[i].timestamp,
        confirmedAtIndex: i + rightBars,
      });
    }
  }

  return swings;
}

export function checkCandleForSweeps(candleIdx, candle, activeSwings) {
  const sweeps = [];

  for (const swing of activeSwings) {
    if (candleIdx <= swing.confirmedAtIndex) {
      continue;
    }

    if (swing.swingType === SwingType.SWING_LOW) {
      if (candle.low < swing.price && candle.close >= swing.price) {
        sweeps.push({
          direction: Direction.BULLISH,
          sweptLevel: swing.price,
          penetrationPrice: candle.low,
          candleIndex: candleIdx,
          timestamp: candle.timestamp,
        });
      }
    } else if (swing.swingType === SwingType.SWING_HIGH) {
      if (candle.high > swing.price && candle.close <= swing.price) {
        sweeps.push({
          direction: Direction.BEARISH,
          sweptLevel: swing.price,
          penetrationPrice: candle.high,
          candleIndex: candleIdx,
          timestamp: candle.timestamp,
        });
      }
    }
  }

  return sweeps;
}

export function checkCandleForFvg(candleIdx, candles, minFvgSizePct = 0.0005) {
  if (candleIdx < 2) return null;

  const c1 = candles[candleIdx - 2];
  const c3 = candles[candleIdx];

  if (c3.low > c1.high) {
    const gapSize = c3.low - c1.high;
    const gapPct = gapSize / c1.high;
    if (gapPct >= minFvgSizePct) {
      return {
        direction: Direction.BULLISH,
        topPrice: c3.low,
        bottomPrice: c1.high,
        consequentEncroachment: (c3.low + c1.high) / 2.0,
        candleIndex: candleIdx,
        timestamp: c3.timestamp,
        gapSize,
      };
    }
  }

  if (c1.low > c3.high) {
    const gapSize = c1.low - c3.high;
    const gapPct = gapSize / c1.low;
    if (gapPct >= minFvgSizePct) {
      return {
        direction: Direction.BEARISH,
        topPrice: c1.low,
        bottomPrice: c3.high,
        consequentEncroachment: (c1.low + c3.high) / 2.0,
        candleIndex: candleIdx,
        timestamp: c3.timestamp,
        gapSize,
      };
    }
  }

  return null;
}
