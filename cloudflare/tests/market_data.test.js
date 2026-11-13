import test from 'node:test';
import assert from 'node:assert/strict';
import {
  fetchCurrentPrice,
  getMarketInstrumentCategory,
  MarketDataError,
} from '../src/market_data.js';

const binanceTicker = {
  lastPrice: '65000.25',
  highPrice: '66000.00',
  lowPrice: '64000.00',
  closeTime: 1730000000000,
};

function coinbaseSpot(base, currency, amount = '71234.56') {
  return Response.json({ data: { base, currency, amount } }, { status: 200 });
}

test('Market data - BTC, ETH, BTCUSDT and ETHUSDT use Binance primary pairs', async () => {
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
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Market data - Binance 403 falls back to Coinbase and preserves quote currency', async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  const requestedUrls = [];
  const diagnostics = [];
  globalThis.fetch = async url => {
    requestedUrls.push(url);
    if (url.startsWith('https://api.binance.com/')) {
      return new Response('<html>Forbidden</html>', {
        status: 403,
        headers: { 'Content-Type': 'text/html' },
      });
    }
    return coinbaseSpot('BTC', 'USDT');
  };
  console.warn = (...args) => diagnostics.push(args);

  try {
    const quote = await fetchCurrentPrice('BTCUSDT');
    assert.equal(quote.symbol, 'BTC-USDT');
    assert.equal(quote.price, 71234.56);
    assert.equal(quote.source, 'Coinbase Public Spot');
    assert.ok(requestedUrls[0].startsWith('https://api.binance.com/'));
    assert.equal(requestedUrls[1], 'https://api.coinbase.com/v2/prices/BTC-USDT/spot');
    assert.deepEqual(diagnostics[0], ['[Market data]', {
      provider: 'Binance',
      httpStatus: 403,
      requestedSymbol: 'BTCUSDT',
      description: 'Expected JSON but received text/html',
    }]);
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test('Market data - Bare BTC and ETH fallback preserve their normalized USDT quote', async () => {
  const originalFetch = globalThis.fetch;
  const requestedUrls = [];
  globalThis.fetch = async url => {
    requestedUrls.push(url);
    if (url.startsWith('https://api.binance.com/')) {
      return new Response('forbidden', {
        status: 403,
        headers: { 'Content-Type': 'text/plain' },
      });
    }
    const base = url.includes('/ETH-USDT/') ? 'ETH' : 'BTC';
    return coinbaseSpot(base, 'USDT', '70000.00');
  };

  try {
    const btc = await fetchCurrentPrice('BTC');
    const eth = await fetchCurrentPrice('ETH');
    assert.equal(btc.symbol, 'BTC-USDT');
    assert.equal(eth.symbol, 'ETH-USDT');
    assert.ok(requestedUrls.includes('https://api.coinbase.com/v2/prices/BTC-USDT/spot'));
    assert.ok(requestedUrls.includes('https://api.coinbase.com/v2/prices/ETH-USDT/spot'));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Market data - Both crypto providers failing returns a clear sanitized error', async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  const diagnostics = [];
  globalThis.fetch = async url => {
    if (url.startsWith('https://api.binance.com/')) {
      return new Response('Forbidden', { status: 403, headers: { 'Content-Type': 'text/plain' } });
    }
    return Response.json({ message: 'Pair unavailable token=private-value' }, { status: 404 });
  };
  console.warn = (...args) => diagnostics.push(args);

  try {
    await assert.rejects(fetchCurrentPrice('BTCUSDT'), error => {
      assert.ok(error instanceof MarketDataError);
      assert.match(error.message, /Binance: HTTP 403/);
      assert.match(error.message, /Coinbase: HTTP 404/);
      assert.match(error.message, /token=\[redacted\]/);
      assert.doesNotMatch(error.message, /private-value/);
      return true;
    });
    assert.deepEqual(diagnostics.map(([, result]) => result.provider), ['Binance', 'Coinbase']);
    assert.equal(JSON.stringify(diagnostics).includes('private-value'), false);
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test('Market data - Rejects malformed JSON and malformed ticker data before fallback parsing', async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  const diagnostics = [];
  let mode = 'binance-invalid-json';
  globalThis.fetch = async url => {
    if (url.startsWith('https://api.binance.com/')) {
      if (mode === 'binance-invalid-json') {
        return new Response('not json', { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return Response.json({ ...binanceTicker, lastPrice: 'not-a-price' }, { status: 200 });
    }
    if (mode === 'coinbase-invalid-json') {
      return new Response('{bad json', { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    const coinbasePair = new URL(url).pathname.split('/').at(-2);
    const [base, currency] = coinbasePair.split('-');
    return coinbaseSpot(base, currency, '3500.25');
  };
  console.warn = (...args) => diagnostics.push(args);

  try {
    const fallbackQuote = await fetchCurrentPrice('BTCUSDT');
    assert.equal(fallbackQuote.symbol, 'BTC-USDT');
    assert.equal(diagnostics[0][1].description, 'Malformed JSON response');

    mode = 'binance-malformed-ticker';
    const malformedFallback = await fetchCurrentPrice('ETH');
    assert.equal(malformedFallback.symbol, 'ETH-USDT');
    assert.equal(malformedFallback.price, 3500.25);
    assert.equal(diagnostics[1][1].description, 'Malformed ticker response fields');

    mode = 'coinbase-invalid-json';
    await assert.rejects(fetchCurrentPrice('BTC'), /Coinbase: HTTP 200 \(Malformed JSON response\)/);
    assert.equal(diagnostics.at(-1)[1].provider, 'Coinbase');
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});

test('Market data - Rejects Coinbase response that changes the requested quote currency', async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  const diagnostics = [];
  let requestCount = 0;
  globalThis.fetch = async () => {
    requestCount += 1;
    if (requestCount === 1) {
      return new Response('blocked', { status: 403, headers: { 'Content-Type': 'text/plain' } });
    }
    return coinbaseSpot('BTC', 'USD');
  };
  console.warn = (...args) => diagnostics.push(args);

  try {
    await assert.rejects(fetchCurrentPrice('BTCUSDT'), /expected BTC-USDT/);
    assert.equal(diagnostics.at(-1)[1].provider, 'Coinbase');
    assert.match(diagnostics.at(-1)[1].description, /unexpected quote currency/);
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

test('Market data - Spot metals use Twelve Data without substituting futures or quote currency', async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async url => {
    requests.push(url);
    return Response.json({ price: '2345.67' });
  };
  try {
    for (const [symbol, providerSymbol] of [
      ['XAUUSD', 'XAU/USD'],
      ['XAGUSD', 'XAG/USD'],
    ]) {
      const quote = await fetchCurrentPrice(symbol, 'mock-private-api-key');
      const url = new URL(requests.at(-1));
      assert.equal(url.pathname, '/price');
      assert.equal(url.searchParams.get('symbol'), providerSymbol);
      assert.equal(url.searchParams.get('apikey'), 'mock-private-api-key');
      assert.equal(quote.symbol, symbol);
      assert.equal(quote.price, 2345.67);
      assert.equal(quote.source, 'Twelve Data Latest Price');
    }

    assert.equal(getMarketInstrumentCategory('BTCUSDT'), 'CRYPTO');
    assert.equal(getMarketInstrumentCategory('EURUSD'), 'FOREX');
    assert.equal(getMarketInstrumentCategory('XAUUSD'), 'METAL');
    assert.equal(getMarketInstrumentCategory('XAUUSDT'), null);
    assert.equal(requests.some(url => url.includes('GC%3DF') || url.includes('SI%3DF')), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Market data - Metals reject unsupported XAUUSDT and sanitize Twelve Data failures', async () => {
  const originalFetch = globalThis.fetch;
  const originalWarn = console.warn;
  const diagnostics = [];
  let requests = 0;
  globalThis.fetch = async () => {
    requests += 1;
    return Response.json({
      status: 'error',
      message: 'Invalid apiKey=do-not-log-this',
    }, { status: 401 });
  };
  console.warn = (...args) => diagnostics.push(args);

  try {
    await assert.rejects(fetchCurrentPrice('XAUUSDT', 'secret-api-key'), /no verified XAU\/USDT listing/i);
    assert.equal(requests, 0);

    await assert.rejects(fetchCurrentPrice('XAUUSD', 'secret-api-key'), error => {
      assert.ok(error instanceof MarketDataError);
      assert.match(error.message, /Twelve Data: HTTP 401/);
      assert.doesNotMatch(error.message, /do-not-log-this|secret-api-key/);
      return true;
    });
    assert.equal(JSON.stringify(diagnostics).includes('secret-api-key'), false);
    assert.equal(JSON.stringify(diagnostics).includes('do-not-log-this'), false);
  } finally {
    globalThis.fetch = originalFetch;
    console.warn = originalWarn;
  }
});
