import os
import logging
import requests
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from ..strategy.mtf_models import MultiTimeframeSetup
from ..paper.engine import PaperPosition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    enabled: bool = False

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        """
        Loads configuration from environment variables.
        Defaults to enabled=False unless explicitly configured.
        Sanitizes whitespace, accidental quotes, or trailing newlines.
        """
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        enabled_env = os.getenv("TELEGRAM_ENABLED", "").strip().lower()

        if token:
            token = token.strip().strip("'\"").strip()
            # If user accidentally included a prefix like 'bot' in the token env var, remove it
            if token.lower().startswith("bot") and len(token) > 3 and token[3].isdigit():
                token = token[3:]

        if chat_id:
            chat_id = chat_id.strip().strip("'\"").strip()

        # If token and chat_id are present, enable unless explicitly set to false/0
        if token and chat_id:
            is_enabled = enabled_env not in ("false", "0", "no")
        else:
            is_enabled = enabled_env in ("true", "1", "yes")

        return cls(bot_token=token, chat_id=chat_id, enabled=is_enabled)


class TelegramAlertService:
    """
    Dispatches formatted trade alerts to Telegram.
    Gracefully runs in offline / mock mode when disabled or unconfigured.
    Never exposes or logs secret tokens.
    """

    def __init__(self, config: Optional[TelegramConfig] = None):
        self.config = config or TelegramConfig()

    def send_message(
        self,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        chat_id: Optional[str | int] = None,
    ) -> bool:
        target_chat = str(chat_id).strip() if chat_id is not None else self.config.chat_id
        if not self.config.enabled or not self.config.bot_token or not target_chat:
            logger.info(f"[TELEGRAM LOCAL LOG to {target_chat or 'default'}] {text}")
            return False

        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = requests.post(url, json=payload, timeout=5)
            if resp.status_code != 200:
                err_desc = "Unknown error"
                try:
                    err_json = resp.json()
                    err_desc = err_json.get("description", resp.text[:100])
                except Exception:
                    err_desc = resp.text[:100]
                logger.warning(f"Telegram API responded with HTTP status code {resp.status_code}: {err_desc}")
                return False
            return True
        except Exception as e:
            logger.warning(f"Failed to deliver Telegram alert: {type(e).__name__}")
            return False

    def answer_callback_query(self, callback_query_id: str, text: str, show_alert: bool = False) -> bool:
        """Acknowledges a button press callback to prevent loading spinners."""
        if not self.config.enabled or not self.config.bot_token:
            return False
        url = f"https://api.telegram.org/bot{self.config.bot_token}/answerCallbackQuery"
        payload = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        try:
            resp = requests.post(url, json=payload, timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def set_bot_commands(self) -> bool:
        """
        Registers slash commands with Telegram Bot API so typing '/' pops up
        command suggestions directly in the chat UI.
        """
        if not self.config.enabled or not self.config.bot_token:
            return False
        url = f"https://api.telegram.org/bot{self.config.bot_token}/setMyCommands"
        payload = {
            "commands": [
                {"command": "start", "description": "Open bot and main menu"},
                {"command": "menu", "description": "Main navigation menu"},
                {"command": "analyze", "description": "Select and analyze a market pair"},
                {"command": "dashboard", "description": "Account summary & web dashboard link"},
                {"command": "watchlist", "description": "View, add, or remove watchlist pairs"},
                {"command": "balance", "description": "Paper balance, equity, and P&L"},
                {"command": "positions", "description": "Monitor active open paper trades"},
                {"command": "history", "description": "Review completed paper trade history"},
                {"command": "performance", "description": "Performance statistics and win rate"},
                {"command": "settings", "description": "Manage risk %, R:R, and max positions"},
                {"command": "close", "description": "Manually close an open paper trade (/close ID)"},
                {"command": "help", "description": "Display command list and instructions"},
            ]
        }
        try:
            resp = requests.post(url, json=payload, timeout=5)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Failed to register Telegram slash commands: {type(e).__name__}")
            return False

    def format_opportunity_message(self, setup: MultiTimeframeSetup, symbol: str) -> str:
        """Formats one concise, unified trade opportunity message matching the requested format."""
        direction_str = "LONG" if setup.direction.value == "bullish" else "SHORT"
        risk_unit = abs(setup.entry_price - setup.stop_loss)
        tp_price = (setup.entry_price + (risk_unit * 4.0)) if direction_str == "LONG" else (setup.entry_price - (risk_unit * 4.0))
        rr_ratio = 4.0
        ts_str = setup.timestamp.strftime("%Y-%m-%d %H:%M UTC") if setup.timestamp else "Current"

        confluence_summary = "\n".join(f"• {note}" for note in setup.audit_trail) if setup.audit_trail else "• Top-Down 5-timeframe confluence confirmed."

        return (
            f"🎯 <b>NEW ICT/SMC TRADE OPPORTUNITY</b>\n\n"
            f"• <b>Instrument:</b> {symbol}\n"
            f"• <b>Direction:</b> <b>{direction_str}</b>\n"
            f"• <b>Entry Price:</b> {setup.entry_price:.2f}\n"
            f"• <b>Stop Loss:</b> {setup.stop_loss:.2f}\n"
            f"• <b>Take Profit (4:1 R:R):</b> {tp_price:.2f}\n"
            f"• <b>Risk-to-Reward Ratio:</b> 4.0:1\n"
            f"• <b>Setup Timeframe:</b> 15m (Confirmed 5m / Refined 1m)\n"
            f"• <b>Analysis Timestamp:</b> {ts_str}\n"
            f"• <b>Expiry / Invalidation:</b> Invalidation on break past {setup.stop_loss:.2f} or 30m timeout\n\n"
            f"<b>Confluence Hierarchy:</b>\n{confluence_summary}\n\n"
            f"<i>Select an option below to enter virtual paper trade, reject, or view analysis:</i>"
        )

    def alert_opportunity_with_approval(
        self,
        signal_id: str,
        setup: MultiTimeframeSetup,
        symbol: str = "BTCUSDT",
        chat_id: Optional[str | int] = None,
    ) -> bool:
        """
        Sends one final trade opportunity message with ENTER PAPER TRADE, REJECT, and VIEW ANALYSIS buttons.
        Requires explicit chat_id so analysis results route to the requesting user's Telegram chat.
        Logs an error and rejects delivery instead of silently falling back to the administrator's chat ID.
        """
        target_chat = str(chat_id).strip() if chat_id is not None else ""
        if not target_chat:
            logger.error(f"Cannot dispatch opportunity alert for {symbol} ({signal_id}): missing chat_id.")
            return False

        msg = self.format_opportunity_message(setup, symbol)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "ENTER PAPER TRADE", "callback_data": f"approve_{signal_id}"},
                    {"text": "REJECT", "callback_data": f"reject_{signal_id}"},
                ],
                [
                    {"text": "VIEW ANALYSIS", "callback_data": f"view_analysis_{signal_id}"},
                ]
            ]
        }
        return self.send_message(msg, reply_markup=keyboard, chat_id=target_chat)

    def send_test_alert(self) -> bool:
        """Sends a verification message to confirm connectivity."""
        msg = "🤖 <b>Trading bot Telegram connection test successful.</b>\nAll systems operational."
        return self.send_message(msg)

    @classmethod
    def discover_chat_id(cls, bot_token: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Safely retrieves recent message sender info from Telegram's official getUpdates endpoint.
        Never logs or exposes the bot token.
        """
        token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            logger.warning("Cannot discover chat ID: TELEGRAM_BOT_TOKEN is not set.")
            return []

        url = f"https://api.telegram.org/bot{token}/getUpdates"
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                logger.warning(f"Telegram getUpdates returned HTTP {resp.status_code}")
                return []
            data = resp.json()
            results = []
            for item in data.get("result", []):
                msg = item.get("message") or item.get("channel_post") or {}
                chat = msg.get("chat") or {}
                if chat and "id" in chat:
                    results.append({
                        "chat_id": chat["id"],
                        "type": chat.get("type", "unknown"),
                        "username": chat.get("username", "N/A"),
                        "first_name": chat.get("first_name", "N/A"),
                    })
            return results
        except Exception as e:
            logger.warning(f"Error checking Telegram updates: {type(e).__name__}")
            return []

    def alert_mtf_setup(
        self,
        setup: MultiTimeframeSetup,
        symbol: str = "BTCUSDT",
        chat_id: Optional[str | int] = None,
    ) -> bool:
        """
        Sends an MTF setup alert. Supports explicit chat_id routing,
        falling back to default config.chat_id only for system/scanner broadcasts.
        """
        target_chat = str(chat_id).strip() if chat_id is not None else self.config.chat_id
        if not target_chat:
            logger.error(f"Cannot dispatch MTF setup alert for {symbol}: missing destination chat_id.")
            return False

        msg = (
            f"🎯 <b>ICT/SMC SETUP DETECTED</b>\n\n"
            f"<b>Symbol:</b> {symbol}\n"
            f"<b>Direction:</b> {setup.direction.value.upper()}\n"
            f"<b>Entry Price:</b> {setup.entry_price:.2f}\n"
            f"<b>Stop Loss:</b> {setup.stop_loss:.2f}\n\n"
            f"<b>Confluence Hierarchy:</b>\n"
            + "\n".join(f"• {note}" for note in setup.audit_trail)
        )
        return self.send_message(msg, chat_id=target_chat)

    def alert_paper_trade_exit(
        self,
        pos: PaperPosition,
        chat_id: Optional[str | int] = None,
    ) -> bool:
        """
        Sends a virtual paper trade closure alert.
        Routes to the provided chat_id or position owner's user_id,
        logging an error if no recipient chat_id is available.
        """
        target_chat = str(chat_id).strip() if chat_id is not None else str(getattr(pos, "user_id", "") or self.config.chat_id or "").strip()
        if not target_chat:
            pos_id = getattr(pos, "position_id", "N/A")
            logger.error(f"Cannot dispatch paper trade exit alert for #{pos_id}: missing destination chat_id.")
            return False

        emoji = "✅" if pos.pnl > 0 else "❌"
        msg = (
            f"{emoji} <b>VIRTUAL TRADE CLOSED</b>\n\n"
            f"<b>Symbol:</b> {pos.symbol}\n"
            f"<b>Direction:</b> {pos.direction.value.upper()}\n"
            f"<b>Exit Price:</b> {pos.exit_price:.2f}\n"
            f"<b>Reason:</b> {pos.exit_reason.value.upper()}\n"
            f"<b>Net PnL:</b> ${pos.pnl:+.2f} ({pos.pnl_pct:+.2f}%)\n"
            f"<b>Fees:</b> ${pos.fee_paid:.2f}"
        )
        return self.send_message(msg, chat_id=target_chat)
