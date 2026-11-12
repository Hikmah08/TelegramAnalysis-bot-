import { Candle } from './types';

export enum Direction {
  BULLISH = 'BULLISH',
  BEARISH = 'BEARISH',
}

export enum SwingType {
  SWING_HIGH = 'SWING_HIGH',
  SWING_LOW = 'SWING_LOW',
}

export interface SwingPoint {
  swingType: SwingType;
  price: number;
  candleIndex: number;
  timestamp: number;
  confirmedAtIndex: number;
}

export interface LiquiditySweep {
  direction: Direction;
  sweptLevel: number;
  penetrationPrice: number;
  candleIndex: number;
  timestamp: number;
}

export interface FairValueGap {
  direction: Direction;
  topPrice: number;
  bottomPrice: number;
  consequentEncroachment: number;
  candleIndex: number;
  timestamp: number;
  gapSize: number;
}

/**
 * Identify confirmed swing highs and lows with N-bars left and right.
 * Strict chronological confirmation: Confirmed only after right_bars have fully closed.
 */
export function identifySwings(
  candles: Candle[],
  leftBars: number = 3,
  rightBars: number = 3
): SwingPoint[] {
  const swings: SwingPoint[] = [];
  const n = candles.length;

  for (let i = leftBars; i < n - rightBars; i++) {
    const currentHigh = candles[i].high;
    const currentLow = candles[i].low;

    // Check Swing High
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

    // Check Swing Low
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

/**
 * Checks if a candle wicks through a confirmed swing point but closes back inside.
 */
export function checkCandleForSweeps(
  candleIdx: number,
  candle: Candle,
  activeSwings: SwingPoint[]
): LiquiditySweep[] {
  const sweeps: LiquiditySweep[] = [];

  for (const swing of activeSwings) {
    if (candleIdx <= swing.confirmedAtIndex) {
      continue;
    }

    if (swing.swingType === SwingType.SWING_LOW) {
      // Bullish sweep: Low penetrates below, but close is above or equal
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
      // Bearish sweep: High penetrates above, but close is below or equal
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

/**
 * Checks for 3-candle Fair Value Gap.
 * Candle 1 = idx - 2, Candle 2 = idx - 1, Candle 3 = idx.
 */
export function checkCandleForFvg(
  candleIdx: number,
  candles: Candle[],
  minFvgSizePct: number = 0.0005
): FairValueGap | null {
  if (candleIdx < 2) return null;

  const c1 = candles[candleIdx - 2];
  const c3 = candles[candleIdx];

  // Bullish FVG: c3 low > c1 high
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

  // Bearish FVG: c1 low > c3 high
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
