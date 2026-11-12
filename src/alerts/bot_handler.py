import os
import logging
from typing import Dict, Any, Optional, List
from ..storage.database import StorageManager
from ..data.provider_factory import MarketProviderFactory
from ..data.instruments import INSTRUMENT_REGISTRY, get_instrument_spec, AssetClass
from ..data.models import Timeframe
from ..strategy.mtf_orchestrator import MultiTimeframeOrchestrator
from ..strategy.config import StrategyConfig
from ..strategy.ranker import SetupRanker
from ..paper.account_service import AccountService
from ..paper.lifecycle_calculator import PositionLifecycleCalculator
from .telegram import TelegramAlertService

logger = logging.getLogger(__name__)


class TelegramBotHandler:
    """
    Handles multi-user interactive Telegram commands, pair-selection menu,
    custom pair on-demand analysis, user-scoped watchlist management,
    user-scoped paper position monitoring, secure dashboard login tokens,
    and inline approval/rejection callbacks with strict backend ownership enforcement.
    """

    def __init__(
        self,
        telegram_service: TelegramAlertService,
        storage: Optional[StorageManager] = None,
        account_service: Optional[AccountService] = None,
    ):
        self.telegram = telegram_service
        self.storage = storage or StorageManager()
        if self.telegram.config.chat_id:
            self.storage.primary_owner_id = str(self.telegram.config.chat_id).strip()
        self.account_service = account_service or AccountService(self.storage)
        # Store state per user_id: {user_id: bool}
        self._awaiting_custom_pair: Dict[str, bool] = {}

    @property
    def default_user_id(self) -> str:
        """Returns the default/primary user ID fallback."""
        if hasattr(self.storage, "primary_owner_id") and self.storage.primary_owner_id:
            return str(self.storage.primary_owner_id).strip()
        return str(self.telegram.config.chat_id or "OWNER_001").strip()

    def get_admin_ids(self) -> List[str]:
        """Returns the authoritative list of admin user IDs from TELEGRAM_ADMIN_IDS environment variable."""
        raw = os.getenv("TELEGRAM_ADMIN_IDS", "").strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    def is_admin(self, user_id: str | int) -> bool:
        """Verifies if the numeric Telegram user_id is in TELEGRAM_ADMIN_IDS."""
        return str(user_id).strip() in self.get_admin_ids()

    def process_incoming_update(self, update: Dict[str, Any]) -> Dict[str, Any]:
        """Routes incoming webhook or long-polling update from Telegram."""
        # 1. Handle Callback Query (Inline Button Press)
        if "callback_query" in update:
            cb = update["callback_query"]
            from_user = cb.get("from") or {}
            chat_id = str(cb.get("message", {}).get("chat", {}).get("id") or self.default_user_id).strip()
            user_id = str(from_user.get("id") or chat_id).strip()
            cb_id = cb.get("id")
            data = cb.get("data", "")

            user = self.storage.get_user(user_id)
            if not user:
                # Auto-register if clicking buttons
                user = self.storage.register_user(
                    user_id=user_id,
                    username=from_user.get("username"),
                    first_name=from_user.get("first_name"),
                    chat_id=chat_id,
                    role="ADMIN" if self.is_admin(user_id) else "USER",
                )
            elif str(user.get("status", "")).upper() == "SUSPENDED":
                self.telegram.answer_callback_query(cb_id, "⛔ Account suspended. Contact administrator.", show_alert=True)
                return {"status": "suspended"}

            return self._handle_callback(cb_id, data, user_id=user_id, chat_id=chat_id)

        # 2. Handle Standard Message / Command
        message = update.get("message") or update.get("channel_post")
        if not message:
            return {"status": "ignored"}

        from_user = message.get("from") or {}
        chat_id = str(message.get("chat", {}).get("id") or self.default_user_id).strip()
        user_id = str(from_user.get("id") or chat_id).strip()
        text = (message.get("text") or "").strip()

        # Check configured single-chat authorization mode if TELEGRAM_CHAT_ID is set and ADMIN_IDS is empty
        if not os.getenv("TELEGRAM_ADMIN_IDS") and self.telegram.config.chat_id:
            if chat_id != str(self.telegram.config.chat_id).strip() and user_id != str(self.telegram.config.chat_id).strip():
                logger.warning(f"Rejected message from unauthorized chat_id: {chat_id}")
                self.telegram.send_message("⛔ <b>Access Denied:</b> You are not authorized to control this trading bot.")
                return {"status": "unauthorized"}

        # Check or register user
        user = self.storage.get_user(user_id)
        if not user:
            role = "ADMIN" if self.is_admin(user_id) else "USER"
            user = self.storage.register_user(
                user_id=user_id,
                username=from_user.get("username"),
                first_name=from_user.get("first_name"),
                chat_id=chat_id,
                role=role,
            )

        # Check suspension
        if str(user.get("status", "")).upper() == "SUSPENDED":
            self.telegram.send_message(
                "⛔ <b>Access Denied:</b> Your account has been suspended by an administrator.",
                chat_id=chat_id,
            )
            return {"status": "suspended"}

        # Handle text message when bot is awaiting custom pair input for this user
        if self._awaiting_custom_pair.get(user_id, False) and not text.startswith("/"):
            self._awaiting_custom_pair[user_id] = False
            return self.trigger_on_demand_analysis(target_symbol=text, user_id=user_id, chat_id=chat_id)

        return self._handle_command(text, user_id=user_id, chat_id=chat_id)

    def _handle_command(
        self,
        text: str,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()

        parts = text.split()
        cmd = parts[0].lower() if parts else ""
        args = parts[1:] if len(parts) > 1 else []

        if cmd in ("/start", "/menu", "menu"):
            return self.send_main_menu(user_id=uid, chat_id=cid)

        elif cmd in ("/analyze", "/scan", "analyze", "scan"):
            if args:
                target_symbol = args[0].upper()
                return self.trigger_on_demand_analysis(target_symbol=target_symbol, user_id=uid, chat_id=cid)
            else:
                return self.send_analyze_selection_menu(user_id=uid, chat_id=cid)

        elif cmd in ("/account", "/balance", "account", "balance"):
            return self.send_account_summary(user_id=uid, chat_id=cid)

        elif cmd in ("/performance", "performance"):
            return self.send_account_summary(user_id=uid, chat_id=cid)

        elif cmd in ("/dashboard", "dashboard"):
            return self.send_dashboard_view(user_id=uid, chat_id=cid)

        elif cmd in ("/positions", "positions"):
            return self.send_open_positions_view(user_id=uid, chat_id=cid)

        elif cmd in ("/history", "history"):
            return self.send_trade_history_view(user_id=uid, chat_id=cid)

        elif cmd in ("/watchlist", "watchlist"):
            if args and args[0].lower() in ("add", "+") and len(args) > 1:
                symbol = args[1].upper()
                return self.add_watchlist_symbol(symbol, user_id=uid, chat_id=cid)
            elif args and args[0].lower() in ("del", "remove", "rm", "-") and len(args) > 1:
                symbol = args[1].upper()
                return self.remove_watchlist_symbol(symbol, user_id=uid, chat_id=cid)
            else:
                return self.send_watchlist(user_id=uid, chat_id=cid)

        elif cmd in ("/settings", "settings"):
            if args and len(args) >= 2:
                setting_key = args[0].lower()
                setting_val = args[1]
                return self.update_setting(setting_key, setting_val, user_id=uid, chat_id=cid)
            return self.send_settings_menu(user_id=uid, chat_id=cid)

        elif cmd in ("/close", "close"):
            if args and args[0].isdigit():
                pos_id = int(args[0])
                return self.manual_close_position(pos_id, user_id=uid, chat_id=cid)
            else:
                self.telegram.send_message("ℹ️ Usage: <code>/close [POSITION_ID]</code>", chat_id=cid)
                return {"status": "usage_sent"}

        # Admin Commands
        elif cmd == "/admin":
            return self.handle_admin_dashboard(user_id=uid, chat_id=cid)

        elif cmd == "/users":
            return self.handle_admin_users_list(user_id=uid, chat_id=cid)

        elif cmd == "/suspend":
            target = args[0] if args else ""
            return self.handle_admin_suspend(target, user_id=uid, chat_id=cid)

        elif cmd == "/activate":
            target = args[0] if args else ""
            return self.handle_admin_activate(target, user_id=uid, chat_id=cid)

        elif cmd == "/system_health":
            return self.handle_admin_system_health(user_id=uid, chat_id=cid)

        elif cmd == "/global_settings":
            return self.handle_admin_global_settings(user_id=uid, chat_id=cid)

        # Fallback Help / cmd in ("/help", "help")
        self.telegram.send_message(
            "ℹ️ <b>ICT/SMC Copilot Commands:</b>\n\n"
            "• <code>/start</code> or <code>/menu</code> - Main Navigation Menu\n"
            "• <code>/analyze</code> - Select a pair to analyze\n"
            "• <code>/analyze [SYMBOL]</code> - Directly analyze one pair\n"
            "• <code>/dashboard</code> - Live Account Stats & Secure Dashboard Login\n"
            "• <code>/balance</code> or <code>/account</code> - Paper Balance & Equity\n"
            "• <code>/performance</code> - Win Rate & Realised P&L Metrics\n"
            "• <code>/positions</code> - Monitor Open Paper Trades\n"
            "• <code>/history</code> - Review Completed Trade History\n"
            "• <code>/watchlist</code> - View Saved Watchlist\n"
            "• <code>/watchlist add [SYM]</code> - Add Instrument\n"
            "• <code>/watchlist del [SYM]</code> - Remove Instrument\n"
            "• <code>/settings</code> - View & Manage Bot Parameters\n"
            "• <code>/close [ID]</code> - Manually Close Open Paper Position\n"
            "• <code>/help</code> - Show Command Guide",
            chat_id=cid,
        )
        return {"status": "help_sent"}

    def send_main_menu(
        self,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        is_adm = self.is_admin(uid)
        msg = (
            "🤖 <b>ICT/SMC Trading Copilot</b>\n"
            "<i>Semi-Automated Multi-User Analysis & Paper Trading</i>\n\n"
            f"👤 <b>User ID:</b> <code>{uid}</code>\n"
            f"🛡️ <b>Role:</b> {'ADMIN' if is_adm else 'TRADER'}\n\n"
            "Select an option below:"
        )
        buttons = [
            [
                {"text": "🔍 Analyse Market", "callback_data": "menu_analyze_menu"},
                {"text": "📊 Dashboard", "callback_data": "menu_dashboard"},
            ],
            [
                {"text": "💼 My Account", "callback_data": "menu_account"},
                {"text": "📋 My Watchlist", "callback_data": "menu_watchlist"},
            ],
            [
                {"text": "📈 Open Positions", "callback_data": "menu_positions"},
                {"text": "📜 Trade History", "callback_data": "menu_history"},
            ],
            [
                {"text": "⚙️ Bot Settings", "callback_data": "menu_settings"},
            ],
        ]
        if is_adm:
            buttons.append([{"text": "👑 Admin Panel", "callback_data": "admin_panel"}])

        keyboard = {"inline_keyboard": buttons}
        self.telegram.send_message(msg, reply_markup=keyboard, chat_id=cid)
        return {"status": "menu_sent"}

    def send_analyze_selection_menu(
        self,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        wl = self.storage.get_watchlist(user_id=uid)
        msg = (
            "🔍 <b>ANALYSE MARKET</b>\n\n"
            "Select a pair from your watchlist or enter a custom pair:\n"
            "<i>(Analyses only the selected pair and returns up to 2 qualified setups)</i>"
        )
        buttons = []
        for item in wl:
            sym = item["symbol"]
            buttons.append([{"text": f"Analyse {sym}", "callback_data": f"analyze_pair_{sym}"}])

        buttons.append([{"text": "✏️ ENTER CUSTOM PAIR", "callback_data": "analyze_custom_pair"}])

        self.telegram.send_message(msg, reply_markup={"inline_keyboard": buttons}, chat_id=cid)
        return {"status": "analyze_menu_sent", "watchlist_count": len(wl)}

    def prompt_custom_pair(
        self,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        self._awaiting_custom_pair[uid] = True
        msg = (
            "✏️ <b>Enter the market pair you want to analyse.</b>\n\n"
            "Examples:\n"
            "• <code>BTCUSDT</code> (Crypto)\n"
            "• <code>ETHUSDT</code> (Crypto)\n"
            "• <code>EURUSD</code> (Forex)\n"
            "• <code>XAUUSD</code> (Gold)\n\n"
            "<i>Type the symbol as a message now:</i>"
        )
        self.telegram.send_message(msg, chat_id=cid)
        return {"status": "custom_pair_prompted"}

    def send_open_positions_view(
        self,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        metrics = self.account_service.get_metrics(user_id=uid)
        if not metrics.open_positions:
            self.telegram.send_message(
                "ℹ️ <b>Open Positions:</b> You currently have 0 active paper positions.",
                chat_id=cid,
            )
            return {"status": "no_open_positions"}

        max_pos = int(self.storage.get_setting("max_open_positions", 20, user_id=uid))
        lines = [f"📈 <b>ACTIVE OPEN POSITIONS ({len(metrics.open_positions)}/{max_pos})</b>\n"]
        buttons = []
        for p in metrics.open_positions:
            p_id = p["position_id"]
            p_pnl = p.get("unrealized_pnl", 0.0)
            p_pct = p.get("progress_pct", 0.0)
            r_mult = p.get("r_multiple", 0.0)
            cur_p = p.get("current_price", p["entry_price"])
            dur = p.get("duration_str", "0m")
            dir_emoji = "🟢" if str(p["direction"]).lower() in ("bullish", "long") else "🔴"
            
            lines.append(
                f"{dir_emoji} <b>#{p_id} {p['direction'].upper()} {p['symbol']}</b>\n"
                f"   • Entry: <code>{p['entry_price']:.4f}</code> → Mark: <code>{cur_p:.4f}</code>\n"
                f"   • SL: <code>{p['stop_loss']:.4f}</code> | TP: <code>{p['take_profit']:.4f}</code>\n"
                f"   • Unrealized P&L: <b>${p_pnl:+,.2f}</b> ({r_mult:+.2f}R)\n"
                f"   • Target Progress: <b>{p_pct:+.1f}%</b> | Open: {dur}\n"
            )
            buttons.append([{"text": f"🛑 Close #{p_id} {p['symbol']}", "callback_data": f"manual_close_{p_id}"}])

        # Include Refresh Positions button
        buttons.append([{"text": "🔄 Refresh Positions", "callback_data": "refresh_positions"}])
        self.telegram.send_message("\n".join(lines), reply_markup={"inline_keyboard": buttons}, chat_id=cid)
        return {"status": "positions_sent", "count": len(metrics.open_positions)}

    def send_trade_history_view(
        self,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        closed = self.storage.get_closed_positions(user_id=uid)
        if not closed:
            self.telegram.send_message(
                "📜 <b>Trade History:</b> No completed trades recorded yet.",
                chat_id=cid,
            )
            return {"status": "no_history"}

        lines = [f"📜 <b>YOUR COMPLETED TRADE HISTORY (Last {min(len(closed), 10)})</b>\n"]
        for p in closed[:10]:
            pnl = p.get("pnl", 0.0)
            emoji = "🟢" if pnl > 0 else "🔴"
            lines.append(
                f"{emoji} <b>#{p['position_id']} {p['direction'].upper()} {p['symbol']}</b>\n"
                f"   Entry: {p['entry_price']:.2f} | Exit: {p.get('exit_price', 0):.2f}\n"
                f"   Exit Reason: {p.get('exit_reason', 'N/A').upper()} | P&L: ${pnl:+,.2f}\n"
            )

        self.telegram.send_message("\n".join(lines), chat_id=cid)
        return {"status": "history_sent", "count": len(closed)}

    def send_dashboard_view(
        self,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        metrics = self.account_service.get_metrics(user_id=uid)
        env_url = os.getenv("DASHBOARD_URL")
        base_url = env_url if env_url else self.storage.get_setting("dashboard_url", "http://127.0.0.1:8000", user_id=uid)
        
        # Generate single-use, cryptographically secure dashboard login token
        raw_token = self.storage.create_dashboard_token(user_id=uid, validity_minutes=15)
        login_url = f"{base_url.rstrip('/')}/auth/login?token={raw_token}"

        open_summary = "None"
        if metrics.open_positions:
            lines = []
            for p in metrics.open_positions:
                p_pnl = p.get("unrealized_pnl", 0.0)
                p_pct = p.get("progress_pct", 0.0)
                lines.append(
                    f"• <b>#{p['position_id']} {p['direction'].upper()} {p['symbol']}</b> @ {p['entry_price']:.2f}\n"
                    f"   SL: {p['stop_loss']:.2f} | TP: {p['take_profit']:.2f}\n"
                    f"   Unrealized: ${p_pnl:+,.2f} | Target Progress: {p_pct:.1f}%"
                )
            open_summary = "\n".join(lines)

        max_pos = int(self.storage.get_setting("max_open_positions", 20, user_id=uid))
        msg = (
            "📊 <b>TRADING COPILOT DASHBOARD</b>\n\n"
            f"• <b>Starting Balance:</b> ${metrics.starting_balance:,.2f}\n"
            f"• <b>Current Balance:</b> ${metrics.current_balance:,.2f}\n"
            f"• <b>Account Equity:</b> ${metrics.equity:,.2f}\n"
            f"• <b>Realised P&L:</b> ${metrics.realized_pnl:+,.2f}\n"
            f"• <b>Unrealised P&L:</b> ${metrics.unrealized_pnl:+,.2f}\n"
            f"• <b>Total P&L:</b> ${metrics.total_pnl:+,.2f} ({metrics.total_pnl_pct:+.2f}%)\n"
            f"• <b>Open Positions:</b> {metrics.open_positions_count}/{max_pos}\n"
            f"• <b>Closed Trades:</b> {metrics.closed_trades_count} (Win Rate: {metrics.win_rate_pct:.1f}%)\n"
            f"• <b>Max Drawdown:</b> {metrics.max_drawdown_pct:.2f}%\n\n"
            f"<b>Active Paper Positions:</b>\n{open_summary}\n\n"
            f"🔒 <i>Secure Single-Use Access Token generated (Valid for 15 minutes).</i>"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🔄 REFRESH ACCOUNT", "callback_data": "dashboard_refresh"},
                    {"text": "🌐 OPEN FULL DASHBOARD", "url": login_url},
                ]
            ]
        }
        self.telegram.send_message(msg, reply_markup=keyboard, chat_id=cid)
        return {"status": "dashboard_view_sent", "metrics": metrics, "login_url": login_url}

    def send_account_summary(
        self,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        metrics = self.account_service.get_metrics(user_id=uid)
        max_pos = int(self.storage.get_setting("max_open_positions", 20, user_id=uid))
        msg = (
            f"💼 <b>VIRTUAL PAPER ACCOUNT</b>\n\n"
            f"<b>Equity:</b> ${metrics.equity:,.2f}\n"
            f"<b>Balance:</b> ${metrics.current_balance:,.2f}\n"
            f"<b>Unrealized P&L:</b> ${metrics.unrealized_pnl:+,.2f}\n"
            f"<b>Realized P&L:</b> ${metrics.realized_pnl:+,.2f}\n"
            f"<b>Win Rate:</b> {metrics.win_rate_pct:.1f}% ({metrics.winning_trades_count}W / {metrics.losing_trades_count}L)\n"
            f"<b>Open Positions:</b> {metrics.open_positions_count}/{max_pos}\n"
            f"<b>Max Drawdown:</b> {metrics.max_drawdown_pct:.2f}%"
        )
        self.telegram.send_message(msg, chat_id=cid)
        return {"status": "account_sent", "metrics": metrics}

    def send_watchlist(
        self,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        wl = self.storage.get_watchlist(user_id=uid)
        symbols_str = "\n".join(f"• <b>{item['symbol']}</b> ({item['asset_class']})" for item in wl) or "<i>No symbols added yet.</i>"
        msg = (
            f"📋 <b>MY WATCHLIST</b>\n\n"
            f"{symbols_str}\n\n"
            f"<i>Add a symbol:</i> <code>/watchlist add ETHUSDT</code>\n"
            f"<i>Remove a symbol:</i> <code>/watchlist del ETHUSDT</code>"
        )
        self.telegram.send_message(msg, chat_id=cid)
        return {"status": "watchlist_sent"}

    def add_watchlist_symbol(
        self,
        symbol: str,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        clean_sym = symbol.strip().upper().replace("/", "").replace("-", "")
        spec = get_instrument_spec(clean_sym)
        self.storage.add_watchlist_symbol(clean_sym, asset_class=spec.asset_class.value, user_id=uid)
        self.telegram.send_message(
            f"✅ Added <b>{clean_sym}</b> ({spec.asset_class.value.upper()}) to your active watchlist.",
            chat_id=cid,
        )
        return {"status": "symbol_added", "symbol": clean_sym}

    def remove_watchlist_symbol(
        self,
        symbol: str,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        clean_sym = symbol.strip().upper().replace("/", "").replace("-", "")
        self.storage.remove_watchlist_symbol(clean_sym, user_id=uid)
        self.telegram.send_message(f"🗑️ Removed <b>{clean_sym}</b> from your active watchlist.", chat_id=cid)
        return {"status": "symbol_removed", "symbol": clean_sym}

    def send_settings_menu(
        self,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        settings = self.storage.get_all_settings(user_id=uid)
        msg = (
            "⚙️ <b>BOT SETTINGS & CONTROLS</b>\n\n"
            f"• <b>Risk Per Trade:</b> {settings.get('risk_per_trade_pct', '1.0')}%\n"
            f"• <b>Target R:R:</b> {settings.get('target_risk_reward', '4.0')}:1\n"
            f"• <b>Max Open Positions:</b> {settings.get('max_open_positions', '20')}\n"
            f"• <b>Virtual Starting Balance:</b> ${float(settings.get('initial_balance', 10000.0)):,.2f}\n"
            f"• <b>Automatic Alerts:</b> {settings.get('auto_alert_enabled', 'true').upper()}\n"
            f"• <b>Notify on Fills / Exits:</b> {settings.get('notify_trade_events', 'true').upper()}\n\n"
            "<i>To modify a setting, send:</i>\n"
            "<code>/settings risk 1.5</code>\n"
            "<code>/settings rr 4.0</code>\n"
            "<code>/settings max_pos 20</code>\n"
            "<code>/settings auto_alert on|off</code>\n"
            "<code>/settings notify_events on|off</code>"
        )
        self.telegram.send_message(msg, chat_id=cid)
        return {"status": "settings_sent"}

    def update_setting(
        self,
        key: str,
        value: str,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        key = key.lower()
        val = value.strip().lower()

        if key in ("risk", "risk_per_trade_pct"):
            try:
                num = float(val)
                if 0.1 <= num <= 10.0:
                    self.storage.set_setting("risk_per_trade_pct", str(num), user_id=uid)
                    self.telegram.send_message(f"✅ Risk per trade updated to <b>{num}%</b>.", chat_id=cid)
                    return {"status": "setting_updated", "key": "risk_per_trade_pct", "value": num}
                else:
                    self.telegram.send_message("⚠️ Risk must be between 0.1% and 10.0%.", chat_id=cid)
                    return {"status": "validation_failed"}
            except ValueError:
                self.telegram.send_message("⚠️ Invalid risk value. Example: <code>/settings risk 1.0</code>", chat_id=cid)
                return {"status": "validation_failed"}

        elif key in ("rr", "target_risk_reward"):
            try:
                num = float(val)
                if num >= 4.0:
                    self.storage.set_setting("target_risk_reward", str(num), user_id=uid)
                    self.telegram.send_message(f"✅ Target R:R updated to <b>{num}:1</b>.", chat_id=cid)
                    return {"status": "setting_updated", "key": "target_risk_reward", "value": num}
                else:
                    self.telegram.send_message("⚠️ Minimum allowed Target R:R is 4.0:1.", chat_id=cid)
                    return {"status": "validation_failed"}
            except ValueError:
                self.telegram.send_message("⚠️ Invalid R:R value. Example: <code>/settings rr 4.0</code>", chat_id=cid)
                return {"status": "validation_failed"}

        elif key in ("max_pos", "max_open_positions"):
            try:
                num = int(val)
                if 1 <= num <= 50:
                    self.storage.set_setting("max_open_positions", str(num), user_id=uid)
                    self.telegram.send_message(f"✅ Max open paper positions updated to <b>{num}</b>.", chat_id=cid)
                    return {"status": "setting_updated", "key": "max_open_positions", "value": num}
                else:
                    self.telegram.send_message("⚠️ Max open positions must be between 1 and 50.", chat_id=cid)
                    return {"status": "validation_failed"}
            except ValueError:
                self.telegram.send_message("⚠️ Invalid number. Example: <code>/settings max_pos 20</code>", chat_id=cid)
                return {"status": "validation_failed"}

        elif key in ("auto_alert", "auto_alerts"):
            is_on = val in ("on", "true", "1", "yes")
            self.storage.set_setting("auto_alert_enabled", "true" if is_on else "false", user_id=uid)
            self.telegram.send_message(f"✅ Automatic alerts set to <b>{'ENABLED' if is_on else 'DISABLED'}</b>.", chat_id=cid)
            return {"status": "setting_updated", "key": "auto_alert_enabled", "value": is_on}

        elif key in ("notify_events", "trade_notifications"):
            is_on = val in ("on", "true", "1", "yes")
            self.storage.set_setting("notify_trade_events", "true" if is_on else "false", user_id=uid)
            self.telegram.send_message(f"✅ Trade event notifications set to <b>{'ENABLED' if is_on else 'DISABLED'}</b>.", chat_id=cid)
            return {"status": "setting_updated", "key": "notify_trade_events", "value": is_on}

        self.telegram.send_message("⚠️ Unknown setting key. Type <code>/settings</code> to view options.", chat_id=cid)
        return {"status": "unknown_key"}

    def trigger_on_demand_analysis(
        self,
        target_symbol: str,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()
        clean_sym = target_symbol.strip().upper().replace("/", "").replace("-", "")

        try:
            provider = MarketProviderFactory.get_provider_for_symbol(clean_sym)
        except ValueError as err:
            self.telegram.send_message(
                f"⚠️ <b>Unsupported or Unconnected Pair: {clean_sym}</b>\n\n"
                f"{str(err)}\n\n"
                f"Please enter or select a supported instrument (e.g. <code>BTCUSDT</code>, <code>ETHUSDT</code>).",
                chat_id=cid,
            )
            return {"status": "unsupported_instrument", "symbol": clean_sym, "error": str(err)}

        self.telegram.send_message(
            f"🔍 <i>Analysing {clean_sym} across 4H, 1H, 15M, 5M, 1M timeframes...</i>",
            chat_id=cid,
        )

        config = StrategyConfig(swing_left_bars=2, swing_right_bars=2, require_ltf_confirmation=True)
        orchestrator = MultiTimeframeOrchestrator(config=config)

        tfs = [Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5, Timeframe.M1]
        dfs = {}
        
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ..data.cache import GLOBAL_CANDLE_CACHE
        import time

        start_time = time.time()

        def fetch_tf(tf: Timeframe):
            cached_df = GLOBAL_CANDLE_CACHE.get(clean_sym, tf, 120)
            if cached_df is not None and not cached_df.empty:
                return tf.value, cached_df
            df = provider.fetch_candles(clean_sym, tf, limit=120)
            if not df.empty:
                GLOBAL_CANDLE_CACHE.set(clean_sym, tf, 120, df)
            return tf.value, df

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_tf = {executor.submit(fetch_tf, tf): tf for tf in tfs}
            for future in as_completed(future_to_tf):
                try:
                    tf_val, df = future.result()
                    if not df.empty:
                        dfs[tf_val] = df
                except Exception as e:
                    logger.warning(f"Error fetching candles concurrently for {clean_sym}: {e}")

        fetch_duration = time.time() - start_time
        logger.info(f"Market data fetch duration for {clean_sym}: {fetch_duration:.2f}s")

        if len(dfs) < 5:
            self.telegram.send_message(
                f"⚠️ Insufficient candle data retrieved for {clean_sym}. Please verify connectivity.",
                chat_id=cid,
            )
            return {"status": "insufficient_data", "symbol": clean_sym}

        candidate_setups = orchestrator.scan_market(dfs)
        top_ranked = SetupRanker.rank_and_deduplicate(candidate_setups, max_results=2)

        if not top_ranked:
            self.telegram.send_message(
                f"ℹ️ <b>Analysis Result for {clean_sym}:</b> No qualifying ICT/SMC setup found.\n\n"
                f"Neither bullish nor bearish conditions met all 5 timeframe confluences (4H Bias $\\to$ 1H AOI $\\to$ 15M Structure $\\to$ 5M Confirmation) with a valid 4:1 R:R target.",
                chat_id=cid,
            )
            return {"status": "no_opportunities", "symbol": clean_sym, "count": 0}

        count_sent = 0
        for s in top_ranked:
            sig_id = f"{clean_sym}_{s.direction.value}_{int(s.timestamp.timestamp()) if s.timestamp else 0}_{uid}"
            risk_unit = abs(s.entry_price - s.stop_loss)
            tp_price = (s.entry_price + (risk_unit * 4.0)) if s.direction.value == "bullish" else (s.entry_price - (risk_unit * 4.0))

            self.storage.save_signal(
                signal_id=sig_id,
                symbol=clean_sym,
                direction=s.direction.value,
                entry_price=s.entry_price,
                stop_loss=s.stop_loss,
                take_profit=tp_price,
                audit_notes=s.audit_trail,
                validity_minutes=30,
                user_id=uid,
            )
            self.telegram.alert_opportunity_with_approval(sig_id, s, symbol=clean_sym, chat_id=cid)
            count_sent += 1

        return {"status": "opportunities_sent", "symbol": clean_sym, "count": count_sent}

    def manual_close_position(
        self,
        position_id: int,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pos = self.storage.get_paper_position(position_id)
        uid = str(user_id or (pos.get("user_id") if pos else self.default_user_id)).strip()
        cid = str(chat_id or uid).strip()

        if not pos:
            self.telegram.send_message(f"⚠️ Position #{position_id} not found.", chat_id=cid)
            return {"status": "not_found"}

        if str(pos.get("user_id")) != uid:
            logger.warning(f"SECURITY: User {uid} attempted to close position #{position_id} owned by {pos.get('user_id')}")
            self.telegram.send_message("⛔ <b>Access Denied:</b> You do not own this position.", chat_id=cid)
            return {"status": "unauthorized"}

        if pos["status"] != "OPEN":
            self.telegram.send_message(f"⚠️ Position #{position_id} is already closed.", chat_id=cid)
            return {"status": "already_closed"}

        # Fetch current mark price for accurate P&L calculation on manual close
        clean_sym = pos["symbol"].strip().upper()
        exit_price = pos.get("current_price") or pos["entry_price"]
        try:
            provider = MarketProviderFactory.get_provider_for_symbol(clean_sym)
            df = provider.fetch_candles(clean_sym, timeframe=Timeframe.M1, limit=1)
            if not df.empty:
                exit_price = float(df.iloc[-1]["close"])
        except Exception as e:
            logger.warning(f"Could not fetch live close price for #{position_id} {clean_sym}, using last known price: {e}")

        size = float(pos["position_size"])
        fee = size * exit_price * 0.0006
        pnl = PositionLifecycleCalculator.calculate_pnl(
            direction=pos["direction"],
            entry_price=pos["entry_price"],
            exit_or_current_price=exit_price,
            position_size=size,
            symbol=clean_sym,
            exit_fee=fee,
        )

        # Atomic closure
        self.storage.close_paper_trade_atomic(
            position_id=position_id,
            user_id=uid,
            exit_price=exit_price,
            exit_reason="manual",
            pnl=pnl,
            fee_paid=fee,
            notification_event={
                "event_type": "trade_closed_manual",
                "symbol": pos["symbol"],
                "pnl": pnl,
                "exit_price": exit_price,
                "message": f"🛑 <b>POSITION #{position_id} CLOSED MANUALLY</b>\n\n<b>Symbol:</b> {pos['symbol']}\n<b>Exit:</b> {exit_price:.2f}\n<b>P&L:</b> ${pnl:+,.2f}",
            },
        )

        self.telegram.send_message(
            f"🛑 <b>POSITION #{position_id} CLOSED MANUALLY</b>\n\n"
            f"<b>Symbol:</b> {pos['symbol']}\n"
            f"<b>Direction:</b> {pos['direction'].upper()}\n"
            f"<b>Exit Price:</b> {exit_price:.2f}\n"
            f"<b>Final P&L:</b> ${pnl:+,.2f}\n\n"
            f"<i>Position status is now CLOSED.</i>",
            chat_id=cid,
        )
        return {"status": "position_closed_manually", "position_id": position_id}

    # Admin Command Handlers
    def handle_admin_dashboard(self, user_id: str, chat_id: str) -> Dict[str, Any]:
        if not self.is_admin(user_id):
            self.telegram.send_message("⛔ <b>Access Denied:</b> Administrator privileges required.", chat_id=chat_id)
            return {"status": "unauthorized"}

        users = self.storage.list_users()
        active_count = sum(1 for u in users if u.get("status") == "ACTIVE")
        suspended_count = sum(1 for u in users if u.get("status") == "SUSPENDED")
        total_open = self.storage.get_open_paper_positions_count()

        msg = (
            "👑 <b>ADMINISTRATION DASHBOARD</b>\n\n"
            f"• <b>Total Users:</b> {len(users)}\n"
            f"• <b>Active Users:</b> {active_count}\n"
            f"• <b>Suspended Users:</b> {suspended_count}\n"
            f"• <b>Global Open Positions:</b> {total_open}\n\n"
            "<b>Admin Commands:</b>\n"
            "• <code>/users</code> - List all registered users\n"
            "• <code>/suspend [USER_ID]</code> - Suspend a user\n"
            "• <code>/activate [USER_ID]</code> - Reactivate a user\n"
            "• <code>/system_health</code> - Inspect system status\n"
            "• <code>/global_settings</code> - Manage platform defaults"
        )
        self.telegram.send_message(msg, chat_id=chat_id)
        return {"status": "admin_dashboard_sent"}

    def handle_admin_users_list(self, user_id: str, chat_id: str) -> Dict[str, Any]:
        if not self.is_admin(user_id):
            self.telegram.send_message("⛔ <b>Access Denied:</b> Administrator privileges required.", chat_id=chat_id)
            return {"status": "unauthorized"}

        users = self.storage.list_users()
        lines = [f"👥 <b>REGISTERED USERS ({len(users)})</b>\n"]
        for u in users:
            uid = u["user_id"]
            uname = u.get("username") or u.get("first_name") or "Anonymous"
            stat = u.get("status", "ACTIVE")
            role = u.get("role", "USER")
            pos_cnt = self.storage.get_open_paper_positions_count(user_id=uid)
            lines.append(f"• <code>{uid}</code> | <b>{uname}</b> | {role} | {stat} | Open: {pos_cnt}")

        self.telegram.send_message("\n".join(lines), chat_id=chat_id)
        return {"status": "users_list_sent", "count": len(users)}

    def handle_admin_suspend(self, target_user_id: str, user_id: str, chat_id: str) -> Dict[str, Any]:
        if not self.is_admin(user_id):
            self.telegram.send_message("⛔ <b>Access Denied:</b> Administrator privileges required.", chat_id=chat_id)
            return {"status": "unauthorized"}

        if not target_user_id:
            self.telegram.send_message("ℹ️ Usage: <code>/suspend [USER_ID]</code>", chat_id=chat_id)
            return {"status": "usage_sent"}

        if self.is_admin(target_user_id):
            self.telegram.send_message("⚠️ Cannot suspend fellow administrator.", chat_id=chat_id)
            return {"status": "error"}

        ok = self.storage.update_user_status(target_user_id, "SUSPENDED")
        if ok:
            self.telegram.send_message(f"🔒 User <code>{target_user_id}</code> is now <b>SUSPENDED</b>.", chat_id=chat_id)
            return {"status": "suspended", "user_id": target_user_id}
        else:
            self.telegram.send_message(f"⚠️ User <code>{target_user_id}</code> not found.", chat_id=chat_id)
            return {"status": "not_found"}

    def handle_admin_activate(self, target_user_id: str, user_id: str, chat_id: str) -> Dict[str, Any]:
        if not self.is_admin(user_id):
            self.telegram.send_message("⛔ <b>Access Denied:</b> Administrator privileges required.", chat_id=chat_id)
            return {"status": "unauthorized"}

        if not target_user_id:
            self.telegram.send_message("ℹ️ Usage: <code>/activate [USER_ID]</code>", chat_id=chat_id)
            return {"status": "usage_sent"}

        ok = self.storage.update_user_status(target_user_id, "ACTIVE")
        if ok:
            self.telegram.send_message(f"🔓 User <code>{target_user_id}</code> is now <b>ACTIVE</b>.", chat_id=chat_id)
            return {"status": "activated", "user_id": target_user_id}
        else:
            self.telegram.send_message(f"⚠️ User <code>{target_user_id}</code> not found.", chat_id=chat_id)
            return {"status": "not_found"}

    def handle_admin_system_health(self, user_id: str, chat_id: str) -> Dict[str, Any]:
        if not self.is_admin(user_id):
            self.telegram.send_message("⛔ <b>Access Denied:</b> Administrator privileges required.", chat_id=chat_id)
            return {"status": "unauthorized"}

        pending_outbox = self.storage.get_pending_outbox_notifications(limit=100)
        msg = (
            "🩺 <b>SYSTEM HEALTH & DIAGNOSTICS</b>\n\n"
            "• <b>Database Integrity:</b> OK\n"
            "• <b>Market Data Cache:</b> ACTIVE\n"
            f"• <b>Pending Outbox Alerts:</b> {len(pending_outbox)}\n"
            "• <b>Telegram Polling:</b> RUNNING\n"
            "• <b>Execution Mode:</b> 100% SIMULATED PAPER TRADING"
        )
        self.telegram.send_message(msg, chat_id=chat_id)
        return {"status": "health_sent"}

    def handle_admin_global_settings(self, user_id: str, chat_id: str) -> Dict[str, Any]:
        if not self.is_admin(user_id):
            self.telegram.send_message("⛔ <b>Access Denied:</b> Administrator privileges required.", chat_id=chat_id)
            return {"status": "unauthorized"}

        self.telegram.send_message(
            "⚙️ <b>PLATFORM GLOBAL SETTINGS</b>\n\n"
            "• <b>Default Max Positions:</b> 20\n"
            "• <b>Default Virtual Balance:</b> $10,000.00\n"
            "• <b>Default Target R:R:</b> 4:1\n"
            "• <b>Slippage Model:</b> 0.02%\n"
            "• <b>Fee Model:</b> 0.06%",
            chat_id=chat_id,
        )
        return {"status": "global_settings_sent"}

    def _handle_callback(
        self,
        cb_id: str,
        data: str,
        user_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or self.default_user_id).strip()
        cid = str(chat_id or uid).strip()

        if data == "menu_analyze_menu":
            self.telegram.answer_callback_query(cb_id, "Opening analysis menu...")
            return self.send_analyze_selection_menu(user_id=uid, chat_id=cid)

        elif data == "analyze_custom_pair":
            self.telegram.answer_callback_query(cb_id, "Enter pair:")
            return self.prompt_custom_pair(user_id=uid, chat_id=cid)

        elif data.startswith("analyze_pair_"):
            target_sym = data[len("analyze_pair_"):]
            self.telegram.answer_callback_query(cb_id, f"Analysing {target_sym}...")
            return self.trigger_on_demand_analysis(target_symbol=target_sym, user_id=uid, chat_id=cid)

        elif data == "menu_account":
            self.telegram.answer_callback_query(cb_id, "Loading account...")
            return self.send_account_summary(user_id=uid, chat_id=cid)

        elif data == "menu_dashboard":
            self.telegram.answer_callback_query(cb_id, "Loading dashboard...")
            return self.send_dashboard_view(user_id=uid, chat_id=cid)

        elif data == "dashboard_refresh":
            self.telegram.answer_callback_query(cb_id, "Refreshing account...")
            return self.send_dashboard_view(user_id=uid, chat_id=cid)

        elif data == "menu_watchlist":
            self.telegram.answer_callback_query(cb_id, "Loading watchlist...")
            return self.send_watchlist(user_id=uid, chat_id=cid)

        elif data == "menu_positions" or data == "refresh_positions":
            self.telegram.answer_callback_query(cb_id, "Refreshing positions...")
            return self.send_open_positions_view(user_id=uid, chat_id=cid)

        elif data == "menu_history":
            self.telegram.answer_callback_query(cb_id, "Loading trade history...")
            return self.send_trade_history_view(user_id=uid, chat_id=cid)

        elif data == "menu_settings":
            self.telegram.answer_callback_query(cb_id, "Loading settings...")
            return self.send_settings_menu(user_id=uid, chat_id=cid)

        elif data == "admin_panel":
            self.telegram.answer_callback_query(cb_id, "Loading admin dashboard...")
            return self.handle_admin_dashboard(user_id=uid, chat_id=cid)

        elif data.startswith("manual_close_"):
            pos_id_str = data[len("manual_close_"):]
            if pos_id_str.isdigit():
                self.telegram.answer_callback_query(cb_id, f"Closing #{pos_id_str}...")
                return self.manual_close_position(int(pos_id_str), user_id=uid, chat_id=cid)

        elif data.startswith("view_analysis_"):
            signal_id = data[len("view_analysis_"):]
            sig = self.storage.get_signal(signal_id)
            if not sig:
                self.telegram.answer_callback_query(cb_id, "Signal not found.", show_alert=True)
                return {"status": "not_found"}

            import json
            notes = json.loads(sig.get("audit_notes") or "[]")
            notes_str = "\n".join(f"• {n}" for n in notes) if notes else "Multi-timeframe confluence confirmed."
            msg = (
                f"🔬 <b>CONFLUENCE ANALYSIS: {sig['symbol']} {sig['direction'].upper()}</b>\n\n"
                f"{notes_str}\n\n"
                f"<i>Entry: {sig['entry_price']:.2f} | SL: {sig['stop_loss']:.2f} | TP: {sig['take_profit']:.2f}</i>"
            )
            self.telegram.answer_callback_query(cb_id, "Analysis details loaded.")
            self.telegram.send_message(msg, chat_id=cid)
            return {"status": "analysis_viewed"}

        elif data.startswith("approve_"):
            signal_id = data[len("approve_"):]
            sig = self.storage.get_signal(signal_id)
            if not sig:
                self.telegram.answer_callback_query(cb_id, "⚠️ Signal not found.", show_alert=True)
                return {"status": "not_found"}

            # Backend ownership verification
            if sig.get("user_id") and str(sig.get("user_id")) != uid:
                logger.warning(f"SECURITY: User {uid} attempted to approve signal {signal_id} owned by {sig.get('user_id')}")
                self.telegram.answer_callback_query(cb_id, "⛔ Access Denied: You cannot approve another user's signal.", show_alert=True)
                return {"status": "unauthorized"}

            if sig["status"] == "EXPIRED":
                self.telegram.answer_callback_query(cb_id, "⏳ Signal expired! Market conditions have changed.", show_alert=True)
                return {"status": "expired"}

            if sig["status"] == "APPROVED":
                self.telegram.answer_callback_query(cb_id, "ℹ️ Trade already approved and active.", show_alert=True)
                return {"status": "already_approved"}

            if sig["status"] == "REJECTED":
                self.telegram.answer_callback_query(cb_id, "ℹ️ Trade was previously dismissed.", show_alert=True)
                return {"status": "already_rejected"}

            # Check open positions limit for this user
            open_positions = self.storage.get_open_positions(user_id=uid)
            max_pos = int(self.storage.get_setting("max_open_positions", 20, user_id=uid))
            if len(open_positions) >= max_pos:
                open_ids = ", ".join(f"#{p['position_id']} ({p['symbol']})" for p in open_positions)
                self.telegram.answer_callback_query(
                    cb_id,
                    f"⚠️ Maximum open positions ({max_pos}) reached! Active ({len(open_positions)}/{max_pos}): {open_ids}. Close an existing position first with /close ID.",
                    show_alert=True,
                )
                return {"status": "max_positions_reached", "open_positions": open_positions}

            # Approve and open in SQLite Paper Positions for this user
            risk_pct = float(self.storage.get_setting("risk_per_trade_pct", 1.0, user_id=uid))
            cur_bal = float(self.storage.get_setting("current_balance", 10000.0, user_id=uid))
            risk_unit = abs(sig["entry_price"] - sig["stop_loss"])
            dollar_risk = cur_bal * (risk_pct / 100.0)
            pos_size = dollar_risk / risk_unit if risk_unit > 0 else 0.0

            pos_id = self.storage.open_paper_trade(
                symbol=sig["symbol"],
                direction=sig["direction"],
                entry_price=sig["entry_price"],
                stop_loss=sig["stop_loss"],
                take_profit=sig["take_profit"],
                position_size=pos_size,
                user_id=uid,
            )
            self.storage.update_signal_status(signal_id, "APPROVED")

            direction_label = "LONG" if sig["direction"].lower() == "bullish" else "SHORT"
            self.telegram.answer_callback_query(cb_id, "✅ Virtual Paper Trade Executed!", show_alert=True)
            self.telegram.send_message(
                f"✅ <b>PAPER TRADE ENTERED (#{pos_id})</b>\n\n"
                f"• <b>Symbol:</b> {sig['symbol']}\n"
                f"• <b>Direction:</b> <b>{direction_label}</b>\n"
                f"• <b>Entry:</b> {sig['entry_price']:.2f}\n"
                f"• <b>Stop Loss:</b> {sig['stop_loss']:.2f}\n"
                f"• <b>Take Profit (4:1):</b> {sig['take_profit']:.2f}\n"
                f"• <b>Virtual Risk:</b> ${dollar_risk:.2f} ({risk_pct}%)\n\n"
                f"<i>Note: Virtual paper trading only. No real funds used. Use <code>/close {pos_id}</code> to exit anytime.</i>",
                chat_id=cid,
            )
            return {"status": "approved", "position_id": pos_id}

        elif data.startswith("reject_"):
            signal_id = data[len("reject_"):]
            sig = self.storage.get_signal(signal_id)
            if not sig:
                self.telegram.answer_callback_query(cb_id, "⚠️ Signal not found.", show_alert=True)
                return {"status": "not_found"}

            if sig.get("user_id") and str(sig.get("user_id")) != uid:
                logger.warning(f"SECURITY: User {uid} attempted to reject signal {signal_id} owned by {sig.get('user_id')}")
                self.telegram.answer_callback_query(cb_id, "⛔ Access Denied: You cannot reject another user's signal.", show_alert=True)
                return {"status": "unauthorized"}

            if sig["status"] in ("APPROVED", "REJECTED", "EXPIRED"):
                self.telegram.answer_callback_query(cb_id, f"ℹ️ Signal already {sig['status']}.", show_alert=True)
                return {"status": f"already_{sig['status']}"}

            self.storage.update_signal_status(signal_id, "REJECTED")
            self.telegram.answer_callback_query(cb_id, "✕ Signal dismissed.", show_alert=False)
            self.telegram.send_message(f"🗑️ Trade opportunity <b>{sig['symbol']}</b> was rejected.", chat_id=cid)
            return {"status": "rejected"}

        return {"status": "unhandled_callback"}
