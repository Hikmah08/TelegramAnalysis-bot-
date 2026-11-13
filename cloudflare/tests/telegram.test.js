import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import {
  diagnoseTelegramBotToken,
  handleTelegramUpdate,
  registerTelegramCommands,
  sendTelegramMessage,
} from '../src/telegram.js';

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
      bot_activity: [],
      dashboard_sessions: new Map(),
      admin_audit_log: [],
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
            if (sql.includes('INSERT INTO users')) {
              const [userId, chatId, username, firstName, role, registeredAt, lastActiveAt] = params;
              if (!self.tables.users.has(userId)) {
                self.tables.users.set(userId, {
                  user_id: userId, chat_id: chatId, username, first_name: firstName,
                  role, status: 'ACTIVE', registered_at: registeredAt, last_active_at: lastActiveAt,
                });

                return { success: true, meta: { changes: 1 } };
              }
              return { success: true, meta: { changes: 0 } };
            } else if (sql.includes('UPDATE users')) {
              const [chatId, username, firstName, cutoff, now, isAdmin, userId] = params;
              const user = self.tables.users.get(userId);
              if (user) {
                user.chat_id = chatId;
                user.username = username ?? user.username;
                user.first_name = firstName ?? user.first_name;
                if (user.last_active_at <= cutoff) user.last_active_at = now;
                if (isAdmin === 1) user.role = 'ADMIN';
              }
            } else if (sql.includes('INSERT INTO user_settings')) {
              const [userId, key, value] = params;
              self.tables.user_settings.set(`${userId}:${key}`, { user_id: userId, key, value });
            } else if (sql.includes('DELETE FROM user_settings')) {
              self.tables.user_settings.delete(`${params[0]}:${params[1]}`);
            } else if (sql.includes('INSERT INTO user_watchlist')) {
              const [userId, symbol, assetClass, addedAt] = params;
              self.tables.user_watchlist.set(`${userId}:${symbol}`, {
                user_id: userId, symbol, asset_class: assetClass, is_active: 1, added_at: addedAt,
              });
            } else if (sql.includes('UPDATE user_watchlist SET is_active = 0')) {
              const [userId, symbol] = params;
              const item = self.tables.user_watchlist.get(`${userId}:${symbol}`);
              if (!item || item.is_active !== 1) return { success: true, meta: { changes: 0 } };
              item.is_active = 0;
              return { success: true, meta: { changes: 1 } };
            } else if (sql.includes('INSERT INTO bot_activity')) {
              const [userId, commandName, eventType, createdAt] = sql.includes('VALUES (?, NULL, ?, ?)')
                ? [params[0], null, params[1], params[2]]
                : params;
              self.tables.bot_activity.push({
                user_id: userId, command_name: commandName, event_type: eventType, created_at: createdAt,
              });
            } else if (sql.includes('INSERT INTO dashboard_auth_tokens')) {
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
            if (sql.includes('SELECT status, role FROM users WHERE user_id = ?')) {
              const user = self.tables.users.get(params[0]);
              return user ? { status: user.status, role: user.role } : null;
            }
            if (sql.includes('FROM signals_queue WHERE signal_id = ? AND user_id = ?')) {
              const signal = self.tables.signals_queue.get(params[0]);
              return signal?.user_id === params[1] ? signal : null;
            }
            if (sql.includes('FROM user_settings WHERE user_id = ? AND key = ?')) {
              return self.tables.user_settings.get(`${params[0]}:${params[1]}`) || null;
            }
            if (sql.includes('FROM user_watchlist WHERE user_id = ? AND symbol = ? AND is_active = 1')) {
              const item = self.tables.user_watchlist.get(`${params[0]}:${params[1]}`);
              return item?.is_active === 1 ? item : null;
            }
            if (sql.includes('SELECT COUNT(*) AS count FROM user_watchlist')) {
              const userId = params[0];
              return {
                count: Array.from(self.tables.user_watchlist.values())
                  .filter(item => item.user_id === userId && item.is_active === 1).length,
              };
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

test('default Telegram command menu excludes /admin', async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, payload: JSON.parse(options.body) });
    return Response.json({ ok: true, result: true });
  };
  try {
    assert.equal(await registerTelegramCommands('12345:MOCK_TOKEN_SECRET', ''), true);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].payload.scope.type, 'default');
    assert.equal(requests[0].payload.commands.some(command => command.command === 'admin'), false);
    assert.deepEqual(requests[0].payload.commands.map(command => command.command), [
      'start', 'help', 'menu', 'price', 'analyze', 'scan', 'balance',
      'account', 'performance', 'positions', 'history', 'watchlist', 'dashboard',
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('authorized Telegram admin receives /admin only in their private-chat command scope', async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, payload: JSON.parse(options.body) });
    return Response.json({ ok: true, result: true });
  };
  try {
    assert.equal(await registerTelegramCommands('12345:MOCK_TOKEN_SECRET', '8675309'), true);
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[0].payload.scope, { type: 'default' });
    assert.equal(requests[0].payload.commands.some(command => command.command === 'admin'), false);
    assert.deepEqual(requests[1].payload.scope, { type: 'chat', chat_id: '8675309' });
    assert.deepEqual(
      requests[1].payload.commands.find(command => command.command === 'admin'),
      { command: 'admin', description: 'Open admin dashboard' }
    );
    assert.equal(requests[1].payload.commands.length, requests[0].payload.commands.length + 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('multiple authorized Telegram admins each receive a private-chat /admin command scope', async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, payload: JSON.parse(options.body) });
    return Response.json({ ok: true, result: true });
  };
  try {
    assert.equal(await registerTelegramCommands('12345:MOCK_TOKEN_SECRET', '112233, 445566, nope, 112233'), true);
    assert.equal(requests.length, 3);
    assert.deepEqual(requests.map(request => request.payload.scope), [
      { type: 'default' },
      { type: 'chat', chat_id: '112233' },
      { type: 'chat', chat_id: '445566' },
    ]);
    assert.equal(requests[0].payload.commands.some(command => command.command === 'admin'), false);
    for (const request of requests.slice(1)) {
      assert.deepEqual(
        request.payload.commands.find(command => command.command === 'admin'),
        { command: 'admin', description: 'Open admin dashboard' }
      );
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

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
    assert.deepEqual(db.tables.bot_activity.map(event => [event.event_type, event.command_name]), [
      ['USER_JOINED', null],
      ['COMMAND_USED', '/start'],
      ['COMMAND_USED', '/start'],
    ]);
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
    { command: '/price', expected: /Which trading pair would you like to check/i },
    { command: '/admin', expected: /Admin access is not enabled/ },
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

test('Telegram activity stores only command names without arguments', async () => {
  const db = new MockD1();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({ ok: true });
  try {
    await handleTelegramUpdate({
      update_id: 66,
      message: {
        message_id: 66,
        from: { id: 654321, is_bot: false, first_name: 'Activity', username: 'activity_user' },
        chat: { id: 654321, type: 'private' },
        text: '/balance private-argument-must-not-be-recorded',
        date: 1699000000,
      },
    }, { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' });

    assert.deepEqual(db.tables.bot_activity.map(event => [event.event_type, event.command_name]), [
      ['USER_JOINED', null],
      ['COMMAND_USED', '/balance'],
    ]);
    assert.equal(JSON.stringify(db.tables.bot_activity).includes('private-argument'), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Telegram /admin only issues an administrator link to an allowlisted ID', async () => {
  const db = new MockD1();
  const env = {
    DB: db,
    TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET',
    TELEGRAM_ADMIN_IDS: '654321',
    DASHBOARD_URL: 'https://worker.example',
  };
  const originalFetch = globalThis.fetch;
  let reply = '';
  let replySentAt = 0;
  globalThis.fetch = async (_url, options) => {
    reply = JSON.parse(options.body).text;
    replySentAt = Date.now();
    return Response.json({ ok: true });
  };
  try {
    await handleTelegramUpdate({
      update_id: 67,
      message: {
        message_id: 67,
        from: { id: 654321, is_bot: false, first_name: 'Admin' },
        chat: { id: 654321, type: 'private' },
        text: '/admin',
        date: 1699000000,
      },
    }, env);
    assert.match(reply, /https:\/\/worker\.example\/admin\?token=[a-f0-9]{64}/);
    const rawToken = reply.match(/token=([a-f0-9]{64})/)?.[1];
    assert.ok(rawToken);
    const tokenHash = createHash('sha256').update(rawToken).digest('hex');
    assert.equal(db.tables.dashboard_auth_tokens.has(tokenHash), true);
    assert.equal(db.tables.dashboard_auth_tokens.has(rawToken), false);
    const storedToken = db.tables.dashboard_auth_tokens.get(tokenHash);
    const expiresAt = Date.parse(storedToken.expiresAt);
    const expiresIn = expiresAt - replySentAt;
    assert.ok(expiresIn <= 15 * 60 * 1000);
    assert.ok(expiresIn > 15 * 60 * 1000 - 1000);
    assert.equal(db.tables.users.get('654321').role, 'ADMIN');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Suspended Telegram users cannot run commands or old trade callbacks', async () => {
  const db = new MockD1();
  db.tables.users.set('7654321', {
    user_id: '7654321', chat_id: '7654321', username: null, first_name: 'Paused',
    role: 'USER', status: 'SUSPENDED', registered_at: new Date().toISOString(),
    last_active_at: new Date(Date.now() - 120_000).toISOString(),
  });
  const originalFetch = globalThis.fetch;
  const apiCalls = [];
  globalThis.fetch = async (url, options) => {
    apiCalls.push({ url, payload: JSON.parse(options.body) });
    return Response.json({ ok: true });
  };
  try {
    const commandResult = await handleTelegramUpdate({
      update_id: 68,
      message: {
        message_id: 68,
        from: { id: 7654321, is_bot: false, first_name: 'Paused' },
        chat: { id: 7654321, type: 'private' },
        text: '/start',
        date: 1699000000,
      },
    }, { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' });
    assert.equal(commandResult.status, 200);
    assert.match(apiCalls[0].payload.text, /access is suspended/i);
    assert.doesNotMatch(apiCalls[0].payload.text, /Welcome/);

    const callbackResult = await handleTelegramUpdate({
      update_id: 69,
      callback_query: {
        id: 'old-approval',
        from: { id: 7654321, first_name: 'Paused' },
        message: { message_id: 10, chat: { id: 7654321 } },
        data: 'approve_old-signal',
      },
    }, { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' });
    assert.equal(callbackResult.status, 200);
    assert.equal(apiCalls[1].url, 'https://api.telegram.org/bot12345:MOCK_TOKEN_SECRET/answerCallbackQuery');
    assert.match(apiCalls[1].payload.text, /access is suspended/i);
    assert.equal(db.tables.paper_positions.size, 0);
    assert.equal(db.tables.signals_queue.size, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Telegram watchlist - Add and remove actions are user-scoped and the watchlist is interactive', async () => {
  const db = new MockD1();
  const originalFetch = globalThis.fetch;
  const sentMessages = [];
  globalThis.fetch = async (url, options) => {
    if (url.startsWith('https://api.telegram.org/')) {
      sentMessages.push(JSON.parse(options.body));
      return Response.json({ ok: true, result: { message_id: sentMessages.length } });
    }
    throw new Error(`Unexpected request: ${url}`);
  };
  const env = { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' };

  try {
    await handleTelegramUpdate({
      update_id: 200,
      callback_query: {
        id: 'watchlist-add',
        from: { id: 101, first_name: 'Owner' },
        data: 'watchlist_add',
      },
    }, env);
    assert.match(sentMessages[0].text, /Enter a supported trading symbol/);

    await handleTelegramUpdate({
      update_id: 201,
      message: {
        message_id: 201,
        from: { id: 101, is_bot: false, first_name: 'Owner' },
        chat: { id: 101, type: 'private' },
        text: 'BTCUSDT',
        date: 1699000000,
      },
    }, env);
    assert.equal(db.tables.user_watchlist.get('101:BTCUSDT').is_active, 1);
    assert.equal(db.tables.user_watchlist.get('101:BTCUSDT').asset_class, 'CRYPTO');
    assert.ok(sentMessages.at(-1).reply_markup.inline_keyboard.some(
      row => row.some(button => button.text === 'Add Pair')
    ));
    assert.ok(sentMessages.at(-1).reply_markup.inline_keyboard.some(
      row => row.some(button => button.callback_data === 'watchlist_analyze_BTCUSDT')
    ));

    await handleTelegramUpdate({
      update_id: 202,
      callback_query: {
        id: 'other-user-remove',
        from: { id: 202, first_name: 'Other' },
        data: 'watchlist_remove_BTCUSDT',
      },
    }, env);
    assert.equal(db.tables.user_watchlist.get('101:BTCUSDT').is_active, 1);

    await handleTelegramUpdate({
      update_id: 203,
      callback_query: {
        id: 'owner-remove-menu',
        from: { id: 101, first_name: 'Owner' },
        data: 'watchlist_remove_menu',
      },
    }, env);
    const removalMenu = sentMessages.filter(message => message.reply_markup?.inline_keyboard
      .some(row => row.some(button => button.callback_data === 'watchlist_remove_BTCUSDT'))).at(-1);
    assert.ok(removalMenu?.reply_markup.inline_keyboard.some(
      row => row.some(button => button.callback_data === 'watchlist_remove_BTCUSDT')
    ));

    await handleTelegramUpdate({
      update_id: 204,
      callback_query: {
        id: 'owner-remove',
        from: { id: 101, first_name: 'Owner' },
        data: 'watchlist_remove_BTCUSDT',
      },
    }, env);
    assert.equal(db.tables.user_watchlist.get('101:BTCUSDT').is_active, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Telegram watchlist - Rejects an unlisted metal quote without converting it', async () => {
  const db = new MockD1();
  const originalFetch = globalThis.fetch;
  const replies = [];
  globalThis.fetch = async (url, options) => {
    if (url.startsWith('https://api.telegram.org/')) {
      replies.push(JSON.parse(options.body));
      return Response.json({ ok: true, result: { message_id: replies.length } });
    }
    throw new Error(`Unexpected request: ${url}`);
  };
  const env = { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' };

  try {
    await handleTelegramUpdate({
      update_id: 209,
      callback_query: {
        id: 'watchlist-add-unsupported',
        from: { id: 505, first_name: 'Trader' },
        data: 'watchlist_add',
      },
    }, env);
    await handleTelegramUpdate({
      update_id: 210,
      message: {
        message_id: 210,
        from: { id: 505, is_bot: false, first_name: 'Trader' },
        chat: { id: 505, type: 'private' },
        text: 'XAUUSDT',
        date: 1699000000,
      },
    }, env);

    assert.equal(db.tables.user_watchlist.size, 0);
    assert.match(replies.at(-1).text, /XAUUSD is a separate quote pair.*will not substitute/i);
    assert.ok(db.tables.user_settings.has('505:telegram_pending_flow'));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Telegram watchlist - Selecting a saved pair invokes the existing ICT analysis pipeline', async () => {
  const db = new MockD1();
  db.tables.user_watchlist.set('303:BTCUSDT', {
    user_id: '303', symbol: 'BTCUSDT', asset_class: 'CRYPTO', is_active: 1,
  });
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push(url);
    if (url.startsWith('https://api.binance.com/api/v3/klines')) {
      return Response.json(flatBinanceCandles(new URL(url).searchParams.get('interval')));
    }
    if (url.startsWith('https://api.telegram.org/')) {
      return Response.json({ ok: true, result: { message_id: 1 } });
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    await handleTelegramUpdate({
      update_id: 205,
      callback_query: {
        id: 'analyze-saved-pair',
        from: { id: 303, first_name: 'Trader' },
        data: 'watchlist_analyze_BTCUSDT',
      },
    }, { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' });
    assert.equal(requests.filter(url => url.includes('/api/v3/klines')).length, 5);
    assert.ok(requests.some(url => url.startsWith('https://api.telegram.org/')
      && url.endsWith('/sendMessage')));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Telegram /price - Prompts for a symbol, sends a quote and refreshes it', async () => {
  const db = new MockD1();
  const originalFetch = globalThis.fetch;
  const sentMessages = [];
  let yahooRequests = 0;
  globalThis.fetch = async (url, options) => {
    if (url.startsWith('https://api.telegram.org/')) {
      sentMessages.push(JSON.parse(options.body));
      return Response.json({ ok: true, result: { message_id: sentMessages.length } });
    }
    if (url.startsWith('https://query1.finance.yahoo.com/')) {
      yahooRequests += 1;
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
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  };
  const env = { DB: db, TELEGRAM_BOT_TOKEN: '12345:MOCK_TOKEN_SECRET' };

  try {
    await handleTelegramUpdate({
      update_id: 206,
      message: {
        message_id: 206,
        from: { id: 404, is_bot: false, first_name: 'Trader' },
        chat: { id: 404, type: 'private' },
        text: '/price',
        date: 1699000000,
      },
    }, env);
    assert.match(sentMessages[0].text, /Which trading pair would you like to check/);

    await handleTelegramUpdate({
      update_id: 207,
      message: {
        message_id: 207,
        from: { id: 404, is_bot: false, first_name: 'Trader' },
        chat: { id: 404, type: 'private' },
        text: 'EURUSD',
        date: 1699000000,
      },
    }, env);
    assert.match(sentMessages[1].text, /LIVE MARKET PRICE/);
    assert.match(sentMessages[1].text, /Instrument: <code>EURUSD<\/code>/);
    assert.match(sentMessages[1].text, /Price: <b>1\.08 USD<\/b>/);
    assert.match(sentMessages[1].text, /Provider: Yahoo FX/);
    assert.equal(sentMessages[1].reply_markup.inline_keyboard[0][0].text, 'Refresh Price');

    await handleTelegramUpdate({
      update_id: 208,
      callback_query: {
        id: 'refresh-price',
        from: { id: 404, first_name: 'Trader' },
        data: 'price_refresh_EURUSD',
      },
    }, env);
    assert.equal(yahooRequests, 2);
    assert.equal(sentMessages.filter(message => message.text.includes('LIVE MARKET PRICE')).length, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
