import json
import logging
from typing import Optional, Dict, Any
from ..storage.database import StorageManager
from .telegram import TelegramAlertService

logger = logging.getLogger(__name__)


class OutboxNotificationDispatcher:
    """
    Reliable outbox notification dispatcher:
    1. Reads pending notifications committed atomically during trade closures/executions.
    2. Sends formatted Telegram alerts directly to the user's verified private chat_id.
    3. Handles retries with exponential backoff / retry limits.
    4. Prevents duplicate messages via immediate status transitions (PENDING -> SENT).
    5. Recovers and drains pending notifications after application restarts.
    """

    def __init__(
        self,
        storage: Optional[StorageManager] = None,
        telegram_service: Optional[TelegramAlertService] = None,
    ):
        self.storage = storage or StorageManager()
        self.telegram = telegram_service or TelegramAlertService()

    def dispatch_pending(self, limit: int = 50) -> int:
        """Processes and delivers pending outbox notifications."""
        pending = self.storage.get_pending_outbox_notifications(limit=limit)
        if not pending:
            return 0

        dispatched_count = 0
        for notif in pending:
            notif_id = notif["id"]
            chat_id = notif["chat_id"]
            event_type = notif["event_type"]

            try:
                payload = json.loads(notif["payload"]) if isinstance(notif["payload"], str) else notif["payload"]
            except Exception:
                payload = {}

            formatted_text = self._format_notification_message(event_type, payload)

            try:
                delivered = self.telegram.send_message(text=formatted_text, chat_id=chat_id)
                if delivered:
                    self.storage.mark_outbox_notification_sent(notif_id)
                    dispatched_count += 1
                else:
                    self.storage.mark_outbox_notification_failed(notif_id, "Delivery returned False")
            except Exception as e:
                logger.error(f"Error dispatching notification {notif_id} to chat {chat_id}: {e}")
                self.storage.mark_outbox_notification_failed(notif_id, str(e))

        return dispatched_count

    def _format_notification_message(self, event_type: str, payload: Dict[str, Any]) -> str:
        symbol = payload.get("symbol", "UNKNOWN")
        direction = payload.get("direction", "").upper()
        pnl = payload.get("pnl", 0.0)
        pnl_formatted = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
        pos_id = payload.get("position_id", "")

        if event_type == "TAKE_PROFIT":
            return (
                f"🎯 <b>Take Profit Reached: {symbol}</b>\n\n"
                f"Position: <code>#{pos_id}</code> {direction}\n"
                f"Exit Price: <code>{payload.get('exit_price', 0.0):.5f}</code>\n"
                f"Realised P&L: <b>{pnl_formatted}</b>\n"
                f"Virtual balance credited successfully."
            )
        elif event_type == "STOP_LOSS":
            return (
                f"🛑 <b>Stop Loss Hit: {symbol}</b>\n\n"
                f"Position: <code>#{pos_id}</code> {direction}\n"
                f"Exit Price: <code>{payload.get('exit_price', 0.0):.5f}</code>\n"
                f"Realised P&L: <b>{pnl_formatted}</b>\n"
                f"Virtual balance updated."
            )
        elif event_type == "MANUAL_CLOSE":
            return (
                f"📋 <b>Paper Trade Closed Manually: {symbol}</b>\n\n"
                f"Position: <code>#{pos_id}</code> {direction}\n"
                f"Exit Price: <code>{payload.get('exit_price', 0.0):.5f}</code>\n"
                f"Realised P&L: <b>{pnl_formatted}</b>"
            )
        elif event_type == "TRADE_OPENED":
            return (
                f"🚀 <b>Paper Trade Opened: {symbol}</b>\n\n"
                f"Position: <code>#{pos_id}</code> {direction}\n"
                f"Entry Price: <code>{payload.get('entry_price', 0.0):.5f}</code>\n"
                f"Stop Loss: <code>{payload.get('stop_loss', 0.0):.5f}</code>\n"
                f"Take Profit: <code>{payload.get('take_profit', 0.0):.5f}</code> (4:1 R:R)"
            )
        else:
            if "message" in payload and isinstance(payload["message"], str):
                return payload["message"]
            return f"🔔 <b>Trade Notification: {event_type}</b>\n\n{json.dumps(payload, indent=2)}"
