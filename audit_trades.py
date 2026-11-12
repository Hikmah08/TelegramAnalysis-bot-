import sys
from pathlib import Path
import pandas as pd

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.data import BinancePublicDataProvider, Timeframe
from src.strategy import SequenceTracker, StrategyConfig
from src.backtest import BacktestEngine, BacktestConfig


def audit_backtest():
    symbol = "BTCUSDT"
    timeframe = Timeframe.M15
    limit = 500

    provider = BinancePublicDataProvider()
    df = provider.fetch_candles(symbol=symbol, timeframe=timeframe, limit=limit)

    strategy_config = StrategyConfig(swing_left_bars=2, swing_right_bars=2)
    backtest_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade_pct=1.0,
        target_risk_reward=2.0,
        fee_pct=0.0006,
        slippage_pct=0.0002,
        max_holding_bars=100,
    )

    tracker = SequenceTracker(timeframe=timeframe.value, config=strategy_config)
    candidates = tracker.analyze(df)
    qualified = [c for c in candidates if c.is_valid and c.entry_timestamp is not None]

    engine = BacktestEngine(config=backtest_config)
    metrics, trades = engine.run(df, tracker, symbol=symbol)

    print(f"TOTAL CANDIDATES QUALIFIED: {len(qualified)}")
    print(f"TOTAL EXECUTED TRADES: {len(trades)}\n")

    gross_profit = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
    gross_loss = sum(t.gross_pnl for t in trades if t.gross_pnl <= 0)

    print(f"Gross Profit: ${gross_profit:,.2f}")
    print(f"Gross Loss:   ${gross_loss:,.2f}")
    print(f"Net PnL:      ${metrics.total_net_pnl:,.2f}")
    print(f"Win Rate:     {metrics.win_rate_pct:.1f}%")
    print(f"Drawdown:     {metrics.max_drawdown_pct:.2f}%\n")

    print("=" * 110)
    print(f"{'ID':<3} | {'DIR':<7} | {'ENTRY TIME':<16} | {'ENTRY':<9} | {'STOP':<9} | {'TP':<9} | {'EXIT TIME':<16} | {'EXIT':<9} | {'REASON':<11} | {'NET PNL':<9} | {'R-MULT':<6}")
    print("=" * 110)

    # Match each trade back to its setup candidate
    for t in trades:
        # Find matching setup candidate
        setup = next((s for s in qualified if s.entry_timestamp == t.entry_time and s.direction == t.direction), None)
        r_mult = t.net_pnl / t.risk_amount if t.risk_amount > 0 else 0.0

        print(
            f"{t.trade_id:<3} | {t.direction.value.upper():<7} | "
            f"{t.entry_time.strftime('%Y-%m-%d %H:%M'):<16} | {t.entry_price:<9.2f} | {t.stop_loss:<9.2f} | {t.take_profit:<9.2f} | "
            f"{t.exit_time.strftime('%Y-%m-%d %H:%M'):<16} | {t.exit_price:<9.2f} | {t.exit_reason.value.upper():<11} | "
            f"${t.net_pnl:<+8.2f} | {r_mult:<+5.2f}R"
        )
        if setup:
            print(f"    * Sweep: level={setup.sweep.swept_level:.2f}, wick={setup.sweep.penetration_price:.2f} at {setup.sweep.timestamp}")
            print(f"    * MSS:   level={setup.structure_break.broken_swing.price:.2f}, close={setup.structure_break.break_price:.2f} at {setup.structure_break.timestamp}")
            print(f"    * FVG:   [{setup.fvg.bottom_price:.2f} - {setup.fvg.top_price:.2f}] at {setup.fvg.timestamp}")
            print(f"    * Notes: {setup.audit_notes[-1]}")
            print("-" * 110)


if __name__ == "__main__":
    audit_backtest()
