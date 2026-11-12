-- Cloudflare D1 SQL Schema for Multi-User Trading Platform
-- Matches exact SQLite structure with multi-user isolation

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    username TEXT,
    first_name TEXT,
    role TEXT DEFAULT 'USER',
    status TEXT DEFAULT 'ACTIVE',
    registered_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS user_balances (
    user_id TEXT PRIMARY KEY,
    starting_balance REAL DEFAULT 10000.0,
    current_balance REAL DEFAULT 10000.0,
    realized_pnl REAL DEFAULT 0.0,
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS balance_transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    position_id INTEGER,
    amount REAL NOT NULL,
    balance_after REAL NOT NULL,
    transaction_type TEXT NOT NULL, -- INITIAL_DEPOSIT, POSITION_PNL, FEE, ADJUSTMENT
    description TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS user_watchlist (
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    added_at TEXT NOT NULL,
    PRIMARY KEY (user_id, symbol),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS paper_positions (
    position_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL, -- BULLISH / BEARISH
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    position_size REAL NOT NULL,
    status TEXT NOT NULL, -- OPEN, CLOSED_TP, CLOSED_SL, CLOSED_MANUAL
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    pnl REAL DEFAULT 0.0,
    fee_paid REAL DEFAULT 0.0,
    current_price REAL,
    unrealized_pnl REAL DEFAULT 0.0,
    r_multiple REAL DEFAULT 0.0,
    target_progress_pct REAL DEFAULT 0.0,
    last_price_time TEXT,
    initial_risk REAL DEFAULT 0.0,
    last_notified_progress_milestone REAL DEFAULT 0.0,
    last_notified_r_milestone REAL DEFAULT 0.0,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS signals_queue (
    signal_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    status TEXT NOT NULL, -- PENDING, APPROVED, REJECTED, EXPIRED
    audit_notes TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS outbox_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT DEFAULT 'PENDING', -- PENDING, SENT, FAILED
    retry_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS dashboard_auth_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    is_used INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS dashboard_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_migrations (version, applied_at, description)
VALUES (1, datetime('now'), 'Initial schema with user balances and transactions');
