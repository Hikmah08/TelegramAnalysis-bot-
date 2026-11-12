import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta
from src.strategy import (
    StrategyConfig,
    Direction,
    SetupStage,
    SequenceTracker,
)
from src.backtest import (
    BacktestEngine,
    BacktestConfig,
    TradeStatus,
    ExitReason,
)


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


def test_controlled_bearish_winning_trade():
    """
    Deterministic Bearish setup:
    1. Swing High at 100
    2. Swing Low at 80
    3. Sweep of Swing High: wick to 105, close at 98
    4. MSS: close below 80 at 75
    5. Bearish FVG: [74 - 78]
    6. Retrace into FVG at 74
    7. Price drops to hit 2R Take Profit
    """
    candles = [
        # Form Swing Low at idx 2 (80)
        [90, 92, 85, 88],    # 0
        [88, 90, 82, 85],    # 1
        [85, 87, 80, 84],    # 2 - Swing Low (80)
        [84, 88, 83, 87],    # 3
        [87, 95, 86, 94],    # 4 - Confirmed Swing Low

        # Form Swing High at idx 5 (100)
        [94, 96, 92, 95],    # 5
        [95, 98, 94, 97],    # 6
        [97, 100, 96, 99],   # 7 - Swing High (100)
        [99, 99, 95, 96],    # 8
        [96, 97, 92, 93],    # 9 - Confirmed Swing High

        # Sweep of Swing High (100) at idx 10: wick to 105, close 98
        [93, 105, 92, 98],   # 10 - Sweep

        # Bearish displacement & MSS at idx 12
        [98, 99, 82, 84],    # 11 - C1 FVG: Low 82
        [84, 85, 68, 70],    # 12 - C2 MSS past 80
        [70, 78, 67, 72],    # 13 - C3 High 78 < C1 Low 82 -> Bearish FVG [78, 82]

        # Retracement into FVG [78, 82] at idx 14: High 79 enters gap!
        # Entry at 78. Stop loss at Sweep High (105). Risk = 105 - 78 = 27.
        # Take Profit (2R) = 78 - (27 * 2) = 24.
        [72, 79, 71, 74],    # 14 - Retrace entry

        # Subsequent candles drop to 20, hitting TP at 24!
        [74, 75, 40, 42],    # 15
        [42, 45, 20, 22],    # 16 - Hits TP!
    ]
    df = make_candle_df(candles)
    tracker = SequenceTracker(timeframe="15m", config=StrategyConfig(swing_left_bars=2, swing_right_bars=2))
    engine = BacktestEngine(config=BacktestConfig(initial_capital=10000.0, target_risk_reward=2.0))

    metrics, trades = engine.run(df, tracker, symbol="BTCUSDT")

    assert metrics.total_trades == 1
    assert metrics.winning_trades == 1
    assert trades[0].direction == Direction.BEARISH
    assert trades[0].status == TradeStatus.WIN
    assert trades[0].exit_reason == ExitReason.TAKE_PROFIT
    assert trades[0].net_pnl > 0.0


def test_controlled_both_sl_and_tp_touched_in_same_candle():
    """
    When an intra-bar candle touches both SL and TP, the engine must
    conservatively assume Stop Loss was hit first (preventing optimistic bias).
    """
    candles = [
        # Form initial Swing High at idx 2 (110)
        [95, 100, 94, 98],
        [98, 105, 97, 103],
        [103, 110, 102, 108],
        [108, 109, 104, 105],
        [105, 106, 95, 96],

        # Form Swing Low at idx 5 (85)
        [96, 98, 85, 89],
        [89, 94, 88, 93],
        [93, 97, 91, 95],

        # Sweep of 85 (wick 80)
        [95, 96, 80, 88],

        # MSS & FVG
        [88, 100, 87, 99],
        [99, 116, 98, 115],
        [115, 120, 105, 118],

        # Entry at 105. Stop at 80. TP at 155.
        [118, 119, 103, 114],

        # Candle 13 has a massive range: Low 70 (breaches SL 80) and High 170 (breaches TP 155)
        [114, 170, 70, 120],
    ]
    df = make_candle_df(candles)
    tracker = SequenceTracker(timeframe="15m", config=StrategyConfig(swing_left_bars=2, swing_right_bars=2))
    engine = BacktestEngine(config=BacktestConfig(initial_capital=10000.0, target_risk_reward=2.0))

    metrics, trades = engine.run(df, tracker, symbol="BTCUSDT")

    assert metrics.total_trades == 1
    # Conservative execution rule: must trigger Stop Loss, NOT Take Profit!
    assert trades[0].status == TradeStatus.LOSS
    assert trades[0].exit_reason == ExitReason.STOP_LOSS
