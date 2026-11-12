import test from 'node:test';
import assert from 'node:assert/strict';
import { identifySwings, checkCandleForSweeps, checkCandleForFvg, SwingType, Direction } from '../src/strategy.js';

test('Strategy - Chronological Swing High and Low Identification', () => {
  // Construct 7 candles with peak at candle 3
  const candles = [
    { timestamp: 1000, open: 10, high: 12, low: 9, close: 11, volume: 100 },
    { timestamp: 2000, open: 11, high: 14, low: 10, close: 13, volume: 100 },
    { timestamp: 3000, open: 13, high: 16, low: 12, close: 15, volume: 100 },
    { timestamp: 4000, open: 15, high: 20, low: 14, close: 18, volume: 100 }, // Peak Swing High at idx 3
    { timestamp: 5000, open: 18, high: 15, low: 13, close: 14, volume: 100 },
    { timestamp: 6000, open: 14, high: 14, low: 11, close: 12, volume: 100 },
    { timestamp: 7000, open: 12, high: 13, low: 10, close: 11, volume: 100 },
  ];

  const swings = identifySwings(candles, 3, 3);
  assert.equal(swings.length, 1);
  assert.equal(swings[0].swingType, SwingType.SWING_HIGH);
  assert.equal(swings[0].price, 20);
  assert.equal(swings[0].candleIndex, 3);
  // Confirmed only after 3 right bars closed (at candleIndex 6)
  assert.equal(swings[0].confirmedAtIndex, 6);
});

test('Strategy - Bullish Liquidity Sweep (Wick Below, Close Inside)', () => {
  const swingLow = {
    swingType: SwingType.SWING_LOW,
    price: 100.0,
    candleIndex: 2,
    timestamp: 2000,
    confirmedAtIndex: 5,
  };

  // Candle at index 6 wicks to 95 but closes at 102
  const candle = { timestamp: 6000, open: 105, high: 106, low: 95, close: 102, volume: 200 };
  const sweeps = checkCandleForSweeps(6, candle, [swingLow]);

  assert.equal(sweeps.length, 1);
  assert.equal(sweeps[0].direction, Direction.BULLISH);
  assert.equal(sweeps[0].sweptLevel, 100.0);
  assert.equal(sweeps[0].penetrationPrice, 95.0);

  // Candle at index 7 breaks and closes below (Low 90, Close 94) -> NOT a sweep
  const closeBelowCandle = { timestamp: 7000, open: 100, high: 101, low: 90, close: 94, volume: 200 };
  const noSweeps = checkCandleForSweeps(7, closeBelowCandle, [swingLow]);
  assert.equal(noSweeps.length, 0);
});

test('Strategy - 3-Candle Fair Value Gap Detection', () => {
  // Bullish FVG: Candle 1 High < Candle 3 Low
  const candles = [
    { timestamp: 1000, open: 100, high: 102, low: 98, close: 101, volume: 100 }, // c1 high = 102
    { timestamp: 2000, open: 102, high: 115, low: 102, close: 114, volume: 500 }, // c2 displacement
    { timestamp: 3000, open: 114, high: 120, low: 106, close: 118, volume: 200 }, // c3 low = 106
  ];

  const fvg = checkCandleForFvg(2, candles, 0.0005);
  assert.notEqual(fvg, null);
  assert.equal(fvg?.direction, Direction.BULLISH);
  assert.equal(fvg?.bottomPrice, 102);
  assert.equal(fvg?.topPrice, 106);
  assert.equal(fvg?.consequentEncroachment, 104);
  assert.equal(fvg?.gapSize, 4);
});
