from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from ..storage.database import StorageManager
from ..data.instruments import get_instrument_spec


@dataclass
class AccountMetrics:
    starting_balance: float
    current_balance: float
    unrealized_pnl: float
    equity: float
    realized_pnl: float
    total_pnl: float
    total_pnl_pct: float
    open_positions_count: int
    closed_trades_count: int
    winning_trades_count: int
    losing_trades_count: int
    win_rate_pct: float
    max_drawdown_pct: float
    open_positions: List[Dict[str, Any]]


class AccountService:
    """
    Unified Account and Performance metrics service.
    Acts as the single source of truth for both Web Dashboard and Telegram views.
    Guarantees consistent, non-fabricated calculations directly from SQLite.
    """

    def __init__(self, storage: Optional[StorageManager] = None):
        self.storage = storage or StorageManager()

    def get_metrics(
        self,
        current_prices: Optional[Dict[str, float]] = None,
        user_id: Optional[str | int] = None,
    ) -> AccountMetrics:
        """
        Calculates starting balance, realized P&L, unrealized P&L, account equity,
        win rate, and peak-to-trough maximum drawdown from persistent paper records
        strictly scoped to the authenticated user.
        """
        init_bal = float(self.storage.get_setting("initial_balance", 10000.0, user_id=user_id))
        cur_bal = float(self.storage.get_setting("current_balance", 10000.0, user_id=user_id))
        open_positions = self.storage.get_open_positions(user_id=user_id)
        closed_positions = self.storage.get_closed_positions(user_id=user_id)

        # 1. Realized PnL from closed positions
        realized_pnl = sum(p.get("pnl", 0.0) for p in closed_positions)

        # 2. Unrealized PnL from open positions based on mark prices
        unrealized_pnl = 0.0
        enriched_open_positions = []
        prices = current_prices or {}

        from .lifecycle_calculator import PositionLifecycleCalculator

        for pos in open_positions:
            sym = pos["symbol"]
            mark_price = prices.get(sym, pos.get("current_price") or pos["entry_price"])
            calc = PositionLifecycleCalculator.calculate_metrics(pos=pos, current_price=mark_price)
            unrealized_pnl += calc.unrealized_pnl

            pos_copy = dict(pos)
            pos_copy["current_price"] = calc.current_price
            pos_copy["unrealized_pnl"] = calc.unrealized_pnl
            pos_copy["r_multiple"] = calc.r_multiple
            pos_copy["target_progress_pct"] = calc.target_progress_pct
            pos_copy["progress_pct"] = calc.target_progress_pct
            pos_copy["initial_risk"] = calc.initial_risk
            pos_copy["distance_from_entry"] = calc.distance_from_entry
            pos_copy["distance_to_sl"] = calc.distance_to_sl
            pos_copy["distance_to_tp"] = calc.distance_to_tp
            pos_copy["duration_str"] = calc.duration_str
            enriched_open_positions.append(pos_copy)

        # 3. Total Equity & Total PnL
        equity = cur_bal + unrealized_pnl
        total_pnl = equity - init_bal
        total_pnl_pct = (total_pnl / init_bal * 100.0) if init_bal > 0 else 0.0

        # 4. Win Rate
        winning_trades = sum(1 for p in closed_positions if p.get("pnl", 0.0) > 0)
        losing_trades = sum(1 for p in closed_positions if p.get("pnl", 0.0) < 0)
        total_closed = len(closed_positions)
        win_rate = (winning_trades / total_closed * 100.0) if total_closed > 0 else 0.0

        # 5. Maximum Drawdown calculation over closed trade equity curve
        running_equity = init_bal
        peak_equity = init_bal
        max_dd_pct = 0.0

        # Sort closed positions chronologically by exit time
        sorted_closed = sorted(
            closed_positions,
            key=lambda x: x.get("exit_time") or x.get("entry_time") or ""
        )

        for p in sorted_closed:
            running_equity += p.get("pnl", 0.0)
            if running_equity > peak_equity:
                peak_equity = running_equity
            if peak_equity > 0:
                dd = (peak_equity - running_equity) / peak_equity * 100.0
                if dd > max_dd_pct:
                    max_dd_pct = dd

        # Also test current equity against peak
        if equity > peak_equity:
            peak_equity = equity
        if peak_equity > 0:
            current_dd = (peak_equity - equity) / peak_equity * 100.0
            if current_dd > max_dd_pct:
                max_dd_pct = current_dd

        return AccountMetrics(
            starting_balance=round(init_bal, 2),
            current_balance=round(cur_bal, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            equity=round(equity, 2),
            realized_pnl=round(realized_pnl, 2),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl_pct, 2),
            open_positions_count=len(open_positions),
            closed_trades_count=total_closed,
            winning_trades_count=winning_trades,
            losing_trades_count=losing_trades,
            win_rate_pct=round(win_rate, 1),
            max_drawdown_pct=round(max_dd_pct, 2),
            open_positions=enriched_open_positions,
        )
