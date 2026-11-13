const CRYPTO_BASES = new Set([
  'BTC', 'ETH', 'BNB', 'SOL', 'XRP', 'ADA', 'DOGE', 'TRX', 'AVAX', 'SHIB',
  'DOT', 'LINK', 'BCH', 'LTC', 'NEAR', 'UNI', 'ATOM', 'ETC', 'XLM', 'APT',
]);

export class MarketDataError extends Error {
  constructor(symbol, failures) {
    super(`No crypto price available for ${symbol}. ${failures.join('; ')}`);
    this.name = 'MarketDataError';
  }
}

function resolveCryptoPair(symbol) {
  for (const quote of ['USDT', 'BUSD', 'USD']) {
    if (symbol.endsWith(quote)) {
      const base = symbol.slice(0, -quote.length);
      if (base && (quote !== 'USD' || CRYPTO_BASES.has(base))) {
        return { base, quote, binanceSymbol: symbol, coinbaseQuote: quote };
      }
    }
  }
  return CRYPTO_BASES.has(symbol)
    ? { base: symbol, quote: 'USDT', binanceSymbol: `${symbol}USDT`, coinbaseQuote: 'USDT' }
    : null;
}

function sanitizeErrorDescription(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return 'No provider error description';
  }
  return value
    .replace(/(api[_-]?key|token|secret)\s*[:=]\s*["']?[^,\s"'&]+/gi, '$1=[redacted]')
    .replace(/\b\d{5,}:[A-Za-z0-9_-]{20,}\b/g, '[redacted]')
    .replace(/[\r\n\t]+/g, ' ')
    .slice(0, 200);
}

function isJsonContentType(response) {
  const contentType = response.headers.get('content-type')?.split(';', 1)[0].trim().toLowerCase();
  return contentType === 'application/json' || Boolean(contentType?.endsWith('+json'));
}

function logProviderFailure(provider, requestedSymbol, httpStatus, description) {
  const safeDescription = sanitizeErrorDescription(description);
  console.warn('[Market data]', {
    provider,
    httpStatus,
    requestedSymbol,
    description: safeDescription,
  });
  return `${provider}: ${httpStatus === null ? 'network error' : `HTTP ${httpStatus}`} (${safeDescription})`;
}

async function readJsonResponse(response, provider, requestedSymbol, errorDescription) {
  if (!isJsonContentType(response)) {
    const contentType = response.headers.get('content-type') || 'missing content type';
    const failure = logProviderFailure(
      provider,
      requestedSymbol,
      response.status,
      `Expected JSON but received ${sanitizeErrorDescription(contentType)}`,
    );
    return { body: null, failure };
  }

  let body;
  try {
    body = await response.json();
  } catch {
    const failure = logProviderFailure(provider, requestedSymbol, response.status, 'Malformed JSON response');
    return { body: null, failure };
  }

  if (!response.ok) {
    const failure = logProviderFailure(
      provider,
      requestedSymbol,
      response.status,
      String(errorDescription(body) || 'Provider rejected request'),
    );
    return { body: null, failure };
  }
  return { body, failure: null };
}

async function fetchBinancePrice(requestedSymbol, pair) {
  let response;
  try {
    response = await fetch(
      `https://api.binance.com/api/v3/ticker/24hr?symbol=${encodeURIComponent(pair.binanceSymbol)}`,
      { headers: { 'User-Agent': 'CloudflareWorker-TradingBot/1.0' } },
    );
  } catch {
    return {
      quote: null,
      failure: logProviderFailure('Binance', requestedSymbol, null, 'Network request failed'),
    };
  }

  const parsed = await readJsonResponse(response, 'Binance', requestedSymbol, body => body?.msg);
  if (!parsed.body) return { quote: null, failure: parsed.failure };

  const price = Number(parsed.body.lastPrice);
  const high = Number(parsed.body.highPrice);
  const low = Number(parsed.body.lowPrice);
  const closeTime = Number(parsed.body.closeTime);
  if (!Number.isFinite(price) || price <= 0 ||
      !Number.isFinite(high) || !Number.isFinite(low) ||
      !Number.isFinite(closeTime) || closeTime <= 0) {
    return {
      quote: null,
      failure: logProviderFailure('Binance', requestedSymbol, response.status, 'Malformed ticker response fields'),
    };
  }

  return {
    quote: {
      symbol: pair.binanceSymbol,
      price,
      high,
      low,
      timestamp: new Date(closeTime).toISOString(),
      source: 'Binance Public',
      isStale: false,
    },
    failure: null,
  };
}

async function fetchCoinbasePrice(requestedSymbol, pair) {
  const coinbasePair = `${pair.base}-${pair.coinbaseQuote}`;
  let response;
  try {
    response = await fetch(`https://api.coinbase.com/v2/prices/${encodeURIComponent(coinbasePair)}/spot`);
  } catch {
    return {
      quote: null,
      failure: logProviderFailure('Coinbase', requestedSymbol, null, 'Network request failed'),
    };
  }

  const parsed = await readJsonResponse(response, 'Coinbase', requestedSymbol, body => body?.message);
  if (!parsed.body) return { quote: null, failure: parsed.failure };

  const data = parsed.body.data;
  const price = Number(data?.amount);
  if (data?.base !== pair.base || data?.currency !== pair.coinbaseQuote ||
      !Number.isFinite(price) || price <= 0) {
    return {
      quote: null,
      failure: logProviderFailure(
        'Coinbase',
        requestedSymbol,
        response.status,
        `Malformed spot response or unexpected quote currency (expected ${pair.base}-${pair.coinbaseQuote})`,
      ),
    };
  }

  return {
    quote: {
      symbol: `${data.base}-${data.currency}`,
      price,
      timestamp: new Date().toISOString(),
      source: 'Coinbase Public Spot',
      isStale: false,
    },
    failure: null,
  };
}

export async function fetchCurrentPrice(symbol) {
  const requestedSymbol = symbol.toUpperCase().trim();
  if (!/^[A-Z0-9._=-]{1,20}$/.test(requestedSymbol)) {
    logProviderFailure('symbol validation', requestedSymbol, null, 'Invalid market symbol format');
    return null;
  }

  const cryptoPair = resolveCryptoPair(requestedSymbol);
  if (cryptoPair) {
    const primary = await fetchBinancePrice(requestedSymbol, cryptoPair);
    if (primary.quote) return primary.quote;

    const fallback = await fetchCoinbasePrice(requestedSymbol, cryptoPair);
    if (fallback.quote) return fallback.quote;

    throw new MarketDataError(requestedSymbol, [primary.failure, fallback.failure]);
  }

  let yahooSymbol = requestedSymbol;
  let sourceDesc = 'Yahoo Finance';
  if (requestedSymbol === 'XAUUSD') {
    yahooSymbol = 'GC=F';
    sourceDesc = 'Yahoo COMEX Gold Futures (GC=F)';
  } else if (requestedSymbol === 'XAGUSD') {
    yahooSymbol = 'SI=F';
    sourceDesc = 'Yahoo COMEX Silver Futures (SI=F)';
  } else if (requestedSymbol.length === 6 && !requestedSymbol.endsWith('USDT')) {
    yahooSymbol = `${requestedSymbol}=X`;
    sourceDesc = 'Yahoo FX';
  }

  let response;
  try {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(yahooSymbol)}?interval=1m&range=1d`;
    response = await fetch(url, {
      headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' },
    });
  } catch {
    logProviderFailure('Yahoo Finance', requestedSymbol, null, 'Network request failed');
    return null;
  }

  const parsed = await readJsonResponse(
    response,
    'Yahoo Finance',
    requestedSymbol,
    body => body?.chart?.error?.description,
  );
  if (!parsed.body) return null;

  const meta = parsed.body?.chart?.result?.[0]?.meta;
  const currentPrice = Number(meta?.regularMarketPrice);
  const marketTime = Number(meta?.regularMarketTime);
  if (!Number.isFinite(currentPrice) || currentPrice <= 0 ||
      !Number.isFinite(marketTime) || marketTime <= 0) {
    logProviderFailure(
      'Yahoo Finance',
      requestedSymbol,
      response.status,
      parsed.body?.chart?.error?.description || 'Malformed chart response fields',
    );
    return null;
  }

  const lastTime = new Date(marketTime * 1000);
  return {
    symbol: requestedSymbol,
    price: currentPrice,
    high: Number.isFinite(Number(meta.regularMarketDayHigh)) ? Number(meta.regularMarketDayHigh) : undefined,
    low: Number.isFinite(Number(meta.regularMarketDayLow)) ? Number(meta.regularMarketDayLow) : undefined,
    timestamp: lastTime.toISOString(),
    source: sourceDesc,
    isStale: (Date.now() - lastTime.getTime()) / (1000 * 60) > 60,
  };
}
