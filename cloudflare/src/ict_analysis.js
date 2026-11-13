const BINANCE_ENDPOINT = 'https://api.binance.com/api/v3';
const INTERVALS = ['4h', '1h', '15m', '5m', '1m'];
const INTERVAL_MINUTES = {
  '1m': 1,
  '5m': 5,
  '15m': 15,
  '1h': 60,
  '4h': 240,
};
const LEFT_BARS = 2;
const RIGHT_BARS = 2;
const MAX_SETUP_CANDLES = 50;
const MIN_CANDLE_COUNT = 9;
const TWELVE_DATA_METALS = {
  XAUUSD: 'XAU/USD',
  XAGUSD: 'XAG/USD',
};
const TWELVE_DATA_INTERVALS = {
  '1m': '1min',
  '5m': '5min',
  '15m': '15min',
  '1h': '1h',
  '4h': '4h',
};

export class IctAnalysisError extends Error {
  constructor(message) {
    super(message);
    this.name = 'IctAnalysisError';
  }
}

function findSwings(candles) {
  const swings = [];
  for (let i = LEFT_BARS; i < candles.length - RIGHT_BARS; i += 1) {
    let high = true;
    let low = true;
    for (let j = i - LEFT_BARS; j <= i + RIGHT_BARS; j += 1) {
      if (j === i) continue;
      if (candles[j].high >= candles[i].high) high = false;
      if (candles[j].low <= candles[i].low) low = false;
    }
    if (high) swings.push({ type: 'high', price: candles[i].high, index: i, confirmed: i + RIGHT_BARS });
    if (low) swings.push({ type: 'low', price: candles[i].low, index: i, confirmed: i + RIGHT_BARS });
  }
  return swings.sort((a, b) => a.confirmed - b.confirmed || a.index - b.index);
}

function getFvg(candles, index) {
  if (index < 2) return null;
  const first = candles[index - 2];
  const third = candles[index];
  if (third.low > first.high) {
    return {
      direction: 'BULLISH',
      top: third.low,
      bottom: first.high,
      ce: (third.low + first.high) / 2,
      index,
      timestamp: third.timestamp,
    };
  }
  if (first.low > third.high) {
    return {
      direction: 'BEARISH',
      top: first.low,
      bottom: third.high,
      ce: (first.low + third.high) / 2,
      index,
      timestamp: third.timestamp,
    };
  }
  return null;
}

function findStructureBreak(candle, index, swings, direction) {
  for (const swing of swings) {
    if (index <= swing.confirmed) continue;
    if (direction === 'BULLISH' && swing.type === 'high' && candle.close > swing.price) {
      return swing;
    }
    if (direction === 'BEARISH' && swing.type === 'low' && candle.close < swing.price) {
      return swing;
    }
  }
  return null;
}

function findSequenceSetups(candles) {
  if (candles.length < LEFT_BARS + RIGHT_BARS + 3) return [];
  const swings = findSwings(candles);
  const active = [];
  const results = [];

  for (let index = 0; index < candles.length; index += 1) {
    const candle = candles[index];
    const confirmed = swings.filter((swing) => swing.confirmed <= index);
    const remaining = [];

    for (const setup of active) {
      if (index - setup.sweep.index > MAX_SETUP_CANDLES) continue;
      if (setup.direction === 'BULLISH' && candle.low < setup.sweep.extreme) continue;
      if (setup.direction === 'BEARISH' && candle.high > setup.sweep.extreme) continue;

      if (setup.stage === 'SWEEP') {
        const recent = confirmed.filter((swing) => swing.index >= setup.sweep.index - 30);
        const broken = findStructureBreak(candle, index, recent, setup.direction);
        if (broken && index >= setup.sweep.index) {
          setup.stage = 'MSS';
          setup.mssIndex = index;
          setup.notes.push(`${setup.direction} MSS confirmed beyond ${broken.price.toFixed(2)} at ${new Date(candle.timestamp).toISOString()}.`);
        }
      } else if (setup.stage === 'MSS') {
        const fvg = getFvg(candles, index);
        if (fvg && fvg.direction === setup.direction && fvg.index >= setup.mssIndex) {
          setup.stage = 'FVG';
          setup.fvg = fvg;
          setup.notes.push(`${setup.direction} FVG formed between ${fvg.bottom.toFixed(2)} and ${fvg.top.toFixed(2)}.`);
        }
      } else if (setup.stage === 'FVG' && index > setup.fvg.index) {
        const retraced = setup.direction === 'BULLISH'
          ? candle.low <= setup.fvg.top
          : candle.high >= setup.fvg.bottom;
        if (retraced) {
          const entryPrice = setup.direction === 'BULLISH' ? setup.fvg.top : setup.fvg.bottom;
          results.push({
            direction: setup.direction,
            sweep: setup.sweep,
            entryPrice,
            stopLoss: setup.sweep.extreme,
            timestamp: candle.timestamp,
            sweepTimestamp: setup.sweep.timestamp,
            fvgTimestamp: setup.fvg.timestamp,
            notes: [
              `${setup.direction} liquidity sweep of ${setup.sweep.level.toFixed(2)}; wick extreme ${setup.sweep.extreme.toFixed(2)}.`,
              ...setup.notes,
              `Price retraced into the FVG at ${entryPrice.toFixed(2)}.`,
            ],
          });
          continue;
        }
      }
      remaining.push(setup);
    }
    active.splice(0, active.length, ...remaining);

    for (const swing of confirmed) {
      if (index <= swing.confirmed) continue;
      const direction = swing.type === 'low' ? 'BULLISH' : 'BEARISH';
      const swept = direction === 'BULLISH'
        ? candle.low < swing.price && candle.close >= swing.price
        : candle.high > swing.price && candle.close <= swing.price;
      if (swept) {
        active.push({
          direction,
          stage: 'SWEEP',
          sweep: {
            index,
            timestamp: candle.timestamp,
            level: swing.price,
            extreme: direction === 'BULLISH' ? candle.low : candle.high,
          },
          notes: [],
        });
      }
    }
  }
  const distinct = new Map();
  for (const result of results) {
    const key = [
      result.direction,
      result.sweep.timestamp,
      result.entryPrice,
      result.stopLoss,
    ].join(':');
    if (!distinct.has(key)) distinct.set(key, result);
  }
  return Array.from(distinct.values());
}

function getDirectionalBias(candles) {
  const swings = findSwings(candles);
  let latestBreak = null;
  for (let index = 0; index < candles.length; index += 1) {
    const confirmed = swings.filter((swing) => swing.confirmed <= index);
    const bullish = findStructureBreak(candles[index], index, confirmed, 'BULLISH');
    const bearish = findStructureBreak(candles[index], index, confirmed, 'BEARISH');
    if (bullish) latestBreak = { direction: 'BULLISH', timestamp: candles[index].timestamp };
    if (bearish) latestBreak = { direction: 'BEARISH', timestamp: candles[index].timestamp };
  }
  if (latestBreak) return latestBreak;

  const highs = swings.filter((swing) => swing.type === 'high');
  const lows = swings.filter((swing) => swing.type === 'low');
  if (highs.length < 2 || lows.length < 2) return null;
  const higherHigh = highs[highs.length - 1].price > highs[highs.length - 2].price;
  const higherLow = lows[lows.length - 1].price > lows[lows.length - 2].price;
  const lowerHigh = highs[highs.length - 1].price < highs[highs.length - 2].price;
  const lowerLow = lows[lows.length - 1].price < lows[lows.length - 2].price;
  if (higherHigh && higherLow) return { direction: 'BULLISH', timestamp: candles[candles.length - 1].timestamp };
  if (lowerHigh && lowerLow) return { direction: 'BEARISH', timestamp: candles[candles.length - 1].timestamp };
  return null;
}

function findHourlyAois(candles, direction) {
  const aois = [];
  for (let index = 2; index < candles.length; index += 1) {
    const fvg = getFvg(candles, index);
    if (!fvg || fvg.direction !== direction) continue;
    const unmitigated = candles.slice(index + 1).every((candle) => (
      direction === 'BULLISH' ? candle.low > fvg.bottom : candle.high < fvg.top
    ));
    if (unmitigated) {
      aois.push({ bottom: fvg.bottom, top: fvg.top, timestamp: fvg.timestamp, type: 'FVG' });
    }
  }
  const swings = findSwings(candles);
  for (let index = 0; index < candles.length; index += 1) {
    for (const swing of swings) {
      if (index <= swing.confirmed || index - swing.confirmed > MAX_SETUP_CANDLES) continue;
      const aligned = direction === 'BULLISH' && swing.type === 'low'
        ? candles[index].low < swing.price && candles[index].close >= swing.price
        : direction === 'BEARISH' && swing.type === 'high'
          ? candles[index].high > swing.price && candles[index].close <= swing.price
          : false;
      if (aligned) {
        const extreme = direction === 'BULLISH' ? candles[index].low : candles[index].high;
        aois.push({
          bottom: Math.min(swing.price, extreme),
          top: Math.max(swing.price, extreme),
          timestamp: candles[index].timestamp,
          type: 'liquidity sweep',
        });
      }
    }
  }
  return aois;
}

function overlapsAoi(setup, aois) {
  return aois.some((aoi) => (
    setup.sweep.level >= aoi.bottom && setup.sweep.level <= aoi.top
  ) || (
    setup.sweep.extreme >= aoi.bottom && setup.sweep.extreme <= aoi.top
  ));
}

export function analyzeIctCandles(timeframes) {
  const bias = getDirectionalBias(timeframes['4h']);
  if (!bias) return { opportunities: [] };
  const auditBase = [`4H directional bias: ${bias.direction}.`];
  const aois = findHourlyAois(timeframes['1h'], bias.direction);
  if (aois.length === 0) return { opportunities: [] };
  const fifteenMinuteSetups = findSequenceSetups(timeframes['15m'])
    .filter((setup) => setup.direction === bias.direction && overlapsAoi(setup, aois))
    .sort((a, b) => b.timestamp - a.timestamp);
  const opportunities = [];

  for (const setup15m of fifteenMinuteSetups) {
    const aoi = aois.find((candidate) => (
      setup15m.sweep.level >= candidate.bottom && setup15m.sweep.level <= candidate.top
    ) || (
      setup15m.sweep.extreme >= candidate.bottom && setup15m.sweep.extreme <= candidate.top
    ));
    if (!aoi) continue;
    const candles5m = timeframes['5m'].filter((candle) => candle.timestamp >= setup15m.sweepTimestamp);
    const confirmation5m = findSequenceSetups(candles5m)
      .filter((setup) => setup.direction === bias.direction && setup.timestamp >= setup15m.timestamp)
      .sort((a, b) => b.timestamp - a.timestamp)[0];
    if (!confirmation5m) continue;

    const candles1m = timeframes['1m'].filter((candle) => candle.timestamp >= confirmation5m.timestamp);
    const refinement1m = findSequenceSetups(candles1m)
      .filter((setup) => setup.direction === bias.direction)
      .sort((a, b) => b.timestamp - a.timestamp)[0];
    if (!refinement1m) continue;

    const entryPrice = refinement1m.entryPrice;
    const stopLoss = refinement1m.stopLoss;
    const risk = Math.abs(entryPrice - stopLoss);
    if (!Number.isFinite(risk) || risk <= 0) continue;
    if (bias.direction === 'BULLISH' ? entryPrice <= stopLoss : entryPrice >= stopLoss) continue;
    const takeProfit = bias.direction === 'BULLISH'
      ? entryPrice + risk * 4
      : entryPrice - risk * 4;
    if (!Number.isFinite(takeProfit) || takeProfit <= 0) continue;

    opportunities.push({
      direction: bias.direction,
      entryPrice,
      stopLoss,
      takeProfit,
      timestamp: refinement1m.timestamp,
      setup15mTimestamp: setup15m.timestamp,
      confirmation5mTimestamp: confirmation5m.timestamp,
      refinement1mTimestamp: refinement1m.timestamp,
      auditTrail: [
        ...auditBase,
        `1H AOI: ${aoi.type} ${aoi.bottom.toFixed(2)}–${aoi.top.toFixed(2)}.`,
        ...setup15m.notes.map((note) => `15M setup: ${note}`),
        ...confirmation5m.notes.map((note) => `5M confirmation: ${note}`),
        ...refinement1m.notes.map((note) => `1M refinement: ${note}`),
        `Risk management: ${risk.toFixed(2)} risk distance; 4:1 target ${takeProfit.toFixed(2)}.`,
      ],
    });
  }
  // The existing pipeline orders 15M setups newest-first and selects the
  // newest valid 5M confirmation and 1M refinement for each setup.
  return { opportunities: opportunities.slice(0, 1) };
}

function normalizeSymbol(symbol) {
  const requested = symbol.trim().toUpperCase().replace(/[/-]/g, '');
  const normalized = ['BTC', 'ETH'].includes(requested) ? `${requested}USDT` : requested;
  if (!/^[A-Z0-9]{2,20}$/.test(normalized)) {
    throw new IctAnalysisError('Invalid instrument symbol. Use letters and numbers, for example BTCUSDT.');
  }
  return normalized;
}

function sanitizeProviderDescription(value) {
  if (typeof value !== 'string' || value.length === 0) return '';
  return value
    .replace(/https?:\/\/\S+/gi, '[URL]')
    .replace(/\b(api[_-]?key|apikey|token|secret)\s*[:=]\s*["']?[^,\s"'&]+/gi, '$1=[redacted]')
    .replace(/\b\d{5,}:[A-Za-z0-9_-]{20,}\b/g, '[redacted]')
    .replace(/[\r\n\t]+/g, ' ')
    .slice(0, 160);
}

async function responseFailure(response) {
  const contentType = response.headers.get('content-type')?.toLowerCase() || '';
  if (!contentType.includes('json')) return `HTTP ${response.status} non-JSON response`;
  try {
    const body = await response.json();
    const detail = typeof body?.message === 'string'
      ? body.message
      : typeof body?.error === 'string'
        ? body.error
        : typeof body?.error_description === 'string'
          ? body.error_description
          : '';
    const safeDetail = sanitizeProviderDescription(detail);
    return `HTTP ${response.status}${safeDetail ? ` (${safeDetail})` : ''}`;
  } catch {
    return `HTTP ${response.status} malformed JSON error response`;
  }
}

function normalizeCandleRows(rows, mapper, intervalMs, now, allowMarketClosures = false) {
  if (!Array.isArray(rows) || rows.length === 0) {
    return { candles: null, description: 'malformed or empty candle payload' };
  }

  const candles = [];
  for (const row of rows) {
    const candle = mapper(row);
    if (!candle ||
        !Number.isFinite(candle.timestamp) ||
        !Number.isFinite(candle.open) ||
        !Number.isFinite(candle.high) ||
        !Number.isFinite(candle.low) ||
        !Number.isFinite(candle.close) ||
        !Number.isFinite(candle.volume) ||
        candle.timestamp <= 0 ||
        candle.volume < 0 ||
        candle.low <= 0 ||
        candle.high < Math.max(candle.open, candle.close, candle.low) ||
        candle.low > Math.min(candle.open, candle.close, candle.high)) {
      return { candles: null, description: 'malformed or non-numeric candle data' };
    }

    if (candle.timestamp + intervalMs <= now) {
      candles.push({
        timestamp: candle.timestamp,
        open: candle.open,
        high: candle.high,
        low: candle.low,
        close: candle.close,
        volume: candle.volume,
      });
    }
  }

  candles.sort((left, right) => left.timestamp - right.timestamp);
  for (let index = 1; index < candles.length; index += 1) {
    const difference = candles[index].timestamp - candles[index - 1].timestamp;
    if (difference <= 0) return { candles: null, description: 'duplicate or unordered candle timestamps' };
    const closedMarketGap = allowMarketClosures &&
      difference >= 24 * 60 * 60 * 1000 &&
      difference % intervalMs === 0;
    if (difference !== intervalMs && !closedMarketGap) {
      return { candles: null, description: 'incomplete candle sequence (time gap)' };
    }
  }
  if (candles.length < MIN_CANDLE_COUNT) {
    return { candles: null, description: `insufficient closed candles (${candles.length}/${MIN_CANDLE_COUNT})` };
  }
  return { candles, description: '' };
}

function extractKrakenRows(payload) {
  if (!payload || !Array.isArray(payload.error) || payload.error.length > 0 ||
      !payload.result || typeof payload.result !== 'object') {
    const error = Array.isArray(payload?.error) ? payload.error.join(', ') : '';
    return { rows: null, description: sanitizeProviderDescription(error) || 'malformed Kraken response' };
  }
  const entry = Object.entries(payload.result).find(([key, value]) => key !== 'last' && Array.isArray(value));
  return entry
    ? { rows: entry[1], description: '' }
    : { rows: null, description: 'missing OHLC result array' };
}

function parseTwelveDataTimestamp(value) {
  if (typeof value !== 'string') return NaN;
  const timestamp = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    ? Date.parse(value)
    : Date.parse(`${value.replace(' ', 'T')}Z`);
  return Number.isFinite(timestamp) ? timestamp : NaN;
}

async function fetchTwelveDataMetalCandles(symbol, interval, apiKey) {
  const requestedSymbol = symbol;
  const providerSymbol = TWELVE_DATA_METALS[symbol];
  if (!apiKey) {
    const description = 'TWELVE_DATA_API_KEY is not configured';
    console.warn('[ICT analysis] Candle provider failed', {
      provider: 'Twelve Data',
      symbol: requestedSymbol,
      interval,
      description,
    });
    throw new IctAnalysisError(
      `Unable to retrieve ${interval} candles for ${symbol}; Twelve Data failed (${description}).`
    );
  }

  try {
    const url = new URL('https://api.twelvedata.com/time_series');
    url.searchParams.set('symbol', providerSymbol);
    url.searchParams.set('interval', TWELVE_DATA_INTERVALS[interval]);
    url.searchParams.set('outputsize', '150');
    url.searchParams.set('timezone', 'UTC');
    url.searchParams.set('apikey', apiKey);
    const response = await fetch(url.toString(), { headers: { Accept: 'application/json' } });
    if (!response.ok) {
      throw new Error(await responseFailure(response));
    }
    if (!(response.headers.get('content-type') || '').toLowerCase().includes('json')) {
      throw new Error(`HTTP ${response.status} non-JSON response`);
    }

    const payload = await response.json().catch(() => null);
    if (!payload || payload.status === 'error' || !Array.isArray(payload.values)) {
      const description = sanitizeProviderDescription(
        payload?.message || payload?.code || 'malformed or empty time-series response'
      );
      throw new Error(description || 'malformed or empty time-series response');
    }
    if (payload.meta?.symbol && payload.meta.symbol !== providerSymbol) {
      throw new Error(`Unexpected instrument returned (${sanitizeProviderDescription(payload.meta.symbol)})`);
    }

    const intervalMs = INTERVAL_MINUTES[interval] * 60_000;
    const normalized = normalizeCandleRows(
      payload.values,
      row => row && typeof row === 'object'
        ? {
          timestamp: parseTwelveDataTimestamp(row.datetime),
          open: Number(row.open),
          high: Number(row.high),
          low: Number(row.low),
          close: Number(row.close),
          volume: Number(row.volume ?? 0),
        }
        : null,
      intervalMs,
      Date.now(),
      true
    );
    if (!normalized.candles) throw new Error(normalized.description);

    console.info('[ICT analysis] Candle provider succeeded', {
      provider: 'Twelve Data',
      symbol: requestedSymbol,
      interval,
      candleCount: normalized.candles.length,
    });
    return normalized.candles;
  } catch (error) {
    const description = sanitizeProviderDescription(
      error instanceof Error ? error.message : 'request failed'
    ) || 'request failed';
    console.warn('[ICT analysis] Candle provider failed', {
      provider: 'Twelve Data',
      symbol: requestedSymbol,
      interval,
      description,
    });
    throw new IctAnalysisError(
      `Unable to retrieve ${interval} candles for ${symbol}; Twelve Data failed (${description}).`
    );
  }
}

export async function fetchHistoricalCandles(requestedSymbol, interval, apiKey) {
  if (!INTERVALS.includes(interval)) {
    throw new IctAnalysisError(`Unsupported analysis timeframe: ${interval}.`);
  }
  const symbol = normalizeSymbol(requestedSymbol);
  if (symbol === 'XAUUSDT') {
    const description = 'No verified XAU/USDT historical listing; XAU/USD is a separate instrument';
    console.warn('[ICT analysis] Unsupported instrument', { symbol, description });
    throw new IctAnalysisError(
      `Unsupported instrument ${symbol}: ${description}.`
    );
  }
  if (TWELVE_DATA_METALS[symbol]) {
    return fetchTwelveDataMetalCandles(symbol, interval, apiKey);
  }
  const intervalMinutes = INTERVAL_MINUTES[interval];
  const intervalMs = intervalMinutes * 60_000;
  const now = Date.now();
  const failures = [];
  const quote = ['USDT', 'USDC', 'USD'].find(currency => symbol.endsWith(currency));
  const base = quote ? symbol.slice(0, -quote.length) : '';

  try {
    const url = new URL(`${BINANCE_ENDPOINT}/klines`);
    url.searchParams.set('symbol', symbol);
    url.searchParams.set('interval', interval);
    url.searchParams.set('limit', '150');
    const response = await fetch(url.toString(), { headers: { Accept: 'application/json' } });
    if (!response.ok) {
      failures.push({ provider: 'Binance', description: await responseFailure(response) });
    } else if (!(response.headers.get('content-type') || '').toLowerCase().includes('json')) {
      failures.push({ provider: 'Binance', description: `HTTP ${response.status} non-JSON response` });
    } else {
      const payload = await response.json().catch(() => null);
      const normalized = normalizeCandleRows(
        payload,
        row => Array.isArray(row) && row.length >= 7
          ? {
            timestamp: Number(row[0]),
            open: Number(row[1]),
            high: Number(row[2]),
            low: Number(row[3]),
            close: Number(row[4]),
            volume: Number(row[5]),
          }
          : null,
        intervalMs,
        now
      );
      if (normalized.candles) {
        console.info('[ICT analysis] Candle provider succeeded', {
          provider: 'Binance',
          symbol,
          interval,
          candleCount: normalized.candles.length,
        });
        return normalized.candles;
      }
      failures.push({ provider: 'Binance', description: normalized.description });
    }
  } catch (error) {
    failures.push({
      provider: 'Binance',
      description: `network or JSON error (${error instanceof Error ? error.name : 'request failure'})`,
    });
  }

  const coinbaseGranularities = new Set([60, 300, 900, 3600, 21600, 86400]);
  if (!base || !quote) {
    failures.push({ provider: 'Coinbase', description: 'unsupported or invalid quote currency' });
  } else if (!coinbaseGranularities.has(intervalMinutes * 60)) {
    failures.push({
      provider: 'Coinbase',
      description: `unsupported granularity ${intervalMinutes * 60} seconds`,
    });
  } else {
    try {
      const url = new URL(`https://api.exchange.coinbase.com/products/${base}-${quote}/candles`);
      url.searchParams.set('granularity', String(intervalMinutes * 60));
      const response = await fetch(url.toString(), { headers: { Accept: 'application/json' } });
      if (!response.ok) {
        failures.push({ provider: 'Coinbase', description: await responseFailure(response) });
      } else if (!(response.headers.get('content-type') || '').toLowerCase().includes('json')) {
        failures.push({ provider: 'Coinbase', description: `HTTP ${response.status} non-JSON response` });
      } else {
        const payload = await response.json().catch(() => null);
        const normalized = normalizeCandleRows(
          payload,
          row => Array.isArray(row) && row.length >= 6
            ? {
              timestamp: Number(row[0]) * 1000,
              low: Number(row[1]),
              high: Number(row[2]),
              open: Number(row[3]),
              close: Number(row[4]),
              volume: Number(row[5]),
            }
            : null,
          intervalMs,
          now
        );
        if (normalized.candles) {
          console.info('[ICT analysis] Candle provider succeeded', {
            provider: 'Coinbase',
            symbol,
            interval,
            candleCount: normalized.candles.length,
          });
          return normalized.candles;
        }
        failures.push({ provider: 'Coinbase', description: normalized.description });
      }
    } catch (error) {
      failures.push({
        provider: 'Coinbase',
        description: `network or JSON error (${error instanceof Error ? error.name : 'request failure'})`,
      });
    }
  }

  const krakenPair = `${base === 'BTC' ? 'XBT' : base}${quote || ''}`;
  if (!base || !quote) {
    failures.push({ provider: 'Kraken', description: 'unsupported or invalid quote currency' });
  } else {
    try {
      const url = new URL('https://api.kraken.com/0/public/OHLC');
      url.searchParams.set('pair', krakenPair);
      url.searchParams.set('interval', String(intervalMinutes));
      const response = await fetch(url.toString(), { headers: { Accept: 'application/json' } });
      if (!response.ok) {
        failures.push({ provider: 'Kraken', description: await responseFailure(response) });
      } else if (!(response.headers.get('content-type') || '').toLowerCase().includes('json')) {
        failures.push({ provider: 'Kraken', description: `HTTP ${response.status} non-JSON response` });
      } else {
        const payload = await response.json().catch(() => null);
        const extracted = extractKrakenRows(payload);
        if (!extracted.rows) {
          failures.push({ provider: 'Kraken', description: extracted.description });
        } else {
          const normalized = normalizeCandleRows(
            extracted.rows,
            row => Array.isArray(row) && row.length >= 8
              ? {
                timestamp: Number(row[0]) * 1000,
                open: Number(row[1]),
                high: Number(row[2]),
                low: Number(row[3]),
                close: Number(row[4]),
                volume: Number(row[6]),
              }
              : null,
            intervalMs,
            now
          );
          if (normalized.candles) {
            console.info('[ICT analysis] Candle provider succeeded', {
              provider: 'Kraken',
              symbol,
              krakenPair,
              interval,
              candleCount: normalized.candles.length,
            });
            return normalized.candles;
          }
          failures.push({ provider: 'Kraken', description: normalized.description });
        }
      }
    } catch (error) {
      failures.push({
        provider: 'Kraken',
        description: `network or JSON error (${error instanceof Error ? error.name : 'request failure'})`,
      });
    }
  }

  for (const failure of failures) {
    console.warn('[ICT analysis] Candle provider failed', {
      provider: failure.provider,
      symbol,
      interval,
      description: failure.description,
    });
  }
  throw new IctAnalysisError(
    `Unable to retrieve ${interval} candles for ${symbol}; ` +
    failures.map(failure => `${failure.provider} failed (${failure.description})`).join('; ') +
    '.'
  );
}

export async function fetchIctAnalysis(symbol, apiKey) {
  const entries = await Promise.all(INTERVALS.map(async (interval) => [
    interval,
    await fetchHistoricalCandles(symbol, interval, apiKey),
  ]));
  const candles = Object.fromEntries(entries);
  return analyzeIctCandles(candles);
}

function formatPrice(value) {
  return Number(value).toFixed(2);
}

export function formatIctOpportunity(symbol, opportunity) {
  const direction = opportunity.direction === 'BULLISH' ? 'LONG' : 'SHORT';
  const timestamp = new Date(opportunity.timestamp).toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
  const confluence = opportunity.auditTrail.map((note) => `• ${note}`).join('\n');
  return (
    `🎯 <b>NEW ICT/SMC TRADE OPPORTUNITY</b>\n\n` +
    `• <b>Instrument:</b> ${symbol}\n` +
    `• <b>Direction:</b> <b>${direction}</b>\n` +
    `• <b>Entry Price:</b> ${formatPrice(opportunity.entryPrice)}\n` +
    `• <b>Stop Loss:</b> ${formatPrice(opportunity.stopLoss)}\n` +
    `• <b>Take Profit (4:1 R:R):</b> ${formatPrice(opportunity.takeProfit)}\n` +
    `• <b>Risk-to-Reward Ratio:</b> 4.0:1\n` +
    `• <b>Setup Timeframe:</b> Confirmed 5M / Refined 1M\n` +
    `• <b>Analysis Timestamp:</b> ${timestamp}\n` +
    `• <b>Expiry / Invalidation:</b> Invalidated on break past ${formatPrice(opportunity.stopLoss)} or after 30 minutes\n\n` +
    `<b>Confluence Hierarchy:</b>\n` +
    `• 1. 4H Bias\n• 2. 1H AOI\n• 3. 15M Setup\n• 4. 5M Confirmation\n• 5. 1M Refinement\n\n` +
    `${confluence}\n\n` +
    `<i>Select an option below to enter virtual paper trade, accept, or view analysis.</i>`
  );
}
