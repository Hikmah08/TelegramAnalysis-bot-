import {
  fetchIctAnalysis,
  formatIctOpportunity,
  IctAnalysisError,
} from './ict_analysis.js';

function escapeHtml(value) {
  return value.replace(/[&<>]/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
  })[character]);
}

function opportunityKeyboard(signalId) {
  return {
    inline_keyboard: [
      [
        { text: 'Enter Paper Trade', callback_data: `approve_${signalId}` },
        { text: 'Accept', callback_data: `accept_${signalId}` },
        { text: 'View Analysis', callback_data: `view_analysis_${signalId}` },
      ],
    ],
  };
}

async function sendIctAnalysis(symbol, userId, chatId, botToken, env) {
  const analysis = await fetchIctAnalysis(symbol);
  if (analysis.opportunities.length === 0) {
    await sendTelegramMessage(
      botToken,
      chatId,
      `No qualified ICT/SMC setup is available for <code>${escapeHtml(symbol)}</code> right now.`
    );
    return;
  }

  const createdAt = new Date();
  const expiresAt = new Date(createdAt.getTime() + 30 * 60 * 1000).toISOString();
  for (const opportunity of analysis.opportunities) {
    const signalId = crypto.randomUUID().replace(/-/g, '');
    await env.DB.prepare(
      `INSERT INTO signals_queue
       (signal_id, user_id, symbol, direction, entry_price, stop_loss, take_profit, status, audit_notes, created_at, expires_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)`
    ).bind(
      signalId,
      userId,
      symbol,
      opportunity.direction,
      opportunity.entryPrice,
      opportunity.stopLoss,
      opportunity.takeProfit,
      JSON.stringify(opportunity.auditTrail),
      createdAt.toISOString(),
      expiresAt
    ).run();

    await sendTelegramMessage(
      botToken,
      chatId,
      formatIctOpportunity(symbol, opportunity),
      opportunityKeyboard(signalId)
    );
  }
}

export async function hashDashboardToken(token) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

function safeTelegramErrorDescription(description, botToken) {
  if (typeof description !== 'string' || description.length === 0) {
    return 'Telegram API did not confirm the request';
  }

  return description
    .split(botToken).join('[redacted]')
    .replace(/\b\d{5,}:[A-Za-z0-9_-]{20,}\b/g, '[redacted]')
    .replace(/[\r\n\t]+/g, ' ')
    .slice(0, 240);
}

function normalizeTelegramBotToken(botToken) {
  return botToken.trim().replace(/^bot(?=\d+:)/i, '');
}

async function callTelegramApi(botToken, method, payload) {
  const normalizedToken = normalizeTelegramBotToken(botToken);
  if (!/^\d+:[A-Za-z0-9_-]+$/.test(normalizedToken)) {
    const result = {
      httpStatus: null,
      description: 'Malformed Telegram bot token configuration',
      ok: false,
    };
    console.error('[Telegram API]', result);
    return result;
  }

  try {
    const response = await fetch(`https://api.telegram.org/bot${normalizedToken}/${method}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    let responseBody = null;
    try {
      responseBody = await response.json();
    } catch {
      // A non-JSON response still provides an HTTP status for diagnosis.
    }

    const result = {
      httpStatus: response.status,
      description: typeof responseBody?.description === 'string'
        ? safeTelegramErrorDescription(responseBody.description, normalizedToken)
        : '',
      ok: responseBody?.ok === true,
    };
    console.info('[Telegram API]', result);
    return result;
  } catch {
    const result = {
      httpStatus: null,
      description: 'Telegram API request failed before receiving an HTTP response',
      ok: false,
    };
    console.error('[Telegram API]', result);
    return result;
  }
}

export async function diagnoseTelegramBotToken(botToken) {
  const result = await callTelegramApi(botToken, 'getMe', {});
  return result.ok;
}

export async function sendTelegramMessage(
  botToken,
  chatId,
  text,
  replyMarkup
) {
  const result = await callTelegramApi(botToken, 'sendMessage', {
    chat_id: chatId,
    text,
    parse_mode: 'HTML',
    reply_markup: replyMarkup,
  });

  if (!result.ok && result.httpStatus !== null) {
    await diagnoseTelegramBotToken(botToken);
  }

  return result.ok;
}

export async function handleTelegramUpdate(
  update,
  env
) {
  const botToken = env.TELEGRAM_BOT_TOKEN;
  console.info(`[Telegram] Processing update; bot token configured=${Boolean(botToken)}`);
  if (!botToken) {
    console.error('[Telegram] TELEGRAM_BOT_TOKEN is not configured in the Worker environment');
    return new Response('TELEGRAM_BOT_TOKEN not configured', { status: 500 });
  }

  // Handle callback queries
  if (update.callback_query) {
    const cb = update.callback_query;
    const userId = String(cb.from.id);
    const chatId = cb.message?.chat.id || cb.from.id;
    const data = cb.data || '';
    const callbackAction = data.startsWith('close_pos_')
      ? 'close_position'
      : data === 'menu_analyze_menu'
        ? 'analysis_menu'
        : data.startsWith('analyze_pair_')
          ? 'analyze_pair'
          : data.startsWith('view_analysis_')
            ? 'view_analysis'
            : data.startsWith('approve_') || data.startsWith('accept_')
              ? 'approve_trade'
              : 'unsupported';
    console.info(`[Telegram] Received callback_query; action=${callbackAction}`);

    let callbackAnswer = '';
    let callbackAlert = false;
    if (data === 'menu_analyze_menu') {
      const watchlist = await env.DB.prepare(
        'SELECT symbol FROM user_watchlist WHERE user_id = ? AND is_active = 1 ORDER BY symbol LIMIT 6'
      ).bind(userId).all();
      const symbols = Array.from(new Set([
        'BTCUSDT',
        'ETHUSDT',
        ...(watchlist.results || []).map(item => item.symbol.toUpperCase()),
      ])).slice(0, 8);
      const keyboard = {
        inline_keyboard: symbols.map(symbol => [{
          text: symbol,
          callback_data: `analyze_pair_${symbol}`,
        }]),
      };
      await sendTelegramMessage(
        botToken,
        chatId,
        'Select an instrument for full multi-timeframe ICT/SMC analysis. You can also send <code>/analyze SYMBOL</code>.',
        keyboard
      );
      callbackAnswer = 'Choose an instrument.';
    } else if (data.startsWith('analyze_pair_')) {
      const symbol = data.slice('analyze_pair_'.length).toUpperCase();
      if (!/^[A-Z0-9._=-]{1,20}$/.test(symbol)) {
        callbackAnswer = 'Invalid instrument.';
        callbackAlert = true;
      } else {
        try {
          await sendIctAnalysis(symbol, userId, chatId, botToken, env);
          callbackAnswer = 'Analysis complete.';
        } catch (error) {
          if (!(error instanceof IctAnalysisError)) throw error;
          console.warn('[Telegram] ICT analysis unavailable', { symbol, description: error.message });
          await sendTelegramMessage(botToken, chatId, escapeHtml(error.message));
          callbackAnswer = 'Analysis data is unavailable.';
          callbackAlert = true;
        }
      }
    } else if (data.startsWith('view_analysis_')) {
      const signalId = data.slice('view_analysis_'.length);
      const signal = await env.DB.prepare(
        'SELECT * FROM signals_queue WHERE signal_id = ? AND user_id = ?'
      ).bind(signalId, userId).first();

      if (!signal) {
        callbackAnswer = 'Signal not found.';
        callbackAlert = true;
      } else {
        const notes = JSON.parse(signal.audit_notes || '[]');
        const notesText = notes.length
          ? notes.map(note => `• ${escapeHtml(note)}`).join('\n')
          : '• Multi-timeframe confluence confirmed.';
        await sendTelegramMessage(
          botToken,
          chatId,
          `🔬 <b>CONFLUENCE ANALYSIS: ${escapeHtml(signal.symbol)} ${escapeHtml(signal.direction)}</b>\n\n` +
            `${notesText}\n\n` +
            `<i>Entry: ${signal.entry_price.toFixed(2)} | SL: ${signal.stop_loss.toFixed(2)} | TP: ${signal.take_profit.toFixed(2)}</i>`
        );
        callbackAnswer = 'Analysis details loaded.';
      }
    } else if (data.startsWith('approve_') || data.startsWith('accept_')) {
      const prefix = data.startsWith('approve_') ? 'approve_' : 'accept_';
      const signalId = data.slice(prefix.length);
      const signal = await env.DB.prepare(
        'SELECT * FROM signals_queue WHERE signal_id = ? AND user_id = ?'
      ).bind(signalId, userId).first();

      if (!signal) {
        callbackAnswer = 'Signal not found.';
        callbackAlert = true;
      } else if (signal.status !== 'PENDING') {
        callbackAnswer = signal.status === 'APPROVED'
          ? 'Trade already approved.'
          : `Signal is ${signal.status.toLowerCase()}.`;
        callbackAlert = true;
      } else if (Date.parse(signal.expires_at) <= Date.now()) {
        await env.DB.prepare(
          'UPDATE signals_queue SET status = ? WHERE signal_id = ? AND user_id = ? AND status = ?'
        ).bind('EXPIRED', signalId, userId, 'PENDING').run();
        callbackAnswer = 'Signal expired; market conditions may have changed.';
        callbackAlert = true;
      } else {
        const riskSetting = await env.DB.prepare(
          'SELECT value FROM user_settings WHERE user_id = ? AND key = ?'
        ).bind(userId, 'risk_per_trade_pct').first();
        const positionLimitSetting = await env.DB.prepare(
          'SELECT value FROM user_settings WHERE user_id = ? AND key = ?'
        ).bind(userId, 'max_open_positions').first();
        const configuredRisk = Number(riskSetting?.value ?? 1);
        const riskPct = Number.isFinite(configuredRisk) && configuredRisk > 0
          ? configuredRisk
          : 1;
        const configuredMax = Number.parseInt(
          positionLimitSetting?.value || env.MAX_OPEN_POSITIONS_PER_USER || '20',
          10
        );
        const maxPositions = Number.isFinite(configuredMax) && configuredMax > 0 ? configuredMax : 20;
        const nowIso = new Date().toISOString();
        const results = await env.DB.batch([
          env.DB.prepare(
            `UPDATE signals_queue SET status = 'APPROVED'
             WHERE signal_id = ? AND user_id = ? AND status = 'PENDING' AND expires_at > ?
               AND entry_price != stop_loss
               AND (SELECT COUNT(*) FROM paper_positions WHERE user_id = ? AND status = 'OPEN') < ?
               AND EXISTS (SELECT 1 FROM user_balances WHERE user_id = ? AND current_balance > 0)`
          ).bind(signalId, userId, nowIso, userId, maxPositions, userId),
          env.DB.prepare(
            `INSERT INTO paper_positions
             (user_id, symbol, direction, entry_price, stop_loss, take_profit, position_size, status, entry_time,
              fee_paid, current_price, unrealized_pnl, r_multiple, target_progress_pct, last_price_time, initial_risk)
             SELECT s.user_id, s.symbol, s.direction, s.entry_price, s.stop_loss, s.take_profit,
                    (b.current_balance * ? / 100.0) / ABS(s.entry_price - s.stop_loss),
                    'OPEN', ?, 0, s.entry_price, 0, 0, 0, ?, ABS(s.entry_price - s.stop_loss)
             FROM signals_queue s JOIN user_balances b ON b.user_id = s.user_id
             WHERE s.signal_id = ? AND s.user_id = ? AND s.status = 'APPROVED' AND changes() = 1
               AND (SELECT COUNT(*) FROM paper_positions WHERE user_id = ? AND status = 'OPEN') < ?`
          ).bind(riskPct, nowIso, nowIso, signalId, userId, userId, maxPositions),
        ]);
        const claimed = results[0]?.meta?.changes === 1;
        const positionOpened = results[1]?.meta?.changes === 1;
        if (claimed && positionOpened) {
          callbackAnswer = 'Virtual paper trade executed.';
          await sendTelegramMessage(
            botToken,
            chatId,
            `✅ <b>PAPER TRADE ENTERED</b>\n\n` +
              `• <b>Symbol:</b> ${escapeHtml(signal.symbol)}\n` +
              `• <b>Direction:</b> ${signal.direction === 'BULLISH' ? 'LONG' : 'SHORT'}\n` +
              `• <b>Entry:</b> ${signal.entry_price.toFixed(2)}\n` +
              `• <b>Stop Loss:</b> ${signal.stop_loss.toFixed(2)}\n` +
              `• <b>Take Profit (4:1):</b> ${signal.take_profit.toFixed(2)}\n` +
              `<i>Virtual paper trading only.</i>`
          );
        } else {
          callbackAnswer = 'Trade could not be opened; the signal may be expired or your open-position limit reached.';
          callbackAlert = true;
        }
      }
    } else if (data.startsWith('close_pos_')) {
      const posId = parseInt(data.replace('close_pos_', ''), 10);
      const posResult = await env.DB.prepare(
        'SELECT * FROM paper_positions WHERE position_id = ? AND user_id = ? AND status = "OPEN"'
      ).bind(posId, userId).first();

      if (!posResult) {
        await sendTelegramMessage(botToken, chatId, '⚠️ Position not found or already closed.');
      } else {
        const exitPrice = posResult.current_price || posResult.entry_price;
        const isLong = posResult.direction === 'BULLISH';
        const realizedPnl = isLong 
          ? (exitPrice - posResult.entry_price) * posResult.position_size 
          : (posResult.entry_price - exitPrice) * posResult.position_size;

        const nowIso = new Date().toISOString();

        await env.DB.batch([
          env.DB.prepare(
            `UPDATE paper_positions 
             SET status = 'CLOSED_MANUAL', exit_time = ?, exit_price = ?, exit_reason = 'MANUAL_TELEGRAM', pnl = ? 
             WHERE position_id = ?`
          ).bind(nowIso, exitPrice, realizedPnl, posId),
          env.DB.prepare(
            `UPDATE user_balances 
             SET current_balance = current_balance + ?, realized_pnl = realized_pnl + ?, total_trades = total_trades + 1, updated_at = ? 
             WHERE user_id = ?`
          ).bind(realizedPnl, realizedPnl, nowIso, userId),
          env.DB.prepare(
            `INSERT INTO balance_transactions (user_id, position_id, amount, balance_after, transaction_type, description, created_at)
             VALUES (?, ?, ?, (SELECT current_balance FROM user_balances WHERE user_id = ?), 'POSITION_PNL', 'Manual close via Telegram', ?)`
          ).bind(userId, posId, realizedPnl, userId, nowIso),
        ]);

        await sendTelegramMessage(
          botToken,
          chatId,
          `✅ <b>Position #${posId} Closed</b>\nSymbol: <code>${posResult.symbol}</code>\nRealized P&L: <b>${realizedPnl >= 0 ? '+' : ''}$${realizedPnl.toFixed(2)}</b>`
        );
      }
    }

    const callbackPayload = { callback_query_id: cb.id };
    if (callbackAnswer) {
      callbackPayload.text = callbackAnswer;
      callbackPayload.show_alert = callbackAlert;
    }
    await callTelegramApi(botToken, 'answerCallbackQuery', callbackPayload);
    return new Response(JSON.stringify({ ok: true }), {
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // Handle text messages
  if (!update.message || !update.message.text) {
    return new Response(JSON.stringify({ ok: true }), {
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const msg = update.message;
  const text = msg.text.trim();
  const commandMatch = /^\/[a-z0-9_]+(?:@[a-z0-9_]+)?/i.exec(text);
  const command = commandMatch?.[0].split('@')[0].toLowerCase();
  const commandArgs = commandMatch ? text.slice(commandMatch[0].length).trim().split(/\s+/).filter(Boolean) : [];
  console.info(`[Telegram] Received ${command ? `command ${command}` : 'non-command message'}`);
  const userId = String(msg.from.id);
  const chatId = String(msg.chat.id);
  const username = msg.from.username || null;
  const firstName = msg.from.first_name || null;
  const nowIso = new Date().toISOString();

  await env.DB.prepare(
    `INSERT INTO users (user_id, chat_id, username, first_name, role, status, registered_at, last_active_at)
     VALUES (?, ?, ?, ?, 'USER', 'ACTIVE', ?, ?)
     ON CONFLICT(user_id) DO UPDATE SET last_active_at = ?, chat_id = ?`
  ).bind(userId, chatId, username, firstName, nowIso, nowIso, nowIso, chatId).run();

  const startingBal = parseFloat(env.DEFAULT_STARTING_BALANCE || '10000.0');
  await env.DB.prepare(
    `INSERT OR IGNORE INTO user_balances (user_id, starting_balance, current_balance, realized_pnl, total_trades, winning_trades, losing_trades, updated_at)
     VALUES (?, ?, ?, 0.0, 0, 0, 0, ?)`
  ).bind(userId, startingBal, startingBal, nowIso).run();

  if (command === '/start' || command === '/help' || command === '/menu') {
    const welcome = `🤖 <b>ICT/SMC AI Paper Trading Bot (Cloudflare Edition)</b>\n\n` +
      `Welcome <b>${firstName || 'Trader'}</b>!\n` +
      `Your account is initialized with <b>$${startingBal.toLocaleString()}</b> virtual capital.\n\n` +
      `<b>Available Commands:</b>\n` +
      `• /balance - View current account balance and metrics\n` +
      `• /positions - View your open paper positions\n` +
      `• /history - View closed paper trades\n` +
      `• /watchlist - View your monitored watchlist\n` +
      `• /dashboard - One-click secure login token for Web Dashboard\n` +
      `• /analyze SYMBOL - Run full multi-timeframe ICT/SMC analysis\n` +
      `• /menu - Display this command overview\n` +
      `• /help - Display this command overview\n\n` +
      `<i>Real-money trading is disabled. All positions run in isolated paper-trading simulation.</i>`;
    await sendTelegramMessage(botToken, chatId, welcome, {
      inline_keyboard: [[{ text: 'Analysis', callback_data: 'menu_analyze_menu' }]],
    });
  } else if (command === '/analyze' || command === '/scan') {
    const symbol = commandArgs[0]?.toUpperCase();
    if (!symbol) {
      await sendTelegramMessage(
        botToken,
        chatId,
        'Usage: <code>/analyze SYMBOL</code>\nExample: <code>/analyze BTCUSDT</code>',
        { inline_keyboard: [[{ text: 'Analysis', callback_data: 'menu_analyze_menu' }]] }
      );
    } else if (!/^[A-Z0-9._=-]{1,20}$/.test(symbol)) {
      await sendTelegramMessage(botToken, chatId, 'Invalid symbol. Use letters and numbers, for example <code>BTCUSDT</code> or <code>EURUSD</code>.');
    } else {
      try {
        await sendIctAnalysis(symbol, userId, chatId, botToken, env);
      } catch (error) {
        if (!(error instanceof IctAnalysisError)) throw error;
        console.warn('[Telegram] ICT analysis unavailable', { symbol, description: error.message });
        await sendTelegramMessage(botToken, chatId, escapeHtml(error.message));
      }
    }
  } else if (command === '/balance' || command === '/account' || command === '/performance') {
    const bal = await env.DB.prepare(
      'SELECT * FROM user_balances WHERE user_id = ?'
    ).bind(userId).first();

    const openPositions = await env.DB.prepare(
      'SELECT SUM(unrealized_pnl) as total_unrealized FROM paper_positions WHERE user_id = ? AND status = "OPEN"'
    ).bind(userId).first();

    const unrealized = openPositions?.total_unrealized || 0.0;
    const equity = (bal?.current_balance || startingBal) + unrealized;

    const msgText = `💰 <b>Account Balance & Ledger</b>\n\n` +
      `• Virtual Balance: <b>$${(bal?.current_balance || startingBal).toFixed(2)}</b>\n` +
      `• Unrealized P&L: <b>${unrealized >= 0 ? '+' : ''}$${unrealized.toFixed(2)}</b>\n` +
      `• Total Equity: <b>$${equity.toFixed(2)}</b>\n` +
      `• Realized P&L: <b>${(bal?.realized_pnl || 0.0) >= 0 ? '+' : ''}$${(bal?.realized_pnl || 0.0).toFixed(2)}</b>\n` +
      `• Total Trades: <b>${bal?.total_trades || 0}</b>`;
    await sendTelegramMessage(botToken, chatId, msgText);
  } else if (command === '/positions') {
    const positions = await env.DB.prepare(
      'SELECT * FROM paper_positions WHERE user_id = ? AND status = "OPEN" ORDER BY entry_time DESC'
    ).bind(userId).all();

    if (!positions.results || positions.results.length === 0) {
      await sendTelegramMessage(botToken, chatId, '📊 You have no open paper positions.');
    } else {
      let msgText = `📊 <b>Your Open Paper Positions (${positions.results.length}):</b>\n\n`;
      for (const p of positions.results) {
        const pnl = p.unrealized_pnl || 0.0;
        msgText += `• <b>#${p.position_id} ${p.symbol}</b> (${p.direction})\n` +
          `  Entry: ${p.entry_price} | Current: ${p.current_price || p.entry_price}\n` +
          `  SL: ${p.stop_loss} | TP: ${p.take_profit}\n` +
          `  P&L: <b>${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</b> (${p.r_multiple || 0.0}R | ${p.target_progress_pct || 0.0}%)\n\n`;
      }
      await sendTelegramMessage(botToken, chatId, msgText);
    }
  } else if (command === '/history') {
    const history = await env.DB.prepare(
      'SELECT * FROM paper_positions WHERE user_id = ? AND status != "OPEN" ORDER BY exit_time DESC LIMIT 10'
    ).bind(userId).all();

    if (!history.results || history.results.length === 0) {
      await sendTelegramMessage(botToken, chatId, '📜 No completed paper trades recorded yet.');
    } else {
      const lines = [`📜 <b>Recent Trade History (${history.results.length}):</b>\n`];
      for (const position of history.results) {
        const pnl = Number(position.pnl || 0);
        lines.push(
          `• <b>#${position.position_id} ${position.symbol}</b> (${position.direction})\n` +
          `  Entry: ${position.entry_price} | Exit: ${position.exit_price ?? 'N/A'}\n` +
          `  Result: ${position.status} | P&L: ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`
        );
      }
      await sendTelegramMessage(botToken, chatId, lines.join('\n'));
    }
  } else if (command === '/watchlist') {
    const watchlist = await env.DB.prepare(
      'SELECT symbol, asset_class FROM user_watchlist WHERE user_id = ? AND is_active = 1 ORDER BY symbol'
    ).bind(userId).all();

    if (!watchlist.results || watchlist.results.length === 0) {
      await sendTelegramMessage(botToken, chatId, '📋 Your watchlist is empty.');
    } else {
      const symbols = watchlist.results
        .map(item => `• <code>${item.symbol}</code> (${item.asset_class})`);
      await sendTelegramMessage(botToken, chatId, `📋 <b>Your Watchlist:</b>\n${symbols.join('\n')}`);
    }
  } else if (command === '/dashboard') {
    const token = crypto.randomUUID().replace(/-/g, '') + crypto.randomUUID().replace(/-/g, '');
    const tokenHash = await hashDashboardToken(token);
    const expiresAt = new Date(Date.now() + 15 * 60 * 1000).toISOString();

    await env.DB.prepare(
      'INSERT INTO dashboard_auth_tokens (token_hash, user_id, created_at, expires_at, is_used) VALUES (?, ?, ?, ?, 0)'
    ).bind(tokenHash, userId, nowIso, expiresAt).run();

    const dashBase = env.DASHBOARD_URL || 'http://localhost:8000';
    const loginUrl = `${dashBase}/auth/login?token=${token}`;

    const textReply = `🔑 <b>Web Dashboard One-Click Login</b>\n\n` +
      `Click the link below to securely log into your web dashboard. This token expires in 15 minutes and can only be used once.\n\n` +
      `🔗 <a href="${loginUrl}">Log In to Web Dashboard</a>`;

    await sendTelegramMessage(botToken, chatId, textReply);
  } else {
    await sendTelegramMessage(
      botToken,
      chatId,
      `Unrecognized command. Type /help to see available commands.`
    );
  }

  return new Response(JSON.stringify({ ok: true }), {
    headers: { 'Content-Type': 'application/json' },
  });
}
