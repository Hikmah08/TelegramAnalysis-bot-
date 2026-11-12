import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta
from src.strategy import StrategyConfig, SequenceTracker
from src.backtest import BacktestEngine, BacktestConfig, TradeStatus, ExitReason


def make_candle_df(candles_data, start_time=None):
    base_time = start_time or datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
    records = []
    for i, c in enumerate(candles_data):
        ts = base_time + timedelta(minutes=15 * i)
        cts = ts + timedelta(minutes=14, seconds=59, milliseconds=999)
        records.append({
            "timestamp": ts,
            "open": float(c[0]),
            "high": float(c[1]),
            "low": float(c[2]),
            "close": float(c[3]),
            "volume": 100.0,
            "close_time": cts,
        })
    return pd.DataFrame(records)


def test_backtest_winning_bullish_trade():
    """
    Simulates a complete setup that enters and reaches 2:1 Take Profit.
    """
    candles = [
        # Form initial Swing High at idx 2 (110)
        [95, 100, 94, 98],    # 0
        [98, 105, 97, 103],   # 1
        [103, 110, 102, 108], # 2 - Swing High (110)
        [108, 109, 104, 105], # 3
        [105, 106, 95, 96],   # 4 - Swing High confirmed

        # Form Swing Low at idx 5 (85)
        [96, 98, 85, 89],     # 5 - Swing Low (85)
        [89, 94, 88, 93],     # 6
        [93, 97, 91, 95],     # 7 - Swing Low confirmed

        # Sweep at idx 8
        [95, 96, 80, 88],     # 8 - Sweep of 85 (wick 80, close 88)

        # MSS at idx 10
        [88, 100, 87, 99],    # 9
        [99, 116, 98, 115],   # 10 - MSS past 110
        [115, 120, 105, 118], # 11 - FVG [100 - 105] formed

        # Retracement Entry at idx 12
        [118, 119, 103, 114], # 12 - Low 103 triggers entry at 105. Stop = 80. Risk = 25. TP (2R) = 155.

        # Subsequent price moves up to hit TP
        [114, 130, 112, 128], # 13
        [128, 160, 126, 158], # 14 - High 160 hits TP at 155!
    ]
    df = make_candle_df(candles)
    tracker = SequenceTracker(timeframe="15m", config=StrategyConfig(swing_left_bars=2, swing_right_bars=2))
    engine = BacktestEngine(config=BacktestConfig(initial_capital=10000.0, target_risk_reward=2.0))

    metrics, trades = engine.run(df, tracker, symbol="BTCUSDT")

    assert metrics.total_trades == 1
    assert metrics.winning_trades == 1
    assert metrics.win_rate_pct == 100.0
    assert metrics.total_net_pnl > 0.0
    assert trades[0].status == TradeStatus.WIN
    assert trades[0].exit_reason == ExitReason.TAKE_PROFIT


def test_backtest_losing_trade_hits_stop_loss():
    """
    Simulates a setup that triggers entry but price continues downward hitting the stop loss.
    """
    candles = [
        # Form initial Swing High at idx 2 (110)
        [95, 100, 94, 98],    # 0
        [98, 105, 97, 103],   # 1
        [103, 110, 102, 108], # 2 - Swing High (110)
        [108, 109, 104, 105], # 3
        [105, 106, 95, 96],   # 4 - Swing High confirmed

        # Form Swing Low at idx 5 (85)
        [96, 98, 85, 89],     # 5 - Swing Low (85)
        [89, 94, 88, 93],     # 6
        [93, 97, 91, 95],     # 7 - Swing Low confirmed

        # Sweep at idx 8
        [95, 96, 80, 88],     # 8 - Sweep of 85 (wick 80)

        # MSS at idx 10
        [88, 100, 87, 99],    # 9
        [99, 116, 98, 115],   # 10
        [115, 120, 105, 118], # 11 - FVG [100 - 105]

        # Retracement Entry at idx 12
        [118, 119, 103, 114], # 12 - Triggers entry at 105. Stop = 80.

        # Price dumps to 75, hitting the stop loss at 80
        [114, 115, 75, 76],   # 13 - Low 75 breaches stop loss!
    ]
    df = make_candle_df(candles)
    tracker = SequenceTracker(timeframe="15m", config=StrategyConfig(swing_left_bars=2, swing_right_bars=2))
    engine = BacktestEngine(config=BacktestConfig(initial_capital=10000.0, target_risk_reward=2.0))

    metrics, trades = engine.run(df, tracker, symbol="BTCUSDT")

    assert metrics.total_trades == 1
    assert metrics.losing_trades == 1
    assert metrics.win_rate_pct == 0.0
    assert metrics.total_net_pnl < 0.0
    assert trades[0].status == TradeStatus.LOSS
    assert trades[0].exit_reason == ExitReason.STOP_LOSS
