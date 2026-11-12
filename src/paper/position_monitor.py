import time
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from .lifecycle_calculator import PositionLifecycleCalculator, PositionCalculations
from ..storage.database import StorageManager
from ..data.provider_factory import MarketProviderFactory
from ..data.models import Timeframe

logger = logging.getLogger(__name__)


class PositionMonitorService:
    """
    Continuous scheduled monitoring system for active paper positions.
    1. Fetches genuine live market data for each active symbol.
    2. Calculates mark price, unrealized P&L, R-multiple, target progress, and distances.
    3. Persists updated monitoring state in SQLite database so restarts preserve state.
    4. Detects Take-Profit and Stop-Loss hits, executing atomic position closure.
    5. Dispatches material progress milestone notifications to Telegram (debounced/rate-limited).
    6. Handles stale data gracefully with clear freshness indicators.
    """

    def __init__(
        self,
        storage: Optional[StorageManager] = None,
        telegram_service: Optional[Any] = None,
        progress_milestone_step: float = 25.0,  # Notify at 25%, 50%, 75%, 100%
        r_milestone_step: float = 1.0,         # Notify at +1R, +2R, +3R
        stale_threshold_seconds: float = 300.0,
    ):
        self.storage = storage or StorageManager()
        self.telegram = telegram_service
        self.progress_milestone_step = progress_milestone_step
        self.r_milestone_step = r_milestone_step
        self.stale_threshold_seconds = stale_threshold_seconds

    def monitor_all_open_positions(self) -> Dict[str, Any]:
        """
        Executes one full monitoring cycle across all users' OPEN positions.
        Returns a summary report of monitored, updated, and closed positions.
        """
        open_positions = self.storage.get_open_positions()
        if not open_positions:
            return {"status": "no_open_positions", "monitored": 0, "closed": 0}

        report = {
            "status": "success",
            "monitored": len(open_positions),
            "updated": 0,
            "closed": 0,
            "stale": 0,
            "errors": 0,
            "details": [],
        }

        # Group positions by symbol to minimize duplicate data fetching
        symbols = set(pos["symbol"] for pos in open_positions)
        market_data_cache: Dict[str, Dict[str, Any]] = {}

        for sym in symbols:
            market_data_cache[sym] = self._fetch_latest_price_and_candle(sym)

        for pos in open_positions:
            pos_id = pos["position_id"]
            user_id = pos["user_id"]
            sym = pos["symbol"]
            m_data = market_data_cache.get(sym, {})

            if not m_data.get("success"):
                report["errors"] += 1
                report["details"].append({
                    "position_id": pos_id,
                    "symbol": sym,
                    "status": "market_data_unavailable",
                    "error": m_data.get("error", "Unknown error"),
                })
                continue

            current_price = m_data["current_price"]
            candle_high = m_data.get("candle_high", current_price)
            candle_low = m_data.get("candle_low", current_price)
            price_time = m_data.get("timestamp")
            is_stale = m_data.get("is_stale", False)

            if is_stale:
                report["stale"] += 1

            # 1. Evaluate Exit Conditions (Take-Profit or Stop-Loss)
            exit_hit, exit_price, exit_reason = PositionLifecycleCalculator.evaluate_exit_conditions(
                direction=pos["direction"],
                stop_loss=pos["stop_loss"],
                take_profit=pos["take_profit"],
                candle_high=candle_high,
                candle_low=candle_low,
                current_price=current_price,
            )

            if exit_hit and exit_price is not None and exit_reason is not None:
                # Close atomically
                fee = pos["position_size"] * exit_price * 0.0006
                pnl = PositionLifecycleCalculator.calculate_pnl(
                    direction=pos["direction"],
                    entry_price=pos["entry_price"],
                    exit_or_current_price=exit_price,
                    position_size=pos["position_size"],
                    symbol=sym,
                    exit_fee=fee,
                )

                notification = {
                    "event_type": f"TRADE_CLOSED_{exit_reason.upper()}",
                    "symbol": sym,
                    "direction": pos["direction"].upper(),
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "pnl": pnl,
                    "entry_price": pos["entry_price"],
                    "message": (
                        f"{'🎯 <b>TAKE-PROFIT REACHED!</b>' if exit_reason == 'take_profit' else '🛑 <b>STOP-LOSS HIT</b>'}\n\n"
                        f"• <b>Position:</b> #{pos_id} {pos['direction'].upper()} {sym}\n"
                        f"• <b>Entry:</b> {pos['entry_price']:.4f}\n"
                        f"• <b>Exit:</b> {exit_price:.4f}\n"
                        f"• <b>Realised P&L:</b> ${pnl:+,.2f}\n"
                        f"• <b>Reason:</b> {exit_reason.upper()}\n"
                    ),
                }

                closed = self.storage.close_paper_trade_atomic(
                    position_id=pos_id,
                    user_id=user_id,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    pnl=pnl,
                    fee_paid=fee,
                    notification_event=notification,
                )
                if closed:
                    report["closed"] += 1
                    report["details"].append({
                        "position_id": pos_id,
                        "status": "closed",
                        "reason": exit_reason,
                        "exit_price": exit_price,
                        "pnl": pnl,
                    })
                continue

            # 2. Update Continuous Monitoring State
            metrics = PositionLifecycleCalculator.calculate_metrics(
                pos=pos,
                current_price=current_price,
                price_timestamp=price_time,
                is_stale=is_stale,
            )

            # Check for material milestone notifications
            last_p_mile = float(pos.get("last_notified_progress_milestone") or 0.0)
            last_r_mile = float(pos.get("last_notified_r_milestone") or 0.0)
            notify_progress = False
            notify_r = False
            new_p_mile = last_p_mile
            new_r_mile = last_r_mile

            # Progress milestone (e.g. crossing 25%, 50%, 75%)
            if metrics.target_progress_pct >= last_p_mile + self.progress_milestone_step:
                new_p_mile = (metrics.target_progress_pct // self.progress_milestone_step) * self.progress_milestone_step
                notify_progress = True

            # R-Multiple milestone (e.g. crossing +1.0R, +2.0R, +3.0R)
            if metrics.r_multiple >= last_r_mile + self.r_milestone_step and metrics.r_multiple >= 1.0:
                new_r_mile = int(metrics.r_multiple // self.r_milestone_step) * self.r_milestone_step
                notify_r = True

            # Persist latest monitored state in database
            self.storage.update_position_monitoring_state(
                position_id=pos_id,
                current_price=current_price,
                unrealized_pnl=metrics.unrealized_pnl,
                r_multiple=metrics.r_multiple,
                target_progress_pct=metrics.target_progress_pct,
                price_timestamp=price_time or datetime.now(timezone.utc).isoformat(),
                last_notified_progress_milestone=new_p_mile if (notify_progress or notify_r) else None,
                last_notified_r_milestone=new_r_mile if (notify_progress or notify_r) else None,
            )
            report["updated"] += 1

            # Dispatch material progress alert if milestone reached
            if (notify_progress or notify_r) and self.telegram:
                self._dispatch_milestone_alert(pos, metrics, new_p_mile, new_r_mile)

            report["details"].append({
                "position_id": pos_id,
                "symbol": sym,
                "current_price": current_price,
                "unrealized_pnl": metrics.unrealized_pnl,
                "r_multiple": metrics.r_multiple,
                "target_progress_pct": metrics.target_progress_pct,
            })

        return report

    def _fetch_latest_price_and_candle(self, symbol: str) -> Dict[str, Any]:
        """
        Fetches genuine market data from the appropriate verified provider.
        Never fabricates prices or mock data.
        """
        try:
            provider = MarketProviderFactory.get_provider_for_symbol(symbol)
            # Fetch 1m candles for fine resolution, or fallback to 5m
            df = provider.fetch_candles(symbol, timeframe=Timeframe.M1, limit=5)
            if df.empty:
                df = provider.fetch_candles(symbol, timeframe=Timeframe.M5, limit=5)
            if df.empty:
                return {"success": False, "error": f"No candles returned by provider for {symbol}"}

            last_candle = df.iloc[-1]
            c_price = float(last_candle["close"])
            c_high = float(df["high"].max())
            c_low = float(df["low"].min())
            c_ts = str(last_candle["timestamp"])

            # Check freshness
            is_stale = False
            try:
                if isinstance(last_candle["timestamp"], datetime):
                    candle_dt = last_candle["timestamp"]
                else:
                    candle_dt = datetime.fromisoformat(str(last_candle["timestamp"]).replace("Z", "+00:00"))
                if candle_dt.tzinfo is None:
                    candle_dt = candle_dt.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - candle_dt).total_seconds()
                if age > self.stale_threshold_seconds:
                    is_stale = True
            except Exception:
                pass

            return {
                "success": True,
                "current_price": c_price,
                "candle_high": c_high,
                "candle_low": c_low,
                "timestamp": c_ts,
                "is_stale": is_stale,
            }
        except Exception as e:
            logger.error(f"Error fetching live market data for {symbol}: {e}")
            return {"success": False, "error": str(e)}

    def _dispatch_milestone_alert(
        self,
        pos: Dict[str, Any],
        metrics: PositionCalculations,
        p_milestone: float,
        r_milestone: float,
    ):
        """Sends a structured Telegram alert for material position progress."""
        u = self.storage.get_user(pos["user_id"])
        chat_id = u["chat_id"] if u else pos["user_id"]
        dir_emoji = "🟢" if metrics.direction in ("bullish", "long") else "🔴"
        pnl_sign = "+" if metrics.unrealized_pnl >= 0 else ""

        msg = (
            f"📈 <b>POSITION PROGRESS UPDATE</b> {dir_emoji}\n\n"
            f"• <b>Position:</b> #{metrics.position_id} {metrics.direction.upper()} <b>{metrics.symbol}</b>\n"
            f"• <b>Entry:</b> {metrics.entry_price:.4f} → <b>Current:</b> {metrics.current_price:.4f}\n"
            f"• <b>Unrealised P&L:</b> ${metrics.unrealized_pnl:+,.2f}\n"
            f"• <b>R-Multiple:</b> {metrics.r_multiple:+.2f}R\n"
            f"• <b>Target Progress:</b> {metrics.target_progress_pct:+.1f}%\n"
            f"• <b>Distance to TP:</b> {abs(metrics.distance_to_tp):.4f}\n"
            f"• <b>Distance to SL:</b> {abs(metrics.distance_to_sl):.4f}\n"
            f"• <b>Duration Open:</b> {metrics.duration_str}\n"
        )
        if metrics.is_stale:
            msg += "\n⚠️ <i>Warning: Market price feed is currently delayed.</i>"

        try:
            self.telegram.send_message(msg, chat_id=chat_id)
        except Exception as e:
            logger.error(f"Failed to dispatch milestone alert for position {metrics.position_id}: {e}")
