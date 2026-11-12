import sys
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.data import BinancePublicDataProvider, Timeframe
from src.strategy import SequenceTracker, StrategyConfig
from src.backtest import BacktestEngine, BacktestConfig


def main():
    symbol = "BTCUSDT"
    timeframe = Timeframe.M15
    limit = 500  # Pull 500 historical closed candles

    print("=" * 65)
    print(f" ICT/SMC Historical Backtest Runner ")
    print(f" Symbol: {symbol} | Timeframe: {timeframe.value.upper()} | Candles: {limit}")
    print("=" * 65)

    provider = BinancePublicDataProvider()
    df = provider.fetch_candles(symbol=symbol, timeframe=timeframe, limit=limit)

    if df.empty:
        print("Failed to fetch historical candle data.")
        return

    print(f"Dataset Range: {df.iloc[0]['timestamp'].strftime('%Y-%m-%d %H:%M')} to {df.iloc[-1]['close_time'].strftime('%Y-%m-%d %H:%M')} UTC")

    # Strategy & Backtest configurations
    strategy_config = StrategyConfig(swing_left_bars=2, swing_right_bars=2)
    backtest_config = BacktestConfig(
        initial_capital=10000.0,
        risk_per_trade_pct=1.0,
        target_risk_reward=2.0,
        fee_pct=0.0006,       # 0.06% taker fee
        slippage_pct=0.0002,  # 0.02% slippage
        max_holding_bars=100,
    )

    tracker = SequenceTracker(timeframe=timeframe.value, config=strategy_config)
    engine = BacktestEngine(config=backtest_config)

    metrics, trades = engine.run(df, tracker, symbol=symbol)

    print("\n" + "=" * 65)
    print(" BACKTEST PERFORMANCE REPORT ")
    print("=" * 65)
    print(f"Initial Capital:         ${metrics.initial_capital:,.2f}")
    print(f"Final Capital:           ${metrics.final_equity:,.2f}")
    print(f"Total Net PnL:           ${metrics.total_net_pnl:,.2f} ({metrics.return_on_capital_pct:+.2f}%)")
    print(f"Total Executed Trades:   {metrics.total_trades}")
    print(f"Winning Trades:          {metrics.winning_trades}")
    print(f"Losing Trades:           {metrics.losing_trades}")
    print(f"Win Rate:                {metrics.win_rate_pct:.1f}%")
    print(f"Profit Factor:           {metrics.profit_factor:.2f}")
    print(f"Max Drawdown:            {metrics.max_drawdown_pct:.2f}%")
    print(f"Total Fees & Frictions:  ${metrics.total_fees_paid:,.2f}")
    print(f"Average R-Multiple:      {metrics.average_r_multiple:+.2f}R")
    print("=" * 65)

    if trades:
        print("\nLast 5 Executed Trades:")
        for t in trades[-5:]:
            print(
                f"Trade #{t.trade_id:>2} | {t.direction.value.upper():<7} | "
                f"Entry: {t.entry_time.strftime('%m-%d %H:%M')} @ {t.entry_price:.2f} | "
                f"Exit: {t.exit_time.strftime('%m-%d %H:%M')} @ {t.exit_price:.2f} | "
                f"Reason: {t.exit_reason.value.upper():<11} | Net PnL: ${t.net_pnl:+7.2f} ({t.status.value.upper()})"
            )
        print("=" * 65)


if __name__ == "__main__":
    main()
