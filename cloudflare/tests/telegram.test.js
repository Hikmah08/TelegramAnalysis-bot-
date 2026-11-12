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
          async run() {
            if (sql.includes('INSERT INTO dashboard_auth_tokens')) {
              const [tokenHash, userId, createdAt, expiresAt] = params;
              self.tables.dashboard_auth_tokens.set(tokenHash, { tokenHash, userId, createdAt, expiresAt });
            }
            return { success: true };
          },
          async first() {
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
    return statements.map(() => ({ success: true }));
  }
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
    if (url.startsWith('https://api.binance.com/api/v3/ticker/24hr')) {
      return Response.json({
        lastPrice: '65000.25',
        highPrice: '66000.00',
        lowPrice: '64000.00',
        closeTime: Date.now(),
      });
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
    { command: '/analyze BTCUSDT', expected: /Market Snapshot: BTCUSDT.*65000\.25/s },
    { command: '/analyze@TradingBot ETHUSDT', expected: /Market Snapshot: ETHUSDT/s },
    { command: '/scan ETHUSDT', expected: /Market Snapshot: ETHUSDT/s },
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
