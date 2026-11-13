import test from 'node:test';
import assert from 'node:assert/strict';
import {
  analyzeIctCandles,
  fetchHistoricalCandles,
  fetchIctAnalysis,
  formatIctOpportunity,
} from '../src/ict_analysis.js';

function sequence(start) {
  const rows = [
    [104, 99, 102],
    [103, 100, 101],
    [105, 98, 102],
    [103, 100, 101],
    [102, 101, 101],
    [100, 97, 99],
    [107, 102, 106],
    [108, 102, 104],
    [105, 101, 103],
  ];
  return rows.map(([high, low, close], index) => ({
    timestamp: start + index * 60_000,
    open: close,
    high,
    low,
    close,
    volume: 10,
    closeTime: start + (index + 1) * 60_000 - 1,
  }));
}

function qualifiedTimeframes() {
  const hourly = Array.from({ length: 9 }, (_, index) => ({
    timestamp: 1_700_000_000_000 + index * 3_600_000,
    open: 100,
    high: index === 0 ? 98 : 101,
    low: index === 0 ? 97 : index === 1 ? 98.5 : 99,
    close: 100,
    volume: 10,
    closeTime: 1_700_000_000_000 + (index + 1) * 3_600_000 - 1,
  }));
  return {
    '4h': sequence(1_700_000_000_000),
    '1h': hourly,
    '15m': sequence(1_700_100_000_000),
    '5m': sequence(1_700_200_000_000),
    '1m': sequence(1_700_300_000_000),
  };
}

test('ICT analysis - Requires 4H bias, 1H AOI, 15M setup, 5M confirmation and 1M refinement', () => {
  const result = analyzeIctCandles(qualifiedTimeframes());
  assert.equal(result.opportunities.length, 1);
  const opportunity = result.opportunities[0];
  assert.equal(opportunity.direction, 'BULLISH');
  assert.ok(opportunity.setup15mTimestamp < opportunity.confirmation5mTimestamp);
  assert.ok(opportunity.confirmation5mTimestamp < opportunity.refinement1mTimestamp);
  assert.equal(
    opportunity.takeProfit,
    opportunity.entryPrice + Math.abs(opportunity.entryPrice - opportunity.stopLoss) * 4
  );
  assert.ok(opportunity.auditTrail.some(note => note.startsWith('4H directional bias:')));
  assert.ok(opportunity.auditTrail.some(note => note.startsWith('1H AOI:')));
  assert.ok(opportunity.auditTrail.some(note => note.includes('15M setup:')));
  assert.ok(opportunity.auditTrail.some(note => note.includes('5M confirmation:')));
  assert.ok(opportunity.auditTrail.some(note => note.includes('1M refinement:')));
});

test('ICT analysis - Returns no opportunity when the confluence pipeline is not satisfied', () => {
  const flat = sequence(1_700_000_000_000).map(candle => ({
    ...candle,
    high: 101,
    low: 99,
    close: 100,
    open: 100,
  }));
  const result = analyzeIctCandles({
    '4h': flat,
    '1h': flat,
    '15m': flat,
    '5m': flat,
    '1m': flat,
  });
  assert.deepEqual(result.opportunities, []);
});

test('ICT alert formatter - Includes risk, expiry and the required confluence hierarchy', () => {
  const opportunity = analyzeIctCandles(qualifiedTimeframes()).opportunities[0];
  const message = formatIctOpportunity('BTCUSDT', opportunity);
  assert.match(message, /🎯 <b>NEW ICT\/SMC TRADE OPPORTUNITY<\/b>/);
  assert.match(message, /Entry Price/);
  assert.match(message, /Take Profit \(4:1 R:R\)/);
  assert.match(message, /Risk-to-Reward Ratio:<\/b> 4\.0:1/);
  assert.match(message, /Setup Timeframe:<\/b> Confirmed 5M \/ Refined 1M/);
  assert.match(message, /Expiry \/ Invalidation/);
  assert.match(message, /1\. 4H Bias[\s\S]*2\. 1H AOI[\s\S]*3\. 15M Setup[\s\S]*4\. 5M Confirmation[\s\S]*5\. 1M Refinement/);
});

test('ICT candle retrieval - Maps BTCUSDT and ETHUSDT to Kraken symbols across all five timeframes', async () => {
  const originalFetch = globalThis.fetch;
  const krakenRequests = [];
  globalThis.fetch = async url => {
    if (url.startsWith('https://api.binance.com/')) {
      return new Response('Forbidden', { status: 403, headers: { 'Content-Type': 'text/plain' } });
    }
    if (url.startsWith('https://api.exchange.coinbase.com/')) {
      return Response.json({ message: 'product temporarily unavailable' }, { status: 400 });
    }
    krakenRequests.push(url);
    const requestUrl = new URL(url);
    const minutes = Number(requestUrl.searchParams.get('interval'));
    const intervalMs = minutes * 60_000;
    const currentBucket = Math.floor(Date.now() / intervalMs) * intervalMs;
    const rows = Array.from({ length: 10 }, (_, index) => [
      (currentBucket - (10 - index) * intervalMs) / 1000,
      '100',
      '101',
      '99',
      '100',
      '100',
      '10',
      20,
    ]);
    return Response.json({ error: [], result: { [requestUrl.searchParams.get('pair')]: rows, last: 1 } });
  };

  try {
    for (const [symbol, pair] of [['BTCUSDT', 'XBTUSDT'], ['ETHUSDT', 'ETHUSDT']]) {
      krakenRequests.length = 0;
      const results = await Promise.all(
        ['1m', '5m', '15m', '1h', '4h'].map(interval => fetchHistoricalCandles(symbol, interval))
      );
      assert.equal(krakenRequests.length, 5);
      assert.deepEqual(
        krakenRequests.map(url => new URL(url).searchParams.get('interval')).sort((a, b) => Number(a) - Number(b)),
        ['1', '5', '15', '60', '240']
      );
      assert.ok(krakenRequests.every(url => new URL(url).searchParams.get('pair') === pair));
      for (const candles of results) {
        assert.ok(candles.length >= 9);
        assert.deepEqual(Object.keys(candles[0]), [
          'timestamp', 'open', 'high', 'low', 'close', 'volume',
        ]);
        assert.ok(candles.every((candle, index) => (
          index === 0 || candle.timestamp > candles[index - 1].timestamp
        )));
      }
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('ICT candle retrieval - Coinbase fallback normalizes reverse-ordered candle rows', async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async url => {
    requests.push(url);
    if (url.startsWith('https://api.binance.com/')) {
      return new Response('Forbidden', { status: 403, headers: { 'Content-Type': 'text/plain' } });
    }
    const intervalMs = Number(new URL(url).searchParams.get('granularity')) * 1000;
    const currentBucket = Math.floor(Date.now() / intervalMs) * intervalMs;
    const rows = Array.from({ length: 10 }, (_, index) => {
      const timestamp = (currentBucket - (9 - index) * intervalMs) / 1000;
      return [timestamp, 99, 101, 100, 100, 10];
    }).reverse();
    return Response.json(rows);
  };

  try {
    const candles = await fetchHistoricalCandles('BTCUSDT', '15m');
    assert.equal(new URL(requests[1]).pathname, '/products/BTC-USDT/candles');
    assert.equal(new URL(requests[1]).searchParams.get('granularity'), '900');
    assert.equal(candles.length, 9);
    assert.deepEqual(candles[0], {
      timestamp: candles[0].timestamp,
      open: 100,
      high: 101,
      low: 99,
      close: 100,
      volume: 10,
    });
    assert.ok(candles[0].timestamp < candles.at(-1).timestamp);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('ICT candle retrieval - Skips unsupported Coinbase 4H granularity and uses Kraken', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async url => {
    calls.push(url);
    if (url.startsWith('https://api.binance.com/')) {
      return new Response('Forbidden', { status: 403, headers: { 'Content-Type': 'text/plain' } });
    }
    if (url.startsWith('https://api.kraken.com/')) {
      const intervalMs = 4 * 60 * 60 * 1000;
      const currentBucket = Math.floor(Date.now() / intervalMs) * intervalMs;
      const rows = Array.from({ length: 10 }, (_, index) => [
        (currentBucket - (9 - index) * intervalMs) / 1000,
        '100', '101', '99', '100', '100', '10', 10,
      ]);
      return Response.json({ error: [], result: { XBTUSDT: rows, last: 1 } });
    }
    throw new Error('Coinbase must not receive unsupported 4H granularity 14400');
  };

  try {
    const candles = await fetchHistoricalCandles('BTCUSDT', '4h');
    assert.equal(candles.length, 9);
    assert.equal(calls.length, 2);
    assert.equal(new URL(calls[1]).searchParams.get('interval'), '240');
    assert.ok(!calls.some(url => url.includes('granularity=14400')));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('ICT candle retrieval - Falls back from Binance and Coinbase failures and reports sanitized provider errors', async () => {
  const originalFetch = globalThis.fetch;
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args);
  globalThis.fetch = async url => {
    if (url.startsWith('https://api.binance.com/')) {
      return new Response('Forbidden', { status: 403, headers: { 'Content-Type': 'text/plain' } });
    }
    if (url.startsWith('https://api.exchange.coinbase.com/')) {
      return Response.json({ message: 'invalid candle granularity' }, { status: 400 });
    }
    return Response.json({ error: ['EQuery:Unknown asset pair'] }, { status: 200 });
  };

  try {
    await assert.rejects(
      fetchHistoricalCandles('BTCUSDT', '15m'),
      /Binance failed \(HTTP 403 non-JSON response\).*Coinbase failed \(HTTP 400 \(invalid candle granularity\)\).*Kraken failed \(EQuery:Unknown asset pair\)/
    );
    assert.equal(warnings.length, 3);
    assert.deepEqual(warnings.map(args => args[1].provider), ['Binance', 'Coinbase', 'Kraken']);
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test('ICT candle retrieval - Rejects malformed provider rows', async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  console.warn = () => {};
  globalThis.fetch = async url => {
    if (url.startsWith('https://api.binance.com/')) {
      return Response.json([[Date.now(), 'not-a-price']]);
    }
    if (url.startsWith('https://api.exchange.coinbase.com/')) {
      return Response.json([[Math.floor(Date.now() / 1000) - 600, 'not-a-price', 101, 100, 100, 1]]);
    }
    return Response.json({ error: [], result: { XBTUSDT: 'not-an-array', last: 1 } });
  };

  try {
    await assert.rejects(
      fetchHistoricalCandles('BTCUSDT', '1m'),
      /malformed or non-numeric candle data.*malformed or non-numeric candle data.*missing OHLC result array/
    );
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test('ICT candle retrieval - Rejects candle gaps, duplicate timestamps, and fewer than the minimum candles', async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  console.warn = () => {};
  let mode = '';
  globalThis.fetch = async url => {
    if (url.startsWith('https://api.binance.com/')) {
      const intervalMs = 60_000;
      const bucket = Math.floor(Date.now() / intervalMs) * intervalMs;
      let timestamps = Array.from({ length: mode === 'insufficient' ? 8 : 10 }, (_, index) => (
        bucket - (mode === 'insufficient' ? 8 - index : 10 - index) * intervalMs
      ));
      if (mode === 'gap') timestamps[4] += intervalMs * 2;
      if (mode === 'duplicate') timestamps[4] = timestamps[3];
      return Response.json(timestamps.map(timestamp => [
        timestamp, '100', '101', '99', '100', '10', timestamp + intervalMs - 1,
      ]));
    }
    if (url.startsWith('https://api.exchange.coinbase.com/')) {
      return Response.json({ message: 'fallback unavailable' }, { status: 400 });
    }
    return Response.json({ error: ['fallback unavailable'], result: {} });
  };

  try {
    mode = 'gap';
    await assert.rejects(fetchHistoricalCandles('BTCUSDT', '1m'), /incomplete candle sequence \(time gap\)/);
    mode = 'duplicate';
    await assert.rejects(fetchHistoricalCandles('BTCUSDT', '1m'), /duplicate or unordered candle timestamps/);
    mode = 'insufficient';
    await assert.rejects(fetchHistoricalCandles('BTCUSDT', '1m'), /insufficient closed candles \(8\/9\)/);
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test('ICT candle retrieval - Does not replace requested USDT quote with USD', async () => {
  const originalFetch = globalThis.fetch;
  const urls = [];
  globalThis.fetch = async url => {
    urls.push(url);
    if (url.startsWith('https://api.binance.com/')) {
      return new Response('Forbidden', { status: 403, headers: { 'Content-Type': 'text/plain' } });
    }
    if (url.startsWith('https://api.exchange.coinbase.com/')) {
      return Response.json({ message: 'unavailable' }, { status: 400 });
    }
    const parsedUrl = new URL(url);
    const intervalMs = Number(parsedUrl.searchParams.get('interval')) * 60_000;
    const currentBucket = Math.floor(Date.now() / intervalMs) * intervalMs;
    const rows = Array.from({ length: 10 }, (_, index) => [
      (currentBucket - (9 - index) * intervalMs) / 1000,
      '100', '101', '99', '100', '100', '10', 10,
    ]);
    return Response.json({ error: [], result: { XBTUSDT: rows, last: 1 } });
  };

  try {
    await fetchHistoricalCandles('BTCUSDT', '1m');
    const krakenUrl = urls.find(url => url.startsWith('https://api.kraken.com/'));
    assert.equal(new URL(krakenUrl).searchParams.get('pair'), 'XBTUSDT');
    assert.ok(!urls.some(url => (
      url.startsWith('https://api.exchange.coinbase.com/') &&
      new URL(url).pathname === '/products/BTC-USD/candles'
    )));
    assert.ok(!urls.some(url => (
      url.startsWith('https://api.kraken.com/') &&
      new URL(url).searchParams.get('pair') === 'XBTUSD'
    )));
  } finally {
    globalThis.fetch = originalFetch;
  }
});
