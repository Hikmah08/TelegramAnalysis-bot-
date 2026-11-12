import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass

from ..data.instruments import get_instrument_spec, AssetClass

logger = logging.getLogger(__name__)


@dataclass
class PositionCalculations:
    """Standardized trading metrics for an open or closed paper position."""
    position_id: int
    user_id: str
    symbol: str
    direction: str
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    initial_risk: float
    unrealized_pnl: float
    r_multiple: float
    target_progress_pct: float
    distance_from_entry: float
    distance_to_sl: float
    distance_to_tp: float
    duration_str: str
    is_stale: bool = False
    price_timestamp: Optional[str] = None
    fetch_timestamp: Optional[str] = None


class PositionLifecycleCalculator:
    """
    Computes exact, verified P&L, R-multiple, progress toward target,
    and distances for Crypto, Forex, and Metals paper positions.
    Never invents market prices or fabricates data.
    """

    @staticmethod
    def calculate_pnl(
        direction: str,
        entry_price: float,
        exit_or_current_price: float,
        position_size: float,
        symbol: str = "BTCUSDT",
        exit_fee: float = 0.0,
    ) -> float:
        """
        Exact P&L formula:
        For BULLISH / LONG: (Exit - Entry) * Size - Fee
        For BEARISH / SHORT: (Entry - Exit) * Size - Fee
        """
        dir_clean = direction.strip().lower()
        if dir_clean in ("bullish", "long", "buy"):
            gross = (exit_or_current_price - entry_price) * position_size
        else:
            gross = (entry_price - exit_or_current_price) * position_size
        return round(gross - exit_fee, 4)

    @staticmethod
    def calculate_initial_risk(
        direction: str,
        entry_price: float,
        stop_loss: float,
        position_size: float,
    ) -> float:
        """Initial risk in account currency (USD/USDT)."""
        risk_per_unit = abs(entry_price - stop_loss)
        return round(risk_per_unit * position_size, 4)

    @staticmethod
    def calculate_metrics(
        pos: Dict[str, Any],
        current_price: float,
        price_timestamp: Optional[str] = None,
        is_stale: bool = False,
    ) -> PositionCalculations:
        """
        Calculates all required live position attributes according to strict specifications:
        - Target progress: price movement relative to entry-to-target distance.
        - R-multiple: unrealized PnL / initial risk.
        - Distance from entry, SL, TP.
        - Open duration.
        """
        direction = str(pos.get("direction", "bullish")).lower()
        entry = float(pos["entry_price"])
        sl = float(pos["stop_loss"])
        tp = float(pos["take_profit"])
        size = float(pos["position_size"])
        pos_id = int(pos.get("position_id", 0))
        user_id = str(pos.get("user_id", "OWNER_001"))
        symbol = str(pos.get("symbol", "BTCUSDT")).upper()

        initial_risk = float(pos.get("initial_risk") or 0.0)
        if initial_risk <= 0.0:
            initial_risk = PositionLifecycleCalculator.calculate_initial_risk(direction, entry, sl, size)

        # Unrealized PnL
        unrealized_pnl = PositionLifecycleCalculator.calculate_pnl(
            direction=direction,
            entry_price=entry,
            exit_or_current_price=current_price,
            position_size=size,
            symbol=symbol,
            exit_fee=0.0,
        )

        # R-Multiple: unrealized P&L / initial risk
        if initial_risk > 0:
            r_multiple = round(unrealized_pnl / initial_risk, 2)
        else:
            r_multiple = 0.0

        # Distance calculations & Target Progress
        # LONG: Target is above entry (tp > entry), SL is below (sl < entry)
        # SHORT: Target is below entry (tp < entry), SL is above (sl > entry)
        if direction in ("bullish", "long", "buy"):
            distance_from_entry = current_price - entry
            distance_to_sl = current_price - sl
            distance_to_tp = tp - current_price
            target_distance = tp - entry
            curr_move = current_price - entry
        else:
            distance_from_entry = entry - current_price
            distance_to_sl = sl - current_price
            distance_to_tp = current_price - tp
            target_distance = entry - tp
            curr_move = entry - current_price

        # Progress calculation:
        # Accounts for adverse price movement (can be negative).
        # Clamped or reported cleanly if target distance is 0.
        if target_distance > 0:
            target_progress_pct = round((curr_move / target_distance) * 100.0, 1)
        else:
            target_progress_pct = 0.0

        # Duration
        entry_time_str = pos.get("entry_time")
        duration_str = "0m"
        if entry_time_str:
            try:
                # Parse ISO timestamp
                if entry_time_str.endswith("Z"):
                    entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                else:
                    entry_dt = datetime.fromisoformat(entry_time_str)
                now_utc = datetime.now(timezone.utc)
                diff = now_utc - entry_dt
                hours, remainder = divmod(int(diff.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                if hours > 24:
                    days = hours // 24
                    duration_str = f"{days}d {hours % 24}h"
                elif hours > 0:
                    duration_str = f"{hours}h {minutes}m"
                else:
                    duration_str = f"{max(0, minutes)}m"
            except Exception:
                duration_str = "unknown"

        now_iso = datetime.now(timezone.utc).isoformat()
        return PositionCalculations(
            position_id=pos_id,
            user_id=user_id,
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            current_price=current_price,
            stop_loss=sl,
            take_profit=tp,
            position_size=size,
            initial_risk=initial_risk,
            unrealized_pnl=unrealized_pnl,
            r_multiple=r_multiple,
            target_progress_pct=target_progress_pct,
            distance_from_entry=round(distance_from_entry, 5),
            distance_to_sl=round(distance_to_sl, 5),
            distance_to_tp=round(distance_to_tp, 5),
            duration_str=duration_str,
            is_stale=is_stale,
            price_timestamp=price_timestamp or now_iso,
            fetch_timestamp=now_iso,
        )

    @staticmethod
    def evaluate_exit_conditions(
        direction: str,
        stop_loss: float,
        take_profit: float,
        candle_high: float,
        candle_low: float,
        current_price: float,
        slippage_pct: float = 0.0002,
    ) -> Tuple[bool, Optional[float], Optional[str]]:
        """
        Evaluates whether a monitored position has hit Stop-Loss or Take-Profit.
        Conservative resolution rule:
        If both SL and TP are touched in the same period/candle,
        assumes Stop-Loss occurred first.
        Models slippage conservatively.
        """
        dir_clean = direction.strip().lower()
        if dir_clean in ("bullish", "long", "buy"):
            sl_hit = candle_low <= stop_loss
            tp_hit = candle_high >= take_profit

            if sl_hit and tp_hit:
                # Conservative intra-candle rule
                exit_price = min(candle_low, stop_loss * (1.0 - slippage_pct))
                return True, round(exit_price, 6), "stop_loss"
            elif sl_hit:
                exit_price = min(current_price, stop_loss * (1.0 - slippage_pct))
                return True, round(exit_price, 6), "stop_loss"
            elif tp_hit:
                exit_price = max(current_price, take_profit * (1.0 - slippage_pct))
                return True, round(exit_price, 6), "take_profit"

        else:  # Bearish / Short
            sl_hit = candle_high >= stop_loss
            tp_hit = candle_low <= take_profit

            if sl_hit and tp_hit:
                exit_price = max(candle_high, stop_loss * (1.0 + slippage_pct))
                return True, round(exit_price, 6), "stop_loss"
            elif sl_hit:
                exit_price = max(current_price, stop_loss * (1.0 + slippage_pct))
                return True, round(exit_price, 6), "stop_loss"
            elif tp_hit:
                exit_price = min(current_price, take_profit * (1.0 + slippage_pct))
                return True, round(exit_price, 6), "take_profit"

        return False, None, None
