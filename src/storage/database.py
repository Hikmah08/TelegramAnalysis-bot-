import os
import sqlite3
import json
import secrets
import hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
from .migration import migrate_database

DB_PATH = Path(__file__).resolve().parent.parent.parent / "trading_bot.db"


class StorageManager:
    """
    Multi-User SQLite persistence with complete account isolation, user-scoped settings,
    watchlists, signals, and paper positions.
    Enforces atomic position closures and persistent outbox notifications.
    """

    def __init__(self, db_path: Path = DB_PATH, primary_owner_id: Optional[str] = None):
        env_db_path = os.getenv("DATABASE_PATH")
        self.db_path = Path(env_db_path) if env_db_path else Path(db_path)
        admin_env = os.getenv("TELEGRAM_ADMIN_IDS", "").strip()
        first_admin = [x.strip() for x in admin_env.split(",") if x.strip()][0] if admin_env else None
        self.primary_owner_id = str(primary_owner_id or os.getenv("PRIMARY_OWNER_ID") or first_admin or os.getenv("TELEGRAM_CHAT_ID") or "OWNER_001").strip()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Runs migration to guarantee complete multi-user schema and default seeds."""
        migrate_database(self.db_path, primary_owner_id=self.primary_owner_id)

    # ==========================================
    # --- USER REGISTRATION & LIFECYCLE API ---
    # ==========================================
    def register_user(
        self,
        user_id: str | int,
        chat_id: str | int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        role: str = "USER",
    ) -> Dict[str, Any]:
        """
        Registers a new Telegram user or updates their last active timestamp.
        Provisions an isolated paper-trading account with default balance, 20-position limit,
        and initial watchlist.
        """
        uid = str(user_id).strip()
        cid = str(chat_id).strip()
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (uid,))
            existing = cursor.fetchone()

            if existing:
                # Update last active timestamp and chat_id in case it changed
                cursor.execute(
                    "UPDATE users SET last_active_at = ?, chat_id = ? WHERE user_id = ?",
                    (now, cid, uid)
                )
                conn.commit()
                return dict(existing)

            # Insert new isolated user
            cursor.execute("""
                INSERT INTO users (user_id, chat_id, username, first_name, role, status, registered_at, last_active_at)
                VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            """, (uid, cid, username or "", first_name or "", role, now, now))

            # Seed isolated user settings
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
                """, (uid, k, v))

            # Seed isolated default watchlist
            cursor.execute("""
                INSERT OR IGNORE INTO user_watchlist (user_id, symbol, asset_class, is_active, added_at)
                VALUES (?, 'BTCUSDT', 'CRYPTO', 1, ?)
            """, (uid, now))

            conn.commit()
            return {
                "user_id": uid,
                "chat_id": cid,
                "username": username or "",
                "first_name": first_name or "",
                "role": role,
                "status": "ACTIVE",
                "registered_at": now,
                "last_active_at": now,
            }

    def get_user(self, user_id: str | int) -> Optional[Dict[str, Any]]:
        uid = str(user_id).strip()
        with self._get_connection() as conn:
            row = conn.cursor().execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
            return dict(row) if row else None

    def get_user_by_chat_id(self, chat_id: str | int) -> Optional[Dict[str, Any]]:
        cid = str(chat_id).strip()
        with self._get_connection() as conn:
            row = conn.cursor().execute("SELECT * FROM users WHERE chat_id = ?", (cid,)).fetchone()
            return dict(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.cursor().execute("SELECT * FROM users ORDER BY registered_at ASC").fetchall()
            return [dict(r) for r in rows]

    def update_user_status(self, user_id: str | int, status: str) -> bool:
        uid = str(user_id).strip()
        valid = status.upper() in ("ACTIVE", "SUSPENDED")
        if not valid:
            raise ValueError(f"Invalid user status: {status}")
        with self._get_connection() as conn:
            cur = conn.cursor().execute("UPDATE users SET status = ? WHERE user_id = ?", (status.upper(), uid))
            conn.commit()
            return cur.rowcount > 0

    # ==========================================
    # --- USER-SCOPED SETTINGS API ---
    # ==========================================
    def get_setting(self, key: str, default: Any = None, user_id: Optional[str | int] = None) -> Any:
        uid = str(user_id).strip() if user_id is not None else self.primary_owner_id
        with self._get_connection() as conn:
            row = conn.cursor().execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                (uid, key)
            ).fetchone()
            if row:
                return row["value"]
            # Fallback check on global bot_settings table
            try:
                legacy = conn.cursor().execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
                if legacy:
                    return legacy["value"]
            except Exception:
                pass
            return default

    def set_setting(self, key: str, value: Any, user_id: Optional[str | int] = None):
        uid = str(user_id).strip() if user_id is not None else self.primary_owner_id
        with self._get_connection() as conn:
            conn.cursor().execute(
                "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
                (uid, key, str(value))
            )
            # Also keep bot_settings synchronized for backward compatibility
            try:
                conn.cursor().execute(
                    "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                    (key, str(value))
                )
            except Exception:
                pass
            conn.commit()

    def get_all_settings(self, user_id: Optional[str | int] = None) -> Dict[str, str]:
        uid = str(user_id).strip() if user_id is not None else self.primary_owner_id
        with self._get_connection() as conn:
            rows = conn.cursor().execute(
                "SELECT key, value FROM user_settings WHERE user_id = ?",
                (uid,)
            ).fetchall()
            res = {r["key"]: r["value"] for r in rows}
            if not res:
                try:
                    legacy_rows = conn.cursor().execute("SELECT key, value FROM bot_settings").fetchall()
                    return {r["key"]: r["value"] for r in legacy_rows}
                except Exception:
                    pass
            return res

    # ==========================================
    # --- USER-SCOPED WATCHLIST API ---
    # ==========================================
    def get_watchlist(self, user_id: Optional[str | int] = None) -> List[Dict[str, Any]]:
        uid = str(user_id).strip() if user_id is not None else self.primary_owner_id
        with self._get_connection() as conn:
            rows = conn.cursor().execute(
                "SELECT symbol, asset_class, is_active FROM user_watchlist WHERE user_id = ? AND is_active = 1",
                (uid,)
            ).fetchall()
            if not rows and uid != self.primary_owner_id:
                # Check owner's watchlist
                rows = conn.cursor().execute(
                    "SELECT symbol, asset_class, is_active FROM user_watchlist WHERE user_id = ? AND is_active = 1",
                    (self.primary_owner_id,)
                ).fetchall()
            if not rows:
                return [{"symbol": "BTCUSDT", "asset_class": "CRYPTO"}]
            return [{"symbol": r["symbol"], "asset_class": r["asset_class"]} for r in rows]

    def add_watchlist_symbol(self, symbol: str, asset_class: str = "CRYPTO", user_id: Optional[str | int] = None) -> bool:
        uid = str(user_id).strip() if user_id is not None else self.primary_owner_id
        clean_sym = symbol.strip().upper().replace("/", "")
        with self._get_connection() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.cursor().execute(
                "INSERT OR REPLACE INTO user_watchlist (user_id, symbol, asset_class, is_active, added_at) VALUES (?, ?, ?, 1, ?)",
                (uid, clean_sym, asset_class.upper(), now)
            )
            conn.commit()
            return True

    def remove_watchlist_symbol(self, symbol: str, user_id: Optional[str | int] = None) -> bool:
        uid = str(user_id).strip() if user_id is not None else self.primary_owner_id
        clean_sym = symbol.strip().upper().replace("/", "")
        with self._get_connection() as conn:
            cur = conn.cursor().execute(
                "DELETE FROM user_watchlist WHERE user_id = ? AND symbol = ?",
                (uid, clean_sym)
            )
            conn.commit()
            return cur.rowcount > 0

    # ==========================================
    # --- USER-SCOPED SIGNALS QUEUE API ---
    # ==========================================
    def save_signal(
        self,
        signal_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        audit_notes: List[str],
        validity_minutes: int = 30,
        user_id: Optional[str | int] = None,
    ) -> bool:
        uid = str(user_id).strip() if user_id is not None else self.primary_owner_id
        with self._get_connection() as conn:
            now = datetime.now(timezone.utc)
            expires = now + timedelta(minutes=validity_minutes)
            conn.cursor().execute("""
                INSERT OR IGNORE INTO signals_queue 
                (signal_id, user_id, symbol, direction, entry_price, stop_loss, take_profit, status, audit_notes, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
            """, (
                signal_id, uid, symbol, direction, entry_price, stop_loss, take_profit,
                json.dumps(audit_notes), now.isoformat(), expires.isoformat()
            ))
            conn.commit()
            return True

    def get_signal(self, signal_id: str, user_id: Optional[str | int] = None) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            if user_id is not None:
                uid = str(user_id).strip()
                row = conn.cursor().execute(
                    "SELECT * FROM signals_queue WHERE signal_id = ? AND user_id = ?",
                    (signal_id, uid)
                ).fetchone()
            else:
                row = conn.cursor().execute(
                    "SELECT * FROM signals_queue WHERE signal_id = ?",
                    (signal_id,)
                ).fetchone()

            if not row:
                return None
            res = dict(row)
            # Check expiration
            expires_dt = datetime.fromisoformat(res["expires_at"])
            if datetime.now(timezone.utc) > expires_dt and res["status"] == "PENDING":
                res["status"] = "EXPIRED"
                conn.cursor().execute("UPDATE signals_queue SET status = 'EXPIRED' WHERE signal_id = ?", (signal_id,))
                conn.commit()
            return res

    def update_signal_status(self, signal_id: str, status: str, user_id: Optional[str | int] = None) -> bool:
        with self._get_connection() as conn:
            if user_id is not None:
                uid = str(user_id).strip()
                cur = conn.cursor().execute(
                    "UPDATE signals_queue SET status = ? WHERE signal_id = ? AND user_id = ?",
                    (status, signal_id, uid)
                )
            else:
                cur = conn.cursor().execute(
                    "UPDATE signals_queue SET status = ? WHERE signal_id = ?",
                    (status, signal_id)
                )
            conn.commit()
            return cur.rowcount > 0

    # ==========================================
    # --- USER-SCOPED PAPER POSITIONS API ---
    # ==========================================
    def open_paper_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        position_size: float,
        fee_paid: float = 0.0,
        user_id: Optional[str | int] = None,
        initial_risk: float = 0.0,
    ) -> int:
        uid = str(user_id).strip() if user_id is not None else self.primary_owner_id
        calc_risk = initial_risk if initial_risk > 0 else abs(entry_price - stop_loss) * position_size
        with self._get_connection() as conn:
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.cursor().execute("""
                INSERT INTO paper_positions 
                (user_id, symbol, direction, entry_price, stop_loss, take_profit, position_size, status, entry_time, fee_paid, current_price, unrealized_pnl, r_multiple, target_progress_pct, last_price_time, initial_risk)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, 0.0, 0.0, 0.0, ?, ?)
            """, (uid, symbol, direction, entry_price, stop_loss, take_profit, position_size, now, fee_paid, entry_price, now, calc_risk))
            conn.commit()
            return cur.lastrowid

    def update_position_monitoring_state(
        self,
        position_id: int,
        current_price: float,
        unrealized_pnl: float,
        r_multiple: float,
        target_progress_pct: float,
        price_timestamp: str,
        last_notified_progress_milestone: Optional[float] = None,
        last_notified_r_milestone: Optional[float] = None,
    ) -> bool:
        """
        Updates live ongoing monitoring state of an OPEN paper position.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if last_notified_progress_milestone is not None and last_notified_r_milestone is not None:
                cursor.execute("""
                    UPDATE paper_positions
                    SET current_price = ?,
                        unrealized_pnl = ?,
                        r_multiple = ?,
                        target_progress_pct = ?,
                        last_price_time = ?,
                        last_notified_progress_milestone = ?,
                        last_notified_r_milestone = ?
                    WHERE position_id = ? AND status = 'OPEN'
                """, (
                    current_price,
                    unrealized_pnl,
                    r_multiple,
                    target_progress_pct,
                    price_timestamp,
                    last_notified_progress_milestone,
                    last_notified_r_milestone,
                    position_id,
                ))
            else:
                cursor.execute("""
                    UPDATE paper_positions
                    SET current_price = ?,
                        unrealized_pnl = ?,
                        r_multiple = ?,
                        target_progress_pct = ?,
                        last_price_time = ?
                    WHERE position_id = ? AND status = 'OPEN'
                """, (
                    current_price,
                    unrealized_pnl,
                    r_multiple,
                    target_progress_pct,
                    price_timestamp,
                    position_id,
                ))
            conn.commit()
            return cursor.rowcount > 0

    def get_open_positions(self, user_id: Optional[str | int] = None) -> List[Dict[str, Any]]:
        """
        Retrieves open positions. If user_id is provided, scopes strictly to that user.
        If user_id is None, returns open positions across all users (for scanner SL/TP evaluation).
        """
        with self._get_connection() as conn:
            if user_id is not None:
                uid = str(user_id).strip()
                rows = conn.cursor().execute(
                    "SELECT * FROM paper_positions WHERE user_id = ? AND status = 'OPEN' ORDER BY position_id DESC",
                    (uid,)
                ).fetchall()
            else:
                rows = conn.cursor().execute(
                    "SELECT * FROM paper_positions WHERE status = 'OPEN' ORDER BY position_id DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_open_paper_positions_count(self, user_id: Optional[str | int] = None) -> int:
        with self._get_connection() as conn:
            if user_id is not None:
                uid = str(user_id).strip()
                row = conn.cursor().execute(
                    "SELECT COUNT(*) as cnt FROM paper_positions WHERE user_id = ? AND status = 'OPEN'",
                    (uid,)
                ).fetchone()
            else:
                row = conn.cursor().execute(
                    "SELECT COUNT(*) as cnt FROM paper_positions WHERE status = 'OPEN'"
                ).fetchone()
            return int(row["cnt"]) if row else 0

    def get_paper_position(self, position_id: int, user_id: Optional[str | int] = None) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            if user_id is not None:
                uid = str(user_id).strip()
                row = conn.cursor().execute(
                    "SELECT * FROM paper_positions WHERE position_id = ? AND user_id = ?",
                    (position_id, uid)
                ).fetchone()
            else:
                row = conn.cursor().execute(
                    "SELECT * FROM paper_positions WHERE position_id = ?",
                    (position_id,)
                ).fetchone()
            return dict(row) if row else None

    def close_paper_trade(
        self,
        position_id: int,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        fee_paid: float = 0.0,
        user_id: Optional[str | int] = None,
    ) -> bool:
        """Standard position closure with balance update."""
        uid = str(user_id).strip() if user_id is not None else None
        with self._get_connection() as conn:
            now = datetime.now(timezone.utc).isoformat()
            if uid:
                cur = conn.cursor().execute("""
                    UPDATE paper_positions 
                    SET status = 'CLOSED', exit_time = ?, exit_price = ?, exit_reason = ?, pnl = ?, fee_paid = fee_paid + ?
                    WHERE position_id = ? AND user_id = ?
                """, (now, exit_price, exit_reason, pnl, fee_paid, position_id, uid))
                if cur.rowcount == 0:
                    return False
            else:
                cur = conn.cursor().execute("""
                    UPDATE paper_positions 
                    SET status = 'CLOSED', exit_time = ?, exit_price = ?, exit_reason = ?, pnl = ?, fee_paid = fee_paid + ?
                    WHERE position_id = ?
                """, (now, exit_price, exit_reason, pnl, fee_paid, position_id))
                if cur.rowcount == 0:
                    return False
                # Discover owner for balance update
                p_row = conn.cursor().execute("SELECT user_id FROM paper_positions WHERE position_id = ?", (position_id,)).fetchone()
                uid = p_row["user_id"] if p_row else self.primary_owner_id

            # Update current balance for this user
            row = conn.cursor().execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = 'current_balance'",
                (uid,)
            ).fetchone()
            cur_bal = float(row["value"]) if row else 10000.0
            new_bal = cur_bal + pnl
            conn.cursor().execute(
                "INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, 'current_balance', ?)",
                (uid, str(new_bal))
            )
            conn.commit()
            return True

    def close_paper_trade_atomic(
        self,
        position_id: int,
        user_id: str | int,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        fee_paid: float = 0.0,
        notification_event: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Atomically closes a paper trade position within a single database transaction:
        1. Verifies position is currently status = 'OPEN' and belongs to user_id.
        2. Updates position to status = 'CLOSED', recording exit_price, exit_reason, pnl, fee.
        3. Updates user's current_balance in user_settings (+pnl).
        4. Persists an outbox notification event for reliable, idempotent dispatch.
        Returns True if successfully closed, False if position was not OPEN or did not belong to user.
        """
        uid = str(user_id).strip()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            
            # 1. Update position ONLY IF OPEN and matches user_id
            cursor.execute("""
                UPDATE paper_positions
                SET status = 'CLOSED', exit_time = ?, exit_price = ?, exit_reason = ?, pnl = ?, fee_paid = fee_paid + ?
                WHERE position_id = ? AND user_id = ? AND status = 'OPEN'
            """, (now, exit_price, exit_reason, pnl, fee_paid, position_id, uid))

            if cursor.rowcount == 0:
                return False  # Position was already closed, non-existent, or belonged to another user!

            # 2. Update user's current balance
            cursor.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = 'current_balance'",
                (uid,)
            )
            row = cursor.fetchone()
            cur_bal = float(row["value"]) if row else 10000.0
            new_bal = round(cur_bal + pnl, 2)
            cursor.execute("""
                INSERT OR REPLACE INTO user_settings (user_id, key, value)
                VALUES (?, 'current_balance', ?)
            """, (uid, str(new_bal)))

            # 3. Create persistent Outbox Notification event
            if notification_event:
                cursor.execute("SELECT chat_id FROM users WHERE user_id = ?", (uid,))
                u_row = cursor.fetchone()
                chat_id = u_row["chat_id"] if u_row else uid
                cursor.execute("""
                    INSERT INTO outbox_notifications 
                    (user_id, chat_id, event_type, payload, status, retry_count, created_at)
                    VALUES (?, ?, ?, ?, 'PENDING', 0, ?)
                """, (
                    uid,
                    chat_id,
                    notification_event.get("event_type", "TRADE_CLOSED"),
                    json.dumps(notification_event),
                    now
                ))

            conn.commit()
            return True

    def get_closed_positions(self, user_id: Optional[str | int] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if user_id is not None:
                uid = str(user_id).strip()
                rows = conn.cursor().execute(
                    "SELECT * FROM paper_positions WHERE user_id = ? AND status = 'CLOSED' ORDER BY position_id DESC",
                    (uid,)
                ).fetchall()
            else:
                rows = conn.cursor().execute(
                    "SELECT * FROM paper_positions WHERE status = 'CLOSED' ORDER BY position_id DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    # ==========================================
    # --- OUTBOX NOTIFICATIONS API ---
    # ==========================================
    def get_pending_outbox_notifications(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.cursor().execute("""
                SELECT * FROM outbox_notifications 
                WHERE status = 'PENDING' AND retry_count < 5
                ORDER BY id ASC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def mark_outbox_notification_sent(self, notification_id: int):
        with self._get_connection() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.cursor().execute(
                "UPDATE outbox_notifications SET status = 'SENT', sent_at = ? WHERE id = ?",
                (now, notification_id)
            )
            conn.commit()

    def mark_outbox_notification_failed(self, notification_id: int, error_msg: str):
        with self._get_connection() as conn:
            conn.cursor().execute("""
                UPDATE outbox_notifications 
                SET retry_count = retry_count + 1,
                    status = CASE WHEN retry_count + 1 >= 5 THEN 'FAILED' ELSE 'PENDING' END
                WHERE id = ?
            """, (notification_id,))
            conn.commit()

    # ==========================================
    # --- DASHBOARD AUTH TOKENS & SESSIONS API ---
    # ==========================================
    def create_dashboard_token(self, user_id: str | int, validity_minutes: int = 15) -> str:
        """Generates a cryptographically secure, single-use token and stores its SHA-256 hash."""
        uid = str(user_id).strip()
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=validity_minutes)

        with self._get_connection() as conn:
            conn.cursor().execute("""
                INSERT INTO dashboard_auth_tokens (token_hash, user_id, created_at, expires_at, is_used)
                VALUES (?, ?, ?, ?, 0)
            """, (token_hash, uid, now.isoformat(), expires.isoformat()))
            conn.commit()

        return raw_token

    def consume_dashboard_token(self, raw_token: str) -> Optional[str]:
        """
        Validates token hash, checks expiry, marks it as used (single-use),
        and returns the authenticated user_id.
        """
        token_hash = hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute("""
                SELECT user_id, expires_at, is_used FROM dashboard_auth_tokens 
                WHERE token_hash = ?
            """, (token_hash,)).fetchone()

            if not row or row["is_used"] != 0:
                return None

            expires_dt = datetime.fromisoformat(row["expires_at"])
            if datetime.now(timezone.utc) > expires_dt:
                return None

            cursor.execute("UPDATE dashboard_auth_tokens SET is_used = 1 WHERE token_hash = ?", (token_hash,))
            conn.commit()
            return row["user_id"]

    def create_outbox_notification(
        self,
        user_id: str | int,
        chat_id: str | int,
        event_type: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Helper method to explicitly queue an outbox notification."""
        uid = str(user_id).strip()
        cid = str(chat_id).strip()
        body = payload or {}
        if "message" not in body:
            body["message"] = message
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor().execute("""
                INSERT INTO outbox_notifications 
                (user_id, chat_id, event_type, payload, status, retry_count, created_at)
                VALUES (?, ?, ?, ?, 'PENDING', 0, ?)
            """, (uid, cid, event_type, json.dumps(body), now))
            conn.commit()
            return cur.lastrowid

    def create_dashboard_session(
        self,
        user_id: str | int,
        validity_hours: int = 24,
        duration_days: Optional[int] = None,
    ) -> str:
        """Creates a server-side session for an authenticated user."""
        uid = str(user_id).strip()
        session_id = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        if duration_days is not None:
            expires = now + timedelta(days=duration_days)
        else:
            expires = now + timedelta(hours=validity_hours)

        with self._get_connection() as conn:
            conn.cursor().execute("""
                INSERT INTO dashboard_sessions (session_id, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
            """, (session_id, uid, now.isoformat(), expires.isoformat()))
            conn.commit()

        return session_id

    def get_dashboard_session_user(self, session_id: str) -> Optional[str]:
        """Validates session cookie and returns user_id if active."""
        if not session_id:
            return None
        with self._get_connection() as conn:
            row = conn.cursor().execute("""
                SELECT user_id, expires_at FROM dashboard_sessions 
                WHERE session_id = ?
            """, (session_id.strip(),)).fetchone()

            if not row:
                return None

            expires_dt = datetime.fromisoformat(row["expires_at"])
            if datetime.now(timezone.utc) > expires_dt:
                # Expired session cleanup
                conn.cursor().execute("DELETE FROM dashboard_sessions WHERE session_id = ?", (session_id.strip(),))
                conn.commit()
                return None

            return row["user_id"]

    def delete_dashboard_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        with self._get_connection() as conn:
            cur = conn.cursor().execute("DELETE FROM dashboard_sessions WHERE session_id = ?", (session_id.strip(),))
            conn.commit()
            return cur.rowcount > 0
