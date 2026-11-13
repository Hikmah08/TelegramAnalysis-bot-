import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { diagnoseTelegramBotToken, handleTelegramUpdate, sendTelegramMessage } from '../src/telegram.js';

// Mock in-memory D1 implementation
class MockD1 {
  constructor() {
    this.tables = {
      users: new Map(),
      user_balances: new Map(),
      paper_positions: new Map(),
      user_watchlist: new Map(),
      user_settings: new Map(),
      signals_queue: new Map(),
      dashboard_auth_tokens: new Map(),
      balance_transactions: [],
      outbox_notifications: [],
    };
  }

  prepare(sql) {
    const self = this;
    return {
      bind(...params) {
        return {
          sql,
          params,
          async run() {
            if (sql.includes('INSERT INTO dashboard_auth_tokens')) {
              const [tokenHash, userId, createdAt, expiresAt] = params;
              self.tables.dashboard_auth_tokens.set(tokenHash, { tokenHash, userId, createdAt, expiresAt });
            } else if (sql.includes('INSERT INTO signals_queue')) {
              const [signalId, userId, symbol, direction, entryPrice, stopLoss, takeProfit, auditNotes, createdAt, expiresAt] = params;
              self.tables.signals_queue.set(signalId, {
                signal_id: signalId,
                user_id: userId,
                symbol,
                direction,
                entry_price: entryPrice,
                stop_loss: stopLoss,
                take_profit: takeProfit,
                status: 'PENDING',
                audit_notes: auditNotes,
                created_at: createdAt,
                expires_at: expiresAt,
              });
            } else if (sql.includes('UPDATE signals_queue SET status = ?')) {
              const [status, signalId, userId, expectedStatus] = params;
              const signal = self.tables.signals_queue.get(signalId);
              if (signal?.user_id === userId && signal.status === expectedStatus) signal.status = status;
            }
            return { success: true };
          },
          async first() {
            if (sql.includes('FROM signals_queue WHERE signal_id = ? AND user_id = ?')) {
              const signal = self.tables.signals_queue.get(params[0]);
              return signal?.user_id === params[1] ? signal : null;
            }
            if (sql.includes('FROM user_settings WHERE user_id = ? AND key = ?')) {
              return self.tables.user_settings.get(`${params[0]}:${params[1]}`) || null;
            }
            if (sql.includes('SELECT * FROM user_balances WHERE user_id = ?')) {
              const uId = params[0];
              return self.tables.user_balances.get(uId) || null;
            }
            if (sql.includes('SELECT SUM(unrealized_pnl)')) {
              return { total_unrealized: 0.0 };
            }
            return null;
          },
          async all() {
            if (sql.includes('FROM user_watchlist WHERE user_id = ? AND is_active = 1')) {
              const uId = params[0];
              return {
                results: Array.from(self.tables.user_watchlist.values())
                  .filter(item => item.user_id === uId && item.is_active === 1),
              };
            }
            if (sql.includes('FROM paper_positions WHERE user_id = ? AND status != "OPEN"')) {
              const uId = params[0];
              return {
                results: Array.from(self.tables.paper_positions.values())
                  .filter(p => p.user_id === uId && p.status !== 'OPEN'),
              };
            }
            if (sql.includes('FROM paper_positions WHERE user_id = ? AND status = "OPEN"')) {
              const uId = params[0];
              const list = Array.from(self.tables.paper_positions.values()).filter(p => p.user_id === uId && p.status === 'OPEN');
              return { results: list };
            }
            return { results: [] };
          }
        };
      }
    };
  }

  async batch(statements) {
    const [claim, insert] = statements;
    const [signalId, userId, nowIso, , maxPositions, balanceUserId] = claim.params;
    const signal = this.tables.signals_queue.get(signalId);
    const balance = this.tables.user_balances.get(balanceUserId);
    const openCount = Array.from(this.tables.paper_positions.values())
      .filter(position => position.user_id === userId && position.status === 'OPEN').length;
    const claimed = signal?.user_id === userId
      && signal.status === 'PENDING'
      && signal.expires_at > nowIso
      && signal.entry_price !== signal.stop_loss
      && openCount < maxPositions
      && balance?.current_balance > 0;
    if (claimed) signal.status = 'APPROVED';

    let positionOpened = false;
    if (claimed && insert.params[3] === signalId && insert.params[4] === userId) {
      const positionSize = (balance.current_balance * insert.params[0] / 100)
        / Math.abs(signal.entry_price - signal.stop_loss);
      const positionId = this.tables.paper_positions.size + 1;
      this.tables.paper_positions.set(positionId, {
        position_id: positionId,
        user_id: userId,
        symbol: signal.symbol,
        direction: signal.direction,
        entry_price: signal.entry_price,
        stop_loss: signal.stop_loss,
        take_profit: signal.take_profit,
        position_size: positionSize,
        status: 'OPEN',
      });
      positionOpened = true;
    }

    return [
      { success: true, meta: { changes: claimed ? 1 : 0 } },
      { success: true, meta: { changes: positionOpened ? 1 : 0 } },
    ];
  }
}

function flatBinanceCandles(interval = '1m', count = 150) {
  const intervalMs = {
    '1m': 60_000,
    '5m': 300_000,
    '15m': 900_000,
    '1h': 3_600_000,
    '4h': 14_400_000,
  }[interval];
  const end = Math.floor(Date.now() / intervalMs) * intervalMs - intervalMs;
  return Array.from({ length: count }, (_, index) => {
    const timestamp = end - (count - index - 1) * intervalMs;
    return [timestamp, '100', '101', '99', '100', '10', timestamp + intervalMs - 1];
  });
}

function ictPattern(intervalMs, start) {
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
  return rows.map(([high, low, close], index) => {
    const timestamp = start + index * intervalMs;
    return [timestamp, String(close), String(high), String(low), String(close), '10', timestamp + intervalMs - 1];
  });
}

function ictHourlyAoi(intervalMs, start) {
  return Array.from({ length: 9 }, (_, index) => {
    const timestamp = start + index * intervalMs;
    const high = index === 0 ? 98 : 101;
    const low = index === 0 ? 97 : index === 1 ? 98.5 : 99;
    const openClose = index === 0 ? '97.5' : '100';
    return [timestamp, openClose, String(high), String(low), openClose, '10', timestamp + intervalMs - 1];
  });
}

test('Telegram Webhook - Registration and /start Command', async () => {
  const db = new MockD1();
  const env = {
    DB: db,
    TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET',
    DEFAULT_STARTING_BALANCE: '10000.0',
  };

  // Mock global fetch for Telegram API sendMessage
  const originalFetch = globalThis.fetch;
  const originalInfo = console.info;
  const sentRequests = [];
  const infoLogs = [];

  globalThis.fetch = async (url, opts) => {
    if (url.includes('api.telegram.org')) {
      sentRequests.push({ url, options: opts, payload: JSON.parse(opts.body) });
      return Response.json({ ok: true, result: { message_id: sentRequests.length } });
    }
    return { ok: false };
  };
  console.info = (...args) => infoLogs.push(args);

  try {
    const update = {
      update_id: 1,
      message: {
        message_id: 100,
        from: { id: 12345678, is_bot: false, first_name: 'Alex' },
        chat: { id: 12345678, type: 'private' },
        text: '/start',
        date: 1699000000,
      }
    };

    const res = await handleTelegramUpdate(update, env);
    assert.equal(res.status, 200);
    assert.equal(sentRequests.length, 1);
    assert.equal(sentRequests[0].url, 'https://api.telegram.org/bot12345:MOCK_TOKEN_SECRET/sendMessage');
    assert.equal(sentRequests[0].options.method, 'POST');
    assert.equal(sentRequests[0].payload.chat_id, '12345678');
    assert.match(sentRequests[0].payload.text, /Welcome/);
    assert.match(sentRequests[0].payload.text, /\$10,000/);

    const addressedStart = await handleTelegramUpdate({
      ...update,
      update_id: 4,
      message: { ...update.message, text: '/start@TradingBot hello' },
    }, env);
    assert.equal(addressedStart.status, 200);
    assert.equal(sentRequests.length, 2);
    assert.match(sentRequests[1].payload.text, /Welcome/);
    assert.ok(infoLogs.some(([message]) => message === '[Telegram] Processing update; bot token configured=true'));
    assert.ok(infoLogs.some(([message]) => message === '[Telegram] Received command /start'));
    assert.ok(infoLogs.some(([message, result]) => message === '[Telegram API]'
      && result.httpStatus === 200 && result.description === '' && result.ok === true));
  } finally {
    globalThis.fetch = originalFetch;
    console.info = originalInfo;
  }
});

test('Telegram commands - Every supported command is routed', async () => {
  const originalFetch = globalThis.fetch;
  const sentMessages = [];
  globalThis.fetch = async (url, options) => {
    if (url.startsWith('https://api.telegram.org/')) {
      sentMessages.push(JSON.parse(options.body));
      return Response.json({ ok: true, result: { message_id: sentMessages.length } });
    }
    if (url.startsWith('https://api.binance.com/api/v3/klines')) {
      return Response.json(flatBinanceCandles(new URL(url).searchParams.get('interval')));
    }
    throw new Error('Unexpected mocked fetch URL');
  };

  const cases = [
    { command: '/start', expected: /Welcome/ },
    { command: '/help', expected: /Available Commands/ },
    { command: '/menu', expected: /Available Commands/ },
    { command: '/menu@TradingBot', expected: /Available Commands/ },
    { command: '/balance', expected: /Account Balance/ },
    { command: '/account', expected: /Account Balance/ },
    { command: '/performance', expected: /Account Balance/ },
    { command: '/positions', expected: /no open paper positions/i },
    { command: '/history', expected: /No completed paper trades/ },
    { command: '/watchlist', expected: /watchlist is empty/i },
    { command: '/dashboard', expected: /One-Click Login/ },
    { command: '/analyze', expected: /Usage:.*analyze SYMBOL/s },
    { command: '/analyze BTCUSDT', expected: /No qualified ICT\/SMC setup is available.*BTCUSDT/i },
    { command: '/analyze@TradingBot ETHUSDT', expected: /No qualified ICT\/SMC setup is available.*ETHUSDT/i },
    { command: '/scan ETHUSDT', expected: /No qualified ICT\/SMC setup is available.*ETHUSDT/i },
  ];

  try {
    for (const [index, testCase] of cases.entries()) {
      const before = sentMessages.length;
      const res = await handleTelegramUpdate({
        update_id: 100 + index,
        message: {
          message_id: 200 + index,
          from: { id: 12345678, is_bot: false, first_name: 'Alex' },
          chat: { id: 12345678, type: 'private' },
          text: testCase.command,
          date: 1699000000,
        },
      }, {
        DB: new MockD1(),
        TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET',
        DEFAULT_STARTING_BALANCE: '10000.0',
      });

      assert.equal(res.status, 200, `${testCase.command} should be processed`);
      assert.equal(sentMessages.length, before + 1, `${testCase.command} should send one reply`);
      assert.match(sentMessages.at(-1).text, testCase.expected, `${testCase.command} reply`);
      assert.doesNotMatch(sentMessages.at(-1).text, /Unrecognized command/i);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Telegram /analyze - Runs multi-timeframe analysis instead of returning a price snapshot', async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    if (url.startsWith('https://api.binance.com/api/v3/klines')) {
      return Response.json(flatBinanceCandles(new URL(url).searchParams.get('interval')));
    }
    if (url.startsWith('https://api.telegram.org/')) {
      return Response.json({ ok: true, result: { message_id: 1 } });
    }
    throw new Error('Unexpected mocked fetch URL');
  };

  try {
    const res = await handleTelegramUpdate({
      update_id: 130,
      message: {
        message_id: 230,
        from: { id: 12345678, is_bot: false, first_name: 'Alex' },
        chat: { id: 12345678, type: 'private' },
        text: '/analyze BTCUSDT',
        date: 1699000000,
      },
    }, {
      DB: new MockD1(),
      TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET',
    });

    assert.equal(res.status, 200);
    const telegramRequest = requests.find(request => request.url.includes('api.telegram.org'));
    const reply = JSON.parse(telegramRequest.options.body).text;
    assert.match(reply, /No qualified ICT\/SMC setup is available.*BTCUSDT/i);
    assert.equal(requests.filter(request => request.url.includes('/klines')).length, 5);
    assert.ok(requests.every(request => !request.url.includes('ticker/24hr')));
    assert.deepEqual(
      requests.filter(request => request.url.includes('/klines')).map(request => new URL(request.url).searchParams.get('interval')).sort(),
      ['1m', '15m', '1h', '4h', '5m'].sort()
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

    test('Telegram Analysis menu callback - Offers built-in crypto instruments', async () => {
      const originalFetch = globalThis.fetch;
      const requests = [];
      globalThis.fetch = async (url, options) => {
        requests.push({ url, payload: JSON.parse(options.body) });
        return Response.json({ ok: true, result: true });
      };

      try {
        await handleTelegramUpdate({
          update_id: 135,
          callback_query: {
            id: 'analysis-menu',
            from: { id: 12345678, first_name: 'Alex' },
            data: 'menu_analyze_menu',
          },
        }, { DB: new MockD1(), TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' });

        const menuMessage = requests.find(request => request.url.endsWith('/sendMessage')).payload;
        assert.match(menuMessage.text, /full multi-timeframe ICT\/SMC analysis/);
        assert.deepEqual(
          menuMessage.reply_markup.inline_keyboard.map(row => row[0].text),
          ['BTCUSDT', 'ETHUSDT']
        );
        assert.ok(requests.some(request => request.url.endsWith('/answerCallbackQuery')
          && request.payload.text === 'Choose an instrument.'));
      } finally {
        globalThis.fetch = originalFetch;
      }
    });

    test('Telegram analysis - Persists a qualified signal and sends exactly the three requested buttons', async () => {
      const originalFetch = globalThis.fetch;
      const now = Date.now();
      const intervalMs = { '4h': 14_400_000, '1h': 3_600_000, '15m': 900_000, '5m': 300_000, '1m': 60_000 };
      const starts = {
        '4h': now - 60 * 3_600_000,
        '1h': now - 12 * 3_600_000,
        '15m': now - 8 * 3_600_000,
        '5m': now - 4 * 3_600_000,
        '1m': now - 2 * 3_600_000,
      };
      const db = new MockD1();
      const telegramRequests = [];
      globalThis.fetch = async (url, options) => {
        if (url.startsWith('https://api.telegram.org/')) {
          telegramRequests.push(JSON.parse(options.body));
          return Response.json({ ok: true, result: { message_id: telegramRequests.length } });
        }
        if (url.startsWith('https://api.binance.com/api/v3/klines')) {
          const interval = new URL(url).searchParams.get('interval');
          const rows = interval === '1h'
            ? ictHourlyAoi(intervalMs[interval], starts[interval])
            : ictPattern(intervalMs[interval], starts[interval]);
          return Response.json(rows);
        }
        throw new Error('Unexpected mocked fetch URL');
      };

      try {
        const result = await handleTelegramUpdate({
          update_id: 140,
          message: {
            message_id: 240,
            from: { id: 12345678, is_bot: false, first_name: 'Alex' },
            chat: { id: 12345678, type: 'private' },
            text: '/analyze BTCUSDT',
            date: 1699000000,
          },
        }, { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' });

        assert.equal(result.status, 200);
        assert.equal(db.tables.signals_queue.size, 1);
        const signal = Array.from(db.tables.signals_queue.values())[0];
        assert.equal(signal.user_id, '12345678');
        assert.equal(signal.status, 'PENDING');
        assert.deepEqual(JSON.parse(signal.audit_notes).some(note => note.startsWith('4H directional bias:')), true);
        const alert = telegramRequests.find(request => request.reply_markup?.inline_keyboard?.[0]?.length === 3);
        assert.ok(alert);
        assert.match(alert.text, /NEW ICT\/SMC TRADE OPPORTUNITY/);
        assert.deepEqual(alert.reply_markup.inline_keyboard[0].map(button => button.text), [
          'Enter Paper Trade',
          'Accept',
          'View Analysis',
        ]);
      } finally {
        globalThis.fetch = originalFetch;
      }
    });

    test('Telegram approval callbacks - Enter Paper Trade and Accept share a single user-scoped execution', async () => {
      const originalFetch = globalThis.fetch;
      const db = new MockD1();
      db.tables.user_balances.set('user-1', { user_id: 'user-1', current_balance: 10000 });
      db.tables.signals_queue.set('signal-1', {
        signal_id: 'signal-1',
        user_id: 'user-1',
        symbol: 'BTCUSDT',
        direction: 'BULLISH',
        entry_price: 100,
        stop_loss: 90,
        take_profit: 140,
        status: 'PENDING',
        audit_notes: '[]',
        expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString(),
      });
      const telegramRequests = [];
      globalThis.fetch = async (url, options) => {
        telegramRequests.push({ url, payload: JSON.parse(options.body) });
        return Response.json({ ok: true, result: true });
      };
      const env = { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' };

      try {
        await handleTelegramUpdate({
          update_id: 149,
          callback_query: {
            id: 'unauthorized-click',
            from: { id: 'other-user', first_name: 'Eve' },
            data: 'approve_signal-1',
          },
        }, env);
        assert.equal(db.tables.paper_positions.size, 0);
        assert.equal(db.tables.signals_queue.get('signal-1').status, 'PENDING');

        for (const [index, action] of ['approve_signal-1', 'accept_signal-1'].entries()) {
          const response = await handleTelegramUpdate({
            update_id: 150 + index,
            callback_query: {
              id: `callback-${index}`,
              from: { id: 'user-1', first_name: 'Alex' },
              data: action,
            },
          }, env);
          assert.equal(response.status, 200);
        }
        assert.equal(db.tables.signals_queue.get('signal-1').status, 'APPROVED');
        assert.equal(db.tables.paper_positions.size, 1);
        const position = Array.from(db.tables.paper_positions.values())[0];
        assert.equal(position.user_id, 'user-1');
        assert.equal(position.position_size, 10);
        assert.equal(telegramRequests.filter(request => request.url.endsWith('/sendMessage')).length, 1);
        assert.ok(telegramRequests.some(request => request.url.endsWith('/answerCallbackQuery')
          && request.payload.text === 'Trade already approved.'));
      } finally {
        globalThis.fetch = originalFetch;
      }
    });

    test('Telegram approval callback - Respects the user-specific open-position limit', async () => {
      const originalFetch = globalThis.fetch;
      const db = new MockD1();
      db.tables.user_balances.set('user-1', { user_id: 'user-1', current_balance: 10000 });
      db.tables.user_settings.set('user-1:max_open_positions', { value: '1' });
      db.tables.paper_positions.set(1, { user_id: 'user-1', status: 'OPEN' });
      db.tables.signals_queue.set('signal-cap', {
        signal_id: 'signal-cap',
        user_id: 'user-1',
        symbol: 'BTCUSDT',
        direction: 'BULLISH',
        entry_price: 100,
        stop_loss: 90,
        take_profit: 140,
        status: 'PENDING',
        audit_notes: '[]',
        expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString(),
      });
      globalThis.fetch = async () => Response.json({ ok: true, result: true });

      try {
        await handleTelegramUpdate({
          update_id: 155,
          callback_query: {
            id: 'position-cap',
            from: { id: 'user-1', first_name: 'Alex' },
            data: 'approve_signal-cap',
          },
        }, { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' });

        assert.equal(db.tables.paper_positions.size, 1);
        assert.equal(db.tables.signals_queue.get('signal-cap').status, 'PENDING');
      } finally {
        globalThis.fetch = originalFetch;
      }
    });

    test('Telegram View Analysis callback - Returns only the owning user’s stored audit trail', async () => {
      const originalFetch = globalThis.fetch;
      const db = new MockD1();
      db.tables.signals_queue.set('signal-private', {
        signal_id: 'signal-private',
        user_id: 'owner',
        symbol: 'ETHUSDT',
        direction: 'BEARISH',
        entry_price: 2000,
        stop_loss: 2050,
        take_profit: 1800,
        status: 'PENDING',
        audit_notes: JSON.stringify(['4H directional bias: BEARISH.', '1H AOI: FVG.']),
        expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString(),
      });
      const sentMessages = [];
      globalThis.fetch = async (url, options) => {
        const payload = JSON.parse(options.body);
        if (url.endsWith('/sendMessage')) sentMessages.push(payload.text);
        return Response.json({ ok: true, result: true });
      };

      try {
        for (const [index, userId] of ['owner', 'other-user'].entries()) {
          await handleTelegramUpdate({
            update_id: 160 + index,
            callback_query: {
              id: `view-${index}`,
              from: { id: userId, first_name: 'Trader' },
              data: 'view_analysis_signal-private',
            },
          }, { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' });
        }
        assert.equal(sentMessages.length, 1);
        assert.match(sentMessages[0], /4H directional bias: BEARISH/);
        assert.match(sentMessages[0], /1H AOI: FVG/);
        assert.doesNotMatch(sentMessages[0], /Unrecognized command/);
      } finally {
        globalThis.fetch = originalFetch;
      }
    });

test('Telegram /history and /watchlist - Return the user stored data', async () => {
  const originalFetch = globalThis.fetch;
  const sentMessages = [];
  globalThis.fetch = async (url, options) => {
    sentMessages.push(JSON.parse(options.body));
    return Response.json({ ok: true, result: { message_id: sentMessages.length } });
  };

  try {
    const db = new MockD1();
    db.tables.paper_positions.set(1, {
      position_id: 1,
      user_id: '12345678',
      symbol: 'BTCUSDT',
      direction: 'BULLISH',
      entry_price: 60000,
      exit_price: 61000,
      status: 'CLOSED_TP',
      pnl: 100,
    });
    db.tables.user_watchlist.set('12345678:BTCUSDT', {
      user_id: '12345678',
      symbol: 'BTCUSDT',
      asset_class: 'CRYPTO',
      is_active: 1,
    });

    for (const [index, command] of ['/history', '/watchlist'].entries()) {
      await handleTelegramUpdate({
        update_id: 120 + index,
        message: {
          message_id: 220 + index,
          from: { id: 12345678, is_bot: false, first_name: 'Alex' },
          chat: { id: 12345678, type: 'private' },
          text: command,
          date: 1699000000,
        },
      }, { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' });
    }

    assert.match(sentMessages[0].text, /BTCUSDT.*CLOSED_TP.*\+\$100\.00/s);
    assert.match(sentMessages[1].text, /BTCUSDT.*CRYPTO/s);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Telegram sendMessage - Logs safe API failures without exposing the bot token', async () => {
  const botToken = '12345:ABCDEFGHIJKLMNOPQRSTUVWX';
  const originalFetch = globalThis.fetch;
  const originalInfo = console.info;
  const originalError = console.error;
  const errorLogs = [];
  const requestUrls = [];
  globalThis.fetch = async url => {
    requestUrls.push(url);
    return Response.json({
    ok: false,
    description: `Unauthorized token ${botToken}`,
    }, { status: 401 });
  };
  console.error = (...args) => errorLogs.push(args);
  console.info = (...args) => errorLogs.push(args);

  try {
    const sent = await sendTelegramMessage(botToken, 'private-chat', 'Test message');

    assert.equal(sent, false);
    assert.equal(requestUrls.length, 2);
    assert.ok(requestUrls[0].endsWith('/sendMessage'));
    assert.ok(requestUrls[1].endsWith('/getMe'));
    assert.ok(errorLogs.every(args => args[0] === '[Telegram API]'));
    assert.ok(errorLogs.every(([, result]) => result.httpStatus === 401
      && result.description.includes('[redacted]') && result.ok === false));
    assert.equal(JSON.stringify(errorLogs).includes(botToken), false);
    assert.equal(JSON.stringify(errorLogs).includes('api.telegram.org'), false);
  } finally {
    globalThis.fetch = originalFetch;
    console.error = originalError;
    console.info = originalInfo;
  }
});

test('Telegram sendMessage - Normalizes bot-prefixed and whitespace-padded tokens', async () => {
  const originalFetch = globalThis.fetch;
  const requestUrls = [];
  globalThis.fetch = async url => {
    requestUrls.push(url);
    return Response.json({ ok: true }, { status: 200 });
  };

  try {
    assert.equal(await sendTelegramMessage('12345:ABCDEFGHIJKLMNOPQRSTUVWX', 123, 'test'), true);
    assert.equal(await sendTelegramMessage('  bot12345:ABCDEFGHIJKLMNOPQRSTUVWX  ', 123, 'test'), true);
    assert.deepEqual(requestUrls, [
      'https://api.telegram.org/bot12345:ABCDEFGHIJKLMNOPQRSTUVWX/sendMessage',
      'https://api.telegram.org/bot12345:ABCDEFGHIJKLMNOPQRSTUVWX/sendMessage',
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Telegram getMe - Uses the same normalized API URL and safe diagnostics', async () => {
  const originalFetch = globalThis.fetch;
  const originalInfo = console.info;
  const requestUrls = [];
  const logs = [];
  globalThis.fetch = async url => {
    requestUrls.push(url);
    return Response.json({ ok: true, result: { id: 12345 } }, { status: 200 });
  };
  console.info = (...args) => logs.push(args);

  try {
    assert.equal(await diagnoseTelegramBotToken('  bot12345:ABCDEFGHIJKLMNOPQRSTUVWX  '), true);
    assert.deepEqual(requestUrls, [
      'https://api.telegram.org/bot12345:ABCDEFGHIJKLMNOPQRSTUVWX/getMe',
    ]);
    assert.deepEqual(logs, [['[Telegram API]', {
      httpStatus: 200,
      description: '',
      ok: true,
    }]]);
    assert.equal(JSON.stringify(logs).includes('api.telegram.org'), false);
  } finally {
    globalThis.fetch = originalFetch;
    console.info = originalInfo;
  }
});

test('Telegram API - Rejects malformed bot token configuration without a request', async () => {
  const originalFetch = globalThis.fetch;
  const originalError = console.error;
  const logs = [];
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    throw new Error('Should not make a request');
  };
  console.error = (...args) => logs.push(args);

  try {
    assert.equal(await sendTelegramMessage('bot-not-a-token', 123, 'test'), false);
    assert.equal(fetchCalled, false);
    assert.deepEqual(logs, [['[Telegram API]', {
      httpStatus: null,
      description: 'Malformed Telegram bot token configuration',
      ok: false,
    }]]);
  } finally {
    globalThis.fetch = originalFetch;
    console.error = originalError;
  }
});

test('Telegram Webhook - Callback queries are acknowledged', async () => {
  const originalFetch = globalThis.fetch;
  let capturedRequest;
  globalThis.fetch = async (url, options) => {
    capturedRequest = { url, payload: JSON.parse(options.body) };
    return Response.json({ ok: true, result: true });
  };

  try {
    const res = await handleTelegramUpdate({
      update_id: 5,
      callback_query: {
        id: 'callback-id',
        from: { id: 12345678, first_name: 'Alex' },
        data: 'unsupported_action',
      },
    }, {
      DB: new MockD1(),
      TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET',
    });

    assert.equal(res.status, 200);
    assert.equal(capturedRequest.url, 'https://api.telegram.org/bot12345:MOCK_TOKEN_SECRET/answerCallbackQuery');
    assert.deepEqual(capturedRequest.payload, { callback_query_id: 'callback-id' });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Telegram Webhook - Multi-User Balance Isolation', async () => {
  const db = new MockD1();
  // Set distinct balance for User A
  db.tables.user_balances.set('user_A', {
    user_id: 'user_A',
    current_balance: 14500.0,
    realized_pnl: 4500.0,
    total_trades: 5,
  });

  const env = {
    DB: db,
    TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET',
  };

  const originalFetch = globalThis.fetch;
  let capturedMessage = '';

  globalThis.fetch = async (url, opts) => {
    if (url.includes('api.telegram.org')) {
      const p = JSON.parse(opts.body);
      capturedMessage = p.text;
      return Response.json({ ok: true }, { status: 200 });
    }
    return { ok: false };
  };

  try {
    const update = {
      update_id: 2,
      message: {
        message_id: 101,
        from: { id: 'user_A', is_bot: false, first_name: 'User A' },
        chat: { id: 9999, type: 'private' },
        text: '/balance',
        date: 1699000000,
      }
    };

    await handleTelegramUpdate(update, env);
    assert.match(capturedMessage, /\$14500\.00/);
    assert.match(capturedMessage, /\+\$4500\.00/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Telegram Webhook - Dashboard Login Token Is Stored Hashed', async () => {
  const db = new MockD1();
  const env = {
    DB: db,
    TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET',
  };
  const originalFetch = globalThis.fetch;
  let sentMessage = '';

  globalThis.fetch = async (url, opts) => {
    if (url.includes('api.telegram.org')) {
      sentMessage = JSON.parse(opts.body).text;
      return Response.json({ ok: true }, { status: 200 });
    }
    return { ok: false };
  };

  try {
    await handleTelegramUpdate({
      update_id: 3,
      message: {
        message_id: 102,
        from: { id: 12345678, is_bot: false, first_name: 'Alex' },
        chat: { id: 12345678, type: 'private' },
        text: '/dashboard',
        date: 1699000000,
      },
    }, env);

    const rawToken = sentMessage.match(/token=([a-f0-9]{64})/)?.[1];
    assert.ok(rawToken, 'dashboard message should contain the one-time token');

    const tokenHash = createHash('sha256').update(rawToken).digest('hex');
    assert.equal(db.tables.dashboard_auth_tokens.has(tokenHash), true);
    assert.equal(db.tables.dashboard_auth_tokens.has(rawToken), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
