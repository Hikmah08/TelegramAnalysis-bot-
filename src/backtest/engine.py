import os
from typing import List, Optional, Dict, Any, Tuple
import pandas as pd
from ..strategy.models import Direction
from ..strategy.sequence_tracker import SequenceTracker
from .models import (
    BacktestConfig,
    TradeRecord,
    TradeStatus,
    ExitReason,
    BacktestMetrics,
)
from ..data.instruments import get_instrument_spec


class BacktestEngine:
    """
    Simulates chronological trade execution strictly on closed historical candles.
    Uses the identical ICT/SMC sequence tracker (Sweep -> MSS -> FVG -> Retracement)
    used in live/paper trading.
    Prevents look-ahead bias and future candle leakage.
    Simulates spread, commission, slippage, and conservative intra-bar ambiguity resolution.
    Generates detailed institutional performance metrics.
    """

    def __init__(self, config: BacktestConfig = BacktestConfig()):
        self.config = config

    def run(
        self,
        df: pd.DataFrame,
        tracker: SequenceTracker,
        symbol: str = "BTCUSDT",
        dataset_metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[BacktestMetrics, List[TradeRecord]]:
        """
        Executes backtest over historical candle DataFrame.
        """
        min_required = tracker.config.swing_left_bars + tracker.config.swing_right_bars + 3
        spec = get_instrument_spec(symbol)

        if df.empty or len(df) < min_required:
            empty_metrics = BacktestMetrics(
                initial_capital=self.config.initial_capital,
                final_equity=self.config.initial_capital,
                total_net_pnl=0.0,
                gross_profit=0.0,
                gross_loss=0.0,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                breakeven_trades=0,
                win_rate_pct=0.0,
                average_win=0.0,
                average_loss=0.0,
                average_r_multiple=0.0,
                expected_value_per_trade=0.0,
                profit_factor=0.0,
                max_drawdown_amount=0.0,
                max_drawdown_pct=0.0,
                return_on_capital_pct=0.0,
                total_fees_paid=0.0,
                long_trades_count=0,
                long_win_rate_pct=0.0,
                long_net_pnl=0.0,
                short_trades_count=0,
                short_win_rate_pct=0.0,
                short_net_pnl=0.0,
                consecutive_wins_max=0,
                consecutive_losses_max=0,
                dataset_metadata=dataset_metadata or {},
            )
            return empty_metrics, []

        # 1. Run the strategy sequence tracker over the data
        candidates = tracker.analyze(df)
        qualified_setups = [c for c in candidates if c.is_valid and c.entry_timestamp is not None]
        qualified_setups.sort(key=lambda s: s.entry_timestamp)

        equity = self.config.initial_capital
        peak_equity = equity
        max_drawdown_pct = 0.0
        max_drawdown_amt = 0.0
        trades: List[TradeRecord] = []
        trade_id_counter = 1
        last_exit_time = pd.Timestamp.min.tz_localize("UTC")
        equity_curve: List[Dict[str, Any]] = [{"timestamp": str(df.iloc[0]["timestamp"]), "equity": equity}]

        for setup in qualified_setups:
            if setup.entry_timestamp <= last_exit_time:
                continue

            raw_entry = setup.entry_price
            raw_stop = setup.stop_loss
            risk_per_unit = abs(raw_entry - raw_stop)
            if risk_per_unit <= 0:
                continue

            # Spread adjustment
            spread_cost = self.config.spread_pips * spec.tick_size
            if setup.direction == Direction.BULLISH:
                entry_price = (raw_entry + spread_cost) * (1.0 + self.config.slippage_pct)
                stop_loss = raw_stop
                take_profit = entry_price + (risk_per_unit * self.config.target_risk_reward)
            else:
                entry_price = (raw_entry - spread_cost) * (1.0 - self.config.slippage_pct)
                stop_loss = raw_stop
                take_profit = entry_price - (risk_per_unit * self.config.target_risk_reward)

            dollar_risk = equity * (self.config.risk_per_trade_pct / 100.0)
            position_size = dollar_risk / risk_per_unit
            notional_entry = position_size * entry_price
            entry_fee = notional_entry * self.config.fee_pct

            subsequent_candles = df[df["timestamp"] > setup.entry_timestamp]
            if subsequent_candles.empty:
                continue

            entry_dt = pd.Timestamp(setup.entry_timestamp)
            trade = TradeRecord(
                trade_id=trade_id_counter,
                symbol=symbol,
                direction=setup.direction,
                entry_time=entry_dt,
                entry_price=round(entry_price, spec.price_precision),
                stop_loss=round(stop_loss, spec.price_precision),
                take_profit=round(take_profit, spec.price_precision),
                position_size=position_size,
                risk_amount=dollar_risk,
                setup_timeframe=tracker.timeframe,
                day_of_week=entry_dt.strftime("%A"),
            )
            trade_id_counter += 1

            exit_found = False
            holding_bars = 0

            for _, bar in subsequent_candles.iterrows():
                holding_bars += 1
                bar_high = float(bar["high"])
                bar_low = float(bar["low"])
                bar_time = pd.Timestamp(bar["timestamp"])

                if setup.direction == Direction.BULLISH:
                    sl_hit = bar_low <= stop_loss
                    tp_hit = bar_high >= take_profit
                    # Conservative intra-bar ambiguity resolution
                    if sl_hit and tp_hit:
                        trade.exit_time = bar_time
                        trade.exit_price = round(stop_loss * (1.0 - self.config.slippage_pct), spec.price_precision)
                        trade.exit_reason = ExitReason.STOP_LOSS
                        trade.status = TradeStatus.LOSS
                        exit_found = True
                        break
                    elif sl_hit:
                        trade.exit_time = bar_time
                        trade.exit_price = round(stop_loss * (1.0 - self.config.slippage_pct), spec.price_precision)
                        trade.exit_reason = ExitReason.STOP_LOSS
                        trade.status = TradeStatus.LOSS
                        exit_found = True
                        break
                    elif tp_hit:
                        trade.exit_time = bar_time
                        trade.exit_price = round(take_profit * (1.0 - self.config.slippage_pct), spec.price_precision)
                        trade.exit_reason = ExitReason.TAKE_PROFIT
                        trade.status = TradeStatus.WIN
                        exit_found = True
                        break

                elif setup.direction == Direction.BEARISH:
                    sl_hit = bar_high >= stop_loss
                    tp_hit = bar_low <= take_profit
                    if sl_hit and tp_hit:
                        trade.exit_time = bar_time
                        trade.exit_price = round(stop_loss * (1.0 + self.config.slippage_pct), spec.price_precision)
                        trade.exit_reason = ExitReason.STOP_LOSS
                        trade.status = TradeStatus.LOSS
                        exit_found = True
                        break
                    elif sl_hit:
                        trade.exit_time = bar_time
                        trade.exit_price = round(stop_loss * (1.0 + self.config.slippage_pct), spec.price_precision)
                        trade.exit_reason = ExitReason.STOP_LOSS
                        trade.status = TradeStatus.LOSS
                        exit_found = True
                        break
                    elif tp_hit:
                        trade.exit_time = bar_time
                        trade.exit_price = round(take_profit * (1.0 + self.config.slippage_pct), spec.price_precision)
                        trade.exit_reason = ExitReason.TAKE_PROFIT
                        trade.status = TradeStatus.WIN
                        exit_found = True
                        break

                if holding_bars >= self.config.max_holding_bars:
                    trade.exit_time = bar_time
                    trade.exit_price = round(float(bar["close"]), spec.price_precision)
                    trade.exit_reason = ExitReason.TIMEOUT
                    trade.status = TradeStatus.EXPIRED
                    exit_found = True
                    break

            if not exit_found:
                last_bar = subsequent_candles.iloc[-1]
                trade.exit_time = pd.Timestamp(last_bar["timestamp"])
                trade.exit_price = round(float(last_bar["close"]), spec.price_precision)
                trade.exit_reason = ExitReason.TIMEOUT
                trade.status = TradeStatus.EXPIRED

            notional_exit = position_size * trade.exit_price
            exit_fee = notional_exit * self.config.fee_pct
            total_fees = entry_fee + exit_fee

            if setup.direction == Direction.BULLISH:
                gross_pnl = (trade.exit_price - trade.entry_price) * position_size
            else:
                gross_pnl = (trade.entry_price - trade.exit_price) * position_size

            net_pnl = gross_pnl - total_fees
            r_mult = net_pnl / dollar_risk if dollar_risk > 0 else 0.0

            trade.gross_pnl = round(gross_pnl, 2)
            trade.fee_paid = round(total_fees, 2)
            trade.net_pnl = round(net_pnl, 2)
            trade.r_multiple = round(r_mult, 2)
            trade.pnl_pct = round((net_pnl / dollar_risk) * 100.0, 2) if dollar_risk > 0 else 0.0

            if abs(trade.net_pnl) < 1.0:
                trade.status = TradeStatus.BREAKEVEN

            equity += net_pnl
            if equity > peak_equity:
                peak_equity = equity
            dd_amt = peak_equity - equity
            dd_pct = (dd_amt / peak_equity * 100.0) if peak_equity > 0 else 0.0
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct
            if dd_amt > max_drawdown_amt:
                max_drawdown_amt = dd_amt

            equity_curve.append({"timestamp": str(trade.exit_time), "equity": round(equity, 2)})
            last_exit_time = trade.exit_time
            trades.append(trade)

        total_trades = len(trades)
        winning_trades = sum(1 for t in trades if t.net_pnl > 0)
        losing_trades = sum(1 for t in trades if t.net_pnl < 0)
        breakeven_trades = sum(1 for t in trades if t.net_pnl == 0 or t.status == TradeStatus.BREAKEVEN)
        win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0

        wins_list = [t.net_pnl for t in trades if t.net_pnl > 0]
        losses_list = [abs(t.net_pnl) for t in trades if t.net_pnl < 0]

        gross_wins = sum(wins_list)
        gross_losses = sum(losses_list)
        avg_win = (gross_wins / len(wins_list)) if wins_list else 0.0
        avg_loss = (gross_losses / len(losses_list)) if losses_list else 0.0

        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (float("inf") if gross_wins > 0 else 0.0)
        total_net_pnl = equity - self.config.initial_capital
        roc = (total_net_pnl / self.config.initial_capital) * 100.0
        total_fees = sum(t.fee_paid for t in trades)

        r_multiples = [t.r_multiple for t in trades]
        avg_r = (sum(r_multiples) / len(r_multiples)) if r_multiples else 0.0
        ev = (total_net_pnl / total_trades) if total_trades > 0 else 0.0

        # Long vs Short
        longs = [t for t in trades if t.direction == Direction.BULLISH]
        shorts = [t for t in trades if t.direction == Direction.BEARISH]
        long_wins = sum(1 for t in longs if t.net_pnl > 0)
        short_wins = sum(1 for t in shorts if t.net_pnl > 0)

        # Consecutive streaks
        max_cw = 0
        curr_cw = 0
        max_cl = 0
        curr_cl = 0
        for t in trades:
            if t.net_pnl > 0:
                curr_cw += 1
                curr_cl = 0
                if curr_cw > max_cw:
                    max_cw = curr_cw
            elif t.net_pnl < 0:
                curr_cl += 1
                curr_cw = 0
                if curr_cl > max_cl:
                    max_cl = curr_cl

        # Performance by day
        perf_by_day: Dict[str, Dict[str, Any]] = {}
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
            d_trades = [t for t in trades if t.day_of_week == day]
            d_wins = sum(1 for t in d_trades if t.net_pnl > 0)
            d_pnl = sum(t.net_pnl for t in d_trades)
            perf_by_day[day] = {
                "trades": len(d_trades),
                "win_rate": round((d_wins / len(d_trades) * 100.0) if d_trades else 0.0, 1),
                "net_pnl": round(d_pnl, 2),
            }

        metrics = BacktestMetrics(
            initial_capital=round(self.config.initial_capital, 2),
            final_equity=round(equity, 2),
            total_net_pnl=round(total_net_pnl, 2),
            gross_profit=round(gross_wins, 2),
            gross_loss=round(gross_losses, 2),
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            breakeven_trades=breakeven_trades,
            win_rate_pct=round(win_rate, 1),
            average_win=round(avg_win, 2),
            average_loss=round(avg_loss, 2),
            average_r_multiple=round(avg_r, 2),
            expected_value_per_trade=round(ev, 2),
            profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
            max_drawdown_amount=round(max_drawdown_amt, 2),
            max_drawdown_pct=round(max_drawdown_pct, 2),
            return_on_capital_pct=round(roc, 2),
            total_fees_paid=round(total_fees, 2),
            long_trades_count=len(longs),
            long_win_rate_pct=round((long_wins / len(longs) * 100.0) if longs else 0.0, 1),
            long_net_pnl=round(sum(t.net_pnl for t in longs), 2),
            short_trades_count=len(shorts),
            short_win_rate_pct=round((short_wins / len(shorts) * 100.0) if shorts else 0.0, 1),
            short_net_pnl=round(sum(t.net_pnl for t in shorts), 2),
            consecutive_wins_max=max_cw,
            consecutive_losses_max=max_cl,
            performance_by_day=perf_by_day,
            equity_curve=equity_curve,
            dataset_metadata=dataset_metadata or {},
        )

        return metrics, trades
