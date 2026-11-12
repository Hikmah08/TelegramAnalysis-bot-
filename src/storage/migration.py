import os
import shutil
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def backup_database(db_path: Path | str) -> Path:
    """
    Creates a verified, timestamped backup of the SQLite database.
    Verifies data integrity using PRAGMA integrity_check.
    """
    db_file = Path(db_path)
    if not db_file.exists():
        raise FileNotFoundError(f"Database file not found for backup: {db_file}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = db_file.parent / f"{db_file.name}.backup_{timestamp}"

    # Perform binary copy
    shutil.copy2(db_file, backup_file)

    # Verify integrity of backup
    conn = sqlite3.connect(backup_file)
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0].lower() != "ok":
            raise RuntimeError(f"Database backup failed integrity check: {check}")
    finally:
        conn.close()

    logger.info(f"Verified database backup created at {backup_file} (size: {backup_file.stat().st_size} bytes)")
    return backup_file


def is_migration_needed(db_file: Path, target_version: int = 1) -> bool:
    """
    Checks if schema migration has already been executed on this database.
    Avoids unnecessary repeated migrations and duplicate backups.
    """
    if not db_file.exists() or db_file.stat().st_size == 0:
        return True
    try:
        conn = sqlite3.connect(db_file)
        try:
            # Check schema_migrations table
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
            if not cur.fetchone():
                return True
            row = conn.execute("SELECT version FROM schema_migrations WHERE version >= ?", (target_version,)).fetchone()
            return row is None
        finally:
            conn.close()
    except Exception:
        return True


def migrate_database(db_path: Path | str, primary_owner_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Executes an atomic, repeatable database migration converting single-tenant schema
    into multi-user schema with complete data isolation:
    1. Creates verified backup.
    2. Provisions primary bot owner in 'users' with 'ADMIN' role.
    3. Reassigns existing paper positions, watchlist items, signals, and settings to the owner.
    4. Sets up atomic outbox notifications, auth tokens, and session tables.
    5. Leaves legacy tables safe and repeatable.
    """
    db_file = Path(db_path)

    # Determine authoritative original owner user_id and chat_id
    admin_env = os.getenv("TELEGRAM_ADMIN_IDS", "").strip()
    first_admin = [x.strip() for x in admin_env.split(",") if x.strip()][0] if admin_env else None
    owner_id = str(primary_owner_id or os.getenv("PRIMARY_OWNER_ID") or first_admin or os.getenv("TELEGRAM_CHAT_ID") or "OWNER_001").strip()
    owner_chat_id = str(os.getenv("TELEGRAM_CHAT_ID") or owner_id).strip()

    # Fast check: If already migrated, skip redundant backups and schema execution
    if not is_migration_needed(db_file, target_version=1):
        return {
            "status": "already_up_to_date",
            "backup_path": None,
            "owner_id": owner_id,
            "owner_chat_id": owner_chat_id,
            "users_migrated": 0,
            "settings_migrated": 0,
            "positions_migrated": 0,
            "watchlist_migrated": 0,
            "signals_migrated": 0,
        }

    backup_path = None
    if db_file.exists() and db_file.stat().st_size > 0:
        backup_path = backup_database(db_file)

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    report = {
        "status": "success",
        "backup_path": str(backup_path) if backup_path else None,
        "owner_id": owner_id,
        "owner_chat_id": owner_chat_id,
        "users_migrated": 0,
        "settings_migrated": 0,
        "positions_migrated": 0,
        "watchlist_migrated": 0,
        "signals_migrated": 0,
    }

    try:
        # Start Transaction
        cursor.execute("BEGIN TRANSACTION")

        # 1. Create Users Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                username TEXT,
                first_name TEXT,
                role TEXT DEFAULT 'USER',       -- 'ADMIN' or 'USER'
                status TEXT DEFAULT 'ACTIVE',   -- 'ACTIVE' or 'SUSPENDED'
                registered_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL
            )
        """)

        # Ensure Owner exists
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
            INSERT OR IGNORE INTO users (user_id, chat_id, username, first_name, role, status, registered_at, last_active_at)
            VALUES (?, ?, 'owner', 'Bot Owner', 'ADMIN', 'ACTIVE', ?, ?)
        """, (owner_id, owner_chat_id, now_iso, now_iso))
        report["users_migrated"] += 1

        # 2. Create User Settings Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # Migrate legacy bot_settings to user_settings under owner_id
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bot_settings'")
        if cursor.fetchone():
            legacy_settings = cursor.execute("SELECT key, value FROM bot_settings").fetchall()
            for r in legacy_settings:
                cursor.execute("""
                    INSERT OR REPLACE INTO user_settings (user_id, key, value)
                    VALUES (?, ?, ?)
                """, (owner_id, r["key"], r["value"]))
                report["settings_migrated"] += 1

        # Seed owner default settings if not already present
        defaults = {
            "risk_per_trade_pct": "1.0",
            "target_risk_reward": "4.0",
            "max_open_positions": "20",
            "initial_balance": "10000.0",
            "current_balance": "10000.0",
            "preferred_timeframes": '["4h", "1h", "15m", "5m", "1m"]',
            "auto_alert_enabled": "true",
            "dashboard_url": "http://127.0.0.1:8000",
        }
        for k, v in defaults.items():
            cursor.execute("""
                INSERT OR IGNORE INTO user_settings (user_id, key, value)
                VALUES (?, ?, ?)
            """, (owner_id, k, v))

        # 3. Create Scoped Watchlist Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_watchlist (
                user_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                added_at TEXT NOT NULL,
                PRIMARY KEY (user_id, symbol),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # Migrate legacy watchlist to user_watchlist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='watchlist'")
        if cursor.fetchone():
            legacy_wl = cursor.execute("SELECT symbol, asset_class, is_active, added_at FROM watchlist").fetchall()
            for w in legacy_wl:
                cursor.execute("""
                    INSERT OR IGNORE INTO user_watchlist (user_id, symbol, asset_class, is_active, added_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (owner_id, w["symbol"], w["asset_class"], w["is_active"], w["added_at"]))
                report["watchlist_migrated"] += 1

        # Seed owner BTCUSDT in user_watchlist
        cursor.execute("""
            INSERT OR IGNORE INTO user_watchlist (user_id, symbol, asset_class, is_active, added_at)
            VALUES (?, 'BTCUSDT', 'CRYPTO', 1, ?)
        """, (owner_id, now_iso))

        # 4. Migrate Paper Positions Table (ensure user_id and monitoring columns exist)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_positions'")
        if cursor.fetchone():
            pos_cols = [c[1] for c in cursor.execute("PRAGMA table_info(paper_positions)").fetchall()]
            if "user_id" not in pos_cols:
                cursor.execute(f"ALTER TABLE paper_positions ADD COLUMN user_id TEXT NOT NULL DEFAULT '{owner_id}'")
            if "current_price" not in pos_cols:
                cursor.execute("ALTER TABLE paper_positions ADD COLUMN current_price REAL")
            if "unrealized_pnl" not in pos_cols:
                cursor.execute("ALTER TABLE paper_positions ADD COLUMN unrealized_pnl REAL DEFAULT 0.0")
            if "r_multiple" not in pos_cols:
                cursor.execute("ALTER TABLE paper_positions ADD COLUMN r_multiple REAL DEFAULT 0.0")
            if "target_progress_pct" not in pos_cols:
                cursor.execute("ALTER TABLE paper_positions ADD COLUMN target_progress_pct REAL DEFAULT 0.0")
            if "last_price_time" not in pos_cols:
                cursor.execute("ALTER TABLE paper_positions ADD COLUMN last_price_time TEXT")
            if "initial_risk" not in pos_cols:
                cursor.execute("ALTER TABLE paper_positions ADD COLUMN initial_risk REAL DEFAULT 0.0")
            if "last_notified_progress_milestone" not in pos_cols:
                cursor.execute("ALTER TABLE paper_positions ADD COLUMN last_notified_progress_milestone REAL DEFAULT 0.0")
            if "last_notified_r_milestone" not in pos_cols:
                cursor.execute("ALTER TABLE paper_positions ADD COLUMN last_notified_r_milestone REAL DEFAULT 0.0")
            # Update any positions without user_id or default to owner_id
            cursor.execute("UPDATE paper_positions SET user_id = ? WHERE user_id IS NULL OR user_id = ''", (owner_id,))
            report["positions_migrated"] = cursor.execute("SELECT COUNT(*) FROM paper_positions WHERE user_id = ?", (owner_id,)).fetchone()[0]
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS paper_positions (
                    position_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    position_size REAL NOT NULL,
                    status TEXT NOT NULL, -- OPEN, CLOSED
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
                )
            """)

        # 5. Migrate Signals Queue Table (ensure user_id exists)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signals_queue'")
        if cursor.fetchone():
            sig_cols = [c[1] for c in cursor.execute("PRAGMA table_info(signals_queue)").fetchall()]
            if "user_id" not in sig_cols:
                cursor.execute(f"ALTER TABLE signals_queue ADD COLUMN user_id TEXT NOT NULL DEFAULT '{owner_id}'")
            cursor.execute("UPDATE signals_queue SET user_id = ? WHERE user_id IS NULL OR user_id = ''", (owner_id,))
            report["signals_migrated"] = cursor.execute("SELECT COUNT(*) FROM signals_queue WHERE user_id = ?", (owner_id,)).fetchone()[0]
        else:
            cursor.execute("""
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
                )
            """)

        # 6. Create Outbox Notifications Table (Atomic Outbox Pattern)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS outbox_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                event_type TEXT NOT NULL, -- TAKE_PROFIT, STOP_LOSS, MANUAL_CLOSE, TRADE_OPENED
                payload TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING', -- PENDING, SENT, FAILED
                retry_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # 7. Create Dashboard Auth Tokens Table (One-Time Login Tokens)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_auth_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                is_used INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # 8. Create Dashboard Sessions Table (Server-Side Session Authentication)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # 9. Track Schema Migrations
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            INSERT OR REPLACE INTO schema_migrations (version, applied_at)
            VALUES (1, ?)
        """, (now_iso,))

        cursor.execute("COMMIT")
        logger.info(f"Database migration completed successfully. Relinked existing records to owner '{owner_id}'.")
    except Exception as e:
        cursor.execute("ROLLBACK")
        logger.error(f"Migration error: {e}. Rolled back transaction.")
        raise
    finally:
        conn.close()

    return report


if __name__ == "__main__":
    db_default = Path(__file__).resolve().parent.parent.parent / "trading_bot.db"
    res = migrate_database(db_default)
    print("Migration Result:", res)
