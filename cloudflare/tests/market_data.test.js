import test from 'node:test';
import assert from 'node:assert/strict';
import { fetchCurrentPrice } from '../src/market_data.js';

const binanceTicker = {
  lastPrice: '65000.25',
  highPrice: '66000.00',
  lowPrice: '64000.00',
  closeTime: 1730000000000,
};

test('Market data - BTC, ETH, BTCUSDT and ETHUSDT use Binance crypto pairs', async () => {
  const originalFetch = globalThis.fetch;
  const requestedUrls = [];
  globalThis.fetch = async url => {
    requestedUrls.push(url);
    return Response.json(binanceTicker, { status: 200 });
  };

  try {
    for (const [symbol, pair] of [
      ['BTC', 'BTCUSDT'],
      ['ETH', 'ETHUSDT'],
      ['BTCUSDT', 'BTCUSDT'],
      ['ETHUSDT', 'ETHUSDT'],
    ]) {
      const quote = await fetchCurrentPrice(symbol);
      assert.equal(quote?.symbol, pair);
      assert.equal(quote?.price, 65000.25);
      assert.equal(quote?.source, 'Binance Public');
      assert.equal(quote?.isStale, false);
    }

    assert.deepEqual(requestedUrls.map(url => new URL(url).searchParams.get('symbol')), [
      'BTCUSDT', 'ETHUSDT', 'BTCUSDT', 'ETHUSDT',
    ]);
    assert.ok(requestedUrls.every(url => url.startsWith('https://api.binance.com/api/v3/ticker/24hr?')));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Market data - Reports sanitized provider status for failed crypto requests without fallback', async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  const requestedUrls = [];
  const diagnostics = [];
  globalThis.fetch = async url => {
    requestedUrls.push(url);
    return Response.json({ code: -1003, msg: 'Rate limit token=do-not-log-this' }, { status: 429 });
  };
  console.warn = (...args) => diagnostics.push(args);

  try {
    assert.equal(await fetchCurrentPrice('BTC'), null);
    assert.equal(requestedUrls.length, 1);
    assert.ok(requestedUrls[0].startsWith('https://api.binance.com/'));
    assert.equal(diagnostics.length, 1);
    assert.deepEqual(diagnostics[0], ['[Market data]', {
      provider: 'Binance',
      httpStatus: 429,
      requestedSymbol: 'BTC',
      description: 'Rate limit token=[redacted]',
    }]);
    assert.equal(JSON.stringify(diagnostics).includes('do-not-log-this'), false);
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test('Market data - Rejects malformed Binance JSON and ticker payloads', async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  const diagnostics = [];
  let responseMode = 'json';
  globalThis.fetch = async () => {
    if (responseMode === 'invalid-json') {
      return new Response('not json', { status: 200 });
    }
    return Response.json({ ...binanceTicker, lastPrice: 'not-a-price' }, { status: 200 });
  };
  console.warn = (...args) => diagnostics.push(args);

  try {
    assert.equal(await fetchCurrentPrice('ETHUSDT'), null);
    responseMode = 'invalid-json';
    assert.equal(await fetchCurrentPrice('BTCUSDT'), null);
    assert.equal(diagnostics.length, 2);
    assert.equal(diagnostics[0][1].description, 'Malformed ticker response fields');
    assert.equal(diagnostics[1][1].description, 'Malformed JSON response');
    assert.ok(diagnostics.every(([, entry]) => entry.provider === 'Binance' && entry.httpStatus === 200));
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test('Market data - Keeps forex routed to Yahoo Finance', async () => {
  const originalFetch = globalThis.fetch;
  let requestedUrl = '';
  globalThis.fetch = async url => {
    requestedUrl = url;
    return Response.json({
      chart: {
        result: [{
          meta: {
            regularMarketPrice: 1.08,
            regularMarketTime: 1730000000,
            regularMarketDayHigh: 1.09,
            regularMarketDayLow: 1.07,
          },
        }],
      },
    }, { status: 200 });
  };

  try {
    const quote = await fetchCurrentPrice('EURUSD');
    assert.ok(requestedUrl.startsWith('https://query1.finance.yahoo.com/v8/finance/chart/EURUSD%3DX?'));
    assert.equal(quote?.source, 'Yahoo FX');
    assert.equal(quote?.price, 1.08);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
