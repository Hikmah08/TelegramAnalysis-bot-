from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
import pandas as pd
from ..strategy.models import Direction
from ..strategy.mtf_models import MultiTimeframeSetup


class PaperOrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class PaperExitReason(str, Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIMEOUT = "timeout"
    MANUAL = "manual"


@dataclass
class PaperPosition:
    """Represents a virtual simulated position."""
    position_id: int
    symbol: str
    direction: Direction
    entry_time: pd.Timestamp
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float
    allocated_margin: float
    user_id: str = "OWNER_001"
    status: PaperOrderStatus = PaperOrderStatus.FILLED
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[PaperExitReason] = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fee_paid: float = 0.0


@dataclass
class PaperAccount:
    """Virtual simulated account with zero connection to real brokerages."""
    initial_balance: float = 10000.0
    current_balance: float = 10000.0
    reserved_margin: float = 0.0
    risk_per_trade_pct: float = 1.0
    target_risk_reward: float = 4.0  # 4:1 target
    fee_pct: float = 0.0006
    slippage_pct: float = 0.0002
    max_open_positions: int = 20
    open_positions: Dict[int, PaperPosition] = field(default_factory=dict)
    closed_positions: List[PaperPosition] = field(default_factory=list)
    next_position_id: int = 1

    @property
    def free_balance(self) -> float:
        return self.current_balance - self.reserved_margin

    @property
    def total_net_pnl(self) -> float:
        return self.current_balance - self.initial_balance


class PaperTradingEngine:
    """
    Virtual Paper Trading engine that operates on live or replay market feeds.
    Strictly isolated: No real broker connections, no API keys, no order routing.
    """

    def __init__(self, account: Optional[PaperAccount] = None):
        self.account = account or PaperAccount()

    def process_setup_candidate(
        self,
        setup: MultiTimeframeSetup,
        symbol: str = "BTCUSDT",
        user_id: str = "OWNER_001",
    ) -> Optional[PaperPosition]:
        """
        Attempts to open a virtual position based on a qualified MTF setup.
        """
        # Enforce maximum concurrent virtual positions
        if len(self.account.open_positions) >= self.account.max_open_positions:
            return None

        entry = setup.entry_price
        sl = setup.stop_loss
        risk_per_unit = abs(entry - sl)
        if risk_per_unit <= 0:
            return None

        # Position sizing: risk exactly risk_per_trade_pct of current balance
        dollar_risk = self.account.current_balance * (self.account.risk_per_trade_pct / 100.0)
        pos_size = dollar_risk / risk_per_unit
        notional = pos_size * entry

        # Apply slippage to virtual entry
        if setup.direction == Direction.BULLISH:
            slipped_entry = entry * (1.0 + self.account.slippage_pct)
            tp = slipped_entry + (risk_per_unit * self.account.target_risk_reward)
        else:
            slipped_entry = entry * (1.0 - self.account.slippage_pct)
            tp = slipped_entry - (risk_per_unit * self.account.target_risk_reward)

        fee = notional * self.account.fee_pct
        self.account.current_balance -= fee  # Deduct entry fee

        pos = PaperPosition(
            position_id=self.account.next_position_id,
            user_id=str(user_id).strip(),
            symbol=symbol,
            direction=setup.direction,
            entry_time=setup.timestamp or pd.Timestamp.now(tz="UTC"),
            entry_price=slipped_entry,
            stop_loss=sl,
            take_profit=tp,
            position_size=pos_size,
            allocated_margin=dollar_risk,
            fee_paid=fee,
        )
        self.account.next_position_id += 1
        self.account.open_positions[pos.position_id] = pos
        return pos

    open_position = process_setup_candidate

    def restore_from_storage(self, storage: Any, user_id: Optional[str | int] = None):
        """Restores open and closed paper positions from the SQLite database."""
        open_db_pos = storage.get_open_positions(user_id=user_id)
        for p in open_db_pos:
            pos_id = p["position_id"]
            if pos_id not in self.account.open_positions:
                pos = PaperPosition(
                    position_id=pos_id,
                    user_id=p.get("user_id", "OWNER_001"),
                    symbol=p["symbol"],
                    direction=Direction(p["direction"].lower()),
                    entry_time=pd.Timestamp(p["entry_time"]),
                    entry_price=p["entry_price"],
                    stop_loss=p["stop_loss"],
                    take_profit=p["take_profit"],
                    position_size=p["position_size"],
                    allocated_margin=abs(p["entry_price"] - p["stop_loss"]) * p["position_size"],
                    fee_paid=p.get("fee_paid", 0.0),
                    status=PaperOrderStatus.FILLED,
                )
                self.account.open_positions[pos_id] = pos
                if pos_id >= self.account.next_position_id:
                    self.account.next_position_id = pos_id + 1

    def update_with_candle(self, candle: pd.Series, storage: Optional[Any] = None) -> List[PaperPosition]:
        """
        Evaluates active virtual positions against newly closed candles.
        Applies conservative resolution for intra-bar ambiguity (if both SL and TP touched,
        assumes Stop Loss hit first unless proven otherwise).
        Persists closed positions to SQLite storage if provided.
        """
        c_high = float(candle["high"])
        c_low = float(candle["low"])
        c_close = float(candle["close"])
        c_time = pd.Timestamp(candle["timestamp"])

        closed_now: List[PaperPosition] = []

        for pos_id, pos in list(self.account.open_positions.items()):
            exit_hit = False
            exit_price = None
            reason = None

            if pos.direction == Direction.BULLISH:
                sl_hit = c_low <= pos.stop_loss
                tp_hit = c_high >= pos.take_profit

                # Conservative intra-bar ambiguity: If both SL and TP are touched in the same candle,
                # conservatively assume Stop Loss occurred first.
                if sl_hit and tp_hit:
                    exit_hit = True
                    exit_price = pos.stop_loss * (1.0 - self.account.slippage_pct)
                    reason = PaperExitReason.STOP_LOSS
                elif sl_hit:
                    exit_hit = True
                    exit_price = pos.stop_loss * (1.0 - self.account.slippage_pct)
                    reason = PaperExitReason.STOP_LOSS
                elif tp_hit:
                    exit_hit = True
                    exit_price = pos.take_profit * (1.0 - self.account.slippage_pct)
                    reason = PaperExitReason.TAKE_PROFIT

            elif pos.direction == Direction.BEARISH:
                sl_hit = c_high >= pos.stop_loss
                tp_hit = c_low <= pos.take_profit

                if sl_hit and tp_hit:
                    exit_hit = True
                    exit_price = pos.stop_loss * (1.0 + self.account.slippage_pct)
                    reason = PaperExitReason.STOP_LOSS
                elif sl_hit:
                    exit_hit = True
                    exit_price = pos.stop_loss * (1.0 + self.account.slippage_pct)
                    reason = PaperExitReason.STOP_LOSS
                elif tp_hit:
                    exit_hit = True
                    exit_price = pos.take_profit * (1.0 + self.account.slippage_pct)
                    reason = PaperExitReason.TAKE_PROFIT

            if exit_hit and exit_price is not None and reason is not None:
                notional_exit = pos.position_size * exit_price
                exit_fee = notional_exit * self.account.fee_pct

                if pos.direction == Direction.BULLISH:
                    gross_pnl = (exit_price - pos.entry_price) * pos.position_size
                else:
                    gross_pnl = (pos.entry_price - exit_price) * pos.position_size

                net_pnl = gross_pnl - exit_fee
                pos.exit_time = c_time
                pos.exit_price = exit_price
                pos.exit_reason = reason
                pos.status = PaperOrderStatus.CLOSED
                pos.pnl = net_pnl
                pos.fee_paid += exit_fee
                pos.pnl_pct = (net_pnl / pos.allocated_margin) * 100.0 if pos.allocated_margin > 0 else 0.0

                # Update virtual balance in memory
                self.account.current_balance += net_pnl
                self.account.closed_positions.append(pos)
                del self.account.open_positions[pos_id]
                closed_now.append(pos)

                # Persist to database if storage is provided
                if storage is not None:
                    if hasattr(storage, "close_paper_trade_atomic"):
                        storage.close_paper_trade_atomic(
                            position_id=pos_id,
                            user_id=pos.user_id,
                            exit_price=exit_price,
                            exit_reason=reason.value,
                            pnl=net_pnl,
                            fee_paid=exit_fee,
                            notification_event={
                                "event_type": f"trade_closed_{reason.value}",
                                "symbol": pos.symbol,
                                "pnl": net_pnl,
                                "exit_price": exit_price,
                                "message": f"🔔 *Trade Closed ({reason.value.upper()})*\nPair: `{pos.symbol}`\nExit: `{exit_price:.4f}`\nPnL: `{net_pnl:+.2f} USD`"
                            }
                        )
                    else:
                        storage.close_paper_trade(
                            position_id=pos_id,
                            exit_price=exit_price,
                            exit_reason=reason.value,
                            pnl=net_pnl,
                            fee_paid=exit_fee,
                        )

        return closed_now

    def close_position_manually(
        self,
        position_id: int,
        current_price: float,
        storage: Optional[Any] = None,
        user_id: Optional[str | int] = None,
    ) -> Optional[PaperPosition]:
        """Manually closes an active virtual paper position at the given market price."""
        if position_id not in self.account.open_positions:
            return None

        pos = self.account.open_positions[position_id]
        if user_id is not None and str(pos.user_id) != str(user_id).strip():
            return None

        notional_exit = pos.position_size * current_price
        exit_fee = notional_exit * self.account.fee_pct

        if pos.direction == Direction.BULLISH:
            gross_pnl = (current_price - pos.entry_price) * pos.position_size
        else:
            gross_pnl = (pos.entry_price - current_price) * pos.position_size

        net_pnl = gross_pnl - exit_fee
        pos.exit_time = pd.Timestamp.now(tz="UTC")
        pos.exit_price = current_price
        pos.exit_reason = PaperExitReason.MANUAL
        pos.status = PaperOrderStatus.CLOSED
        pos.pnl = net_pnl
        pos.fee_paid += exit_fee
        pos.pnl_pct = (net_pnl / pos.allocated_margin) * 100.0 if pos.allocated_margin > 0 else 0.0

        self.account.current_balance += net_pnl
        self.account.closed_positions.append(pos)
        del self.account.open_positions[position_id]

        if storage is not None:
            if hasattr(storage, "close_paper_trade_atomic"):
                storage.close_paper_trade_atomic(
                    position_id=position_id,
                    user_id=pos.user_id,
                    exit_price=current_price,
                    exit_reason="manual",
                    pnl=net_pnl,
                    fee_paid=exit_fee,
                    notification_event={
                        "event_type": "trade_closed_manual",
                        "symbol": pos.symbol,
                        "pnl": net_pnl,
                        "exit_price": current_price,
                        "message": f"🔔 *Manual Closure*\nPair: `{pos.symbol}`\nExit: `{current_price:.4f}`\nPnL: `{net_pnl:+.2f} USD`"
                    }
                )
            else:
                storage.close_paper_trade(
                    position_id=position_id,
                    exit_price=current_price,
                    exit_reason="manual",
                    pnl=net_pnl,
                    fee_paid=exit_fee,
                )

        return pos

