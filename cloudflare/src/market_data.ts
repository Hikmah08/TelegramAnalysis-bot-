export interface PriceQuote {
  symbol: string;
  price: number;
  high?: number;
  low?: number;
  timestamp: string;
  source: string;
  isStale: boolean;
}

const CRYPTO_BASES = new Set([
  'BTC', 'ETH', 'BNB', 'SOL', 'XRP', 'ADA', 'DOGE', 'TRX', 'AVAX', 'SHIB',
  'DOT', 'LINK', 'BCH', 'LTC', 'NEAR', 'UNI', 'ATOM', 'ETC', 'XLM', 'APT',
]);

function resolveCryptoPair(symbol: string): string | null {
  if (symbol.endsWith('USDT') || symbol.endsWith('BUSD')) {
    return symbol;
  }
  return CRYPTO_BASES.has(symbol) ? `${symbol}USDT` : null;
}

function sanitizeErrorDescription(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0) {
    return 'No provider error description';
  }

  return value
    .replace(/(api[_-]?key|token|secret)\s*[:=]\s*["']?[^,\s"'&]+/gi, '$1=[redacted]')
    .replace(/\b\d{5,}:[A-Za-z0-9_-]{20,}\b/g, '[redacted]')
    .replace(/[\r\n\t]+/g, ' ')
    .slice(0, 200);
}

function logProviderResult(
  provider: string,
  requestedSymbol: string,
  httpStatus: number | null,
  description: unknown
): void {
  console.warn('[Market data]', {
    provider,
    httpStatus,
    requestedSymbol,
    description: sanitizeErrorDescription(description),
  });
}

async function fetchBinancePrice(requestedSymbol: string, pair: string): Promise<PriceQuote | null> {
  let response: Response;
  try {
    response = await fetch(
      `https://api.binance.com/api/v3/ticker/24hr?symbol=${encodeURIComponent(pair)}`,
      { headers: { 'User-Agent': 'CloudflareWorker-TradingBot/1.0' } }
    );
  } catch {
    logProviderResult('Binance', requestedSymbol, null, 'Network request failed');
    return null;
  }

  type BinanceTickerResponse = {
    lastPrice?: unknown;
    highPrice?: unknown;
    lowPrice?: unknown;
    closeTime?: unknown;
    msg?: unknown;
  };
  let data: BinanceTickerResponse | null = null;
  try {
    data = await response.json() as BinanceTickerResponse;
  } catch {
    logProviderResult('Binance', requestedSymbol, response.status, 'Malformed JSON response');
    return null;
  }

  if (!response.ok) {
    logProviderResult('Binance', requestedSymbol, response.status, data?.msg);
    return null;
  }

  const price = Number(data?.lastPrice);
  const high = Number(data?.highPrice);
  const low = Number(data?.lowPrice);
  const closeTime = Number(data?.closeTime);
  if (!Number.isFinite(price) || price <= 0 ||
      !Number.isFinite(high) || !Number.isFinite(low) ||
      !Number.isFinite(closeTime) || closeTime <= 0) {
    logProviderResult('Binance', requestedSymbol, response.status, 'Malformed ticker response fields');
    return null;
  }

  const timestamp = new Date(closeTime);
  if (!Number.isFinite(timestamp.getTime())) {
    logProviderResult('Binance', requestedSymbol, response.status, 'Malformed ticker timestamp');
    return null;
  }

  return {
    symbol: pair,
    price,
    high,
    low,
    timestamp: timestamp.toISOString(),
    source: 'Binance Public',
    isStale: false,
  };
}

export async function fetchCurrentPrice(symbol: string): Promise<PriceQuote | null> {
  const requestedSymbol = symbol.toUpperCase().trim();
  if (!/^[A-Z0-9._=-]{1,20}$/.test(requestedSymbol)) {
    logProviderResult('symbol validation', requestedSymbol, null, 'Invalid market symbol format');
    return null;
  }

  const cryptoPair = resolveCryptoPair(requestedSymbol);
  if (cryptoPair) {
    return fetchBinancePrice(requestedSymbol, cryptoPair);
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

  let response: Response;
  try {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(yahooSymbol)}?interval=1m&range=1d`;
    response = await fetch(url, {
      headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' },
    });
  } catch {
    logProviderResult('Yahoo Finance', requestedSymbol, null, 'Network request failed');
    return null;
  }

  let data: any;
  try {
    data = await response.json();
  } catch {
    logProviderResult('Yahoo Finance', requestedSymbol, response.status, 'Malformed JSON response');
    return null;
  }

  if (!response.ok) {
    logProviderResult('Yahoo Finance', requestedSymbol, response.status, data?.chart?.error?.description);
    return null;
  }

  const result = data?.chart?.result?.[0];
  const meta = result?.meta;
  const currentPrice = Number(meta?.regularMarketPrice);
  const marketTime = Number(meta?.regularMarketTime);
  if (!Number.isFinite(currentPrice) || currentPrice <= 0 ||
      !Number.isFinite(marketTime) || marketTime <= 0) {
    logProviderResult(
      'Yahoo Finance',
      requestedSymbol,
      response.status,
      data?.chart?.error?.description || 'Malformed chart response fields'
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
