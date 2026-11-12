from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
import pandas as pd
from ..strategy.models import Direction
from ..data.models import Timeframe


class TradeStatus(str, Enum):
    OPEN = "open"
    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"
    EXPIRED = "expired"


class ExitReason(str, Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIMEOUT = "timeout"
    MANUAL = "manual"


@dataclass(frozen=True)
class BacktestConfig:
    """Configurable backtest simulation parameters."""
    initial_capital: float = 10000.0
    risk_per_trade_pct: float = 1.0  # Risk % of equity per trade
    target_risk_reward: float = 4.0  # Configured 4:1 R:R target
    spread_pips: float = 0.0         # Spread in pips/ticks
    fee_pct: float = 0.0006          # 0.06% exchange fee per leg
    slippage_pct: float = 0.0002     # 0.02% slippage estimate
    max_holding_bars: int = 150      # Bar timeout


@dataclass
class TradeRecord:
    """Represents a simulated trade executed during historical backtesting."""
    trade_id: int
    symbol: str
    direction: Direction
    entry_time: pd.Timestamp
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    risk_amount: float
    setup_timeframe: str = "15m"
    trading_session: str = "NY"
    day_of_week: str = "Monday"
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    fee_paid: float = 0.0
    r_multiple: float = 0.0
    pnl_pct: float = 0.0
    status: TradeStatus = TradeStatus.OPEN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "entry_time": str(self.entry_time),
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "position_size": self.position_size,
            "risk_amount": self.risk_amount,
            "setup_timeframe": self.setup_timeframe,
            "trading_session": self.trading_session,
            "day_of_week": self.day_of_week,
            "exit_time": str(self.exit_time),
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "gross_pnl": round(self.gross_pnl, 2),
            "net_pnl": round(self.net_pnl, 2),
            "fee_paid": round(self.fee_paid, 2),
            "r_multiple": round(self.r_multiple, 2),
            "pnl_pct": round(self.pnl_pct, 2),
            "status": self.status.value,
        }


@dataclass
class BacktestMetrics:
    """Comprehensive performance metrics report for historical backtest evaluation."""
    initial_capital: float
    final_equity: float
    total_net_pnl: float
    gross_profit: float
    gross_loss: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate_pct: float
    average_win: float
    average_loss: float
    average_r_multiple: float
    expected_value_per_trade: float
    profit_factor: float
    max_drawdown_amount: float
    max_drawdown_pct: float
    return_on_capital_pct: float
    total_fees_paid: float
    long_trades_count: int
    long_win_rate_pct: float
    long_net_pnl: float
    short_trades_count: int
    short_win_rate_pct: float
    short_net_pnl: float
    consecutive_wins_max: int
    consecutive_losses_max: int
    performance_by_day: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    dataset_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "initial_capital": self.initial_capital,
            "final_equity": self.final_equity,
            "total_net_pnl": self.total_net_pnl,
            "gross_profit": self.gross_profit,
            "gross_loss": self.gross_loss,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "breakeven_trades": self.breakeven_trades,
            "win_rate_pct": self.win_rate_pct,
            "average_win": self.average_win,
            "average_loss": self.average_loss,
            "average_r_multiple": self.average_r_multiple,
            "expected_value_per_trade": self.expected_value_per_trade,
            "profit_factor": self.profit_factor,
            "max_drawdown_amount": self.max_drawdown_amount,
            "max_drawdown_pct": self.max_drawdown_pct,
            "return_on_capital_pct": self.return_on_capital_pct,
            "total_fees_paid": self.total_fees_paid,
            "long_performance": {
                "trades": self.long_trades_count,
                "win_rate": self.long_win_rate_pct,
                "net_pnl": self.long_net_pnl,
            },
            "short_performance": {
                "trades": self.short_trades_count,
                "win_rate": self.short_win_rate_pct,
                "net_pnl": self.short_net_pnl,
            },
            "consecutive_wins_max": self.consecutive_wins_max,
            "consecutive_losses_max": self.consecutive_losses_max,
            "performance_by_day": self.performance_by_day,
            "equity_curve": self.equity_curve,
            "dataset_metadata": self.dataset_metadata,
        }
