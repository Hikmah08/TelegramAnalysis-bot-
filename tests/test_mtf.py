import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta
from src.strategy import (
    StrategyConfig,
    BiasState,
    Direction,
    HigherTimeframeBias,
    AreaOfInterest,
    MultiTimeframeAnalyzer,
    MultiTimeframeOrchestrator,
)
from src.strategy.models import SetupCandidate, SetupStage, LiquiditySweep, MarketStructureBreak, StructureType, FairValueGap, SwingPoint, SwingType


def make_candle_df(candles_data, start_time=None, timeframe_minutes=60):
    base_time = start_time or datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)
    records = []
    for i, c in enumerate(candles_data):
        ts = base_time + timedelta(minutes=timeframe_minutes * i)
        cts = ts + timedelta(minutes=timeframe_minutes - 1, seconds=59, milliseconds=999)
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


def test_4h_bias_determination_bullish_mss():
    analyzer = MultiTimeframeAnalyzer()
    # 4H candles with a confirmed swing high at idx 2 (100) and MSS body close at idx 6 (105)
    candles = [
        [90, 95, 88, 92],    # 0
        [92, 98, 91, 96],    # 1
        [96, 100, 94, 98],   # 2 - Swing High (100)
        [98, 99, 93, 94],    # 3
        [94, 95, 89, 90],    # 4 - Swing High confirmed
        [90, 98, 89, 97],    # 5
        [97, 108, 96, 105],  # 6 - MSS: Body close at 105 > 100
        [105, 107, 102, 104],# 7
        [104, 106, 101, 103],# 8
        [103, 105, 100, 102],# 9
        [102, 104, 99, 101], # 10
    ]
    df_4h = make_candle_df(candles, timeframe_minutes=240)
    bias = analyzer.determine_4h_bias(df_4h)

    assert bias.direction == BiasState.BULLISH
    assert "BULLISH" in bias.reason.upper()


def test_lower_timeframe_counter_trend_is_rejected():
    orchestrator = MultiTimeframeOrchestrator()

    # Synthetic 4H bias: BEARISH
    candles_4h = [
        [110, 112, 108, 109],  # 0
        [109, 111, 105, 106],  # 1
        [106, 107, 95, 98],    # 2 - Swing Low (95)
        [98, 100, 96, 97],     # 3
        [97, 101, 96, 99],     # 4 - Confirmed
        [99, 100, 93, 94],     # 5
        [94, 95, 85, 88],      # 6 - Bearish MSS: close 88 < 95
        [88, 90, 84, 87],      # 7
        [87, 89, 83, 85],      # 8
        [85, 87, 82, 84],      # 9
        [84, 86, 81, 83],      # 10
    ]
    df_4h = make_candle_df(candles_4h, timeframe_minutes=240)

    # 1H candles with a Bearish AOI (Bearish FVG)
    candles_1h = [
        [100, 102, 98, 100],   # 0
        [100, 101, 88, 89],    # 1 - C1 FVG: Low 88
        [89, 90, 75, 76],      # 2 - C2 Displacement
        [76, 78, 70, 72],      # 3 - C3 High 78 < C1 Low 88 -> Bearish FVG [78, 88]
        [72, 75, 71, 74],      # 4
        [74, 76, 72, 75],      # 5
        [75, 77, 73, 76],      # 6
        [76, 78, 74, 77],      # 7
        [77, 79, 75, 78],      # 8
        [78, 80, 76, 79],      # 9
        [79, 81, 77, 80],      # 10
    ]
    df_1h = make_candle_df(candles_1h, timeframe_minutes=60)

    # 15M candles containing a BULLISH setup (which is COUNTER to the 4H Bearish Bias)
    candles_15m = [
        [80, 82, 78, 80],
        [80, 81, 76, 77],
        [77, 79, 70, 72],     # Swing Low 70
        [72, 75, 71, 74],
        [74, 76, 72, 75],     # Confirmed
        [75, 76, 68, 72],     # Sweep of 70
        [72, 85, 71, 84],     # Bullish MSS
        [84, 90, 82, 88],     # Bullish FVG
        [88, 89, 81, 85],     # Retraced
        [85, 87, 84, 86],
        [86, 88, 85, 87],
    ]
    df_15m = make_candle_df(candles_15m, timeframe_minutes=15)
    df_5m = make_candle_df(candles_15m, timeframe_minutes=5)
    df_1m = make_candle_df(candles_15m, timeframe_minutes=1)

    timeframe_dfs = {
        "4h": df_4h,
        "1h": df_1h,
        "15m": df_15m,
        "5m": df_5m,
        "1m": df_1m,
    }

    # Should reject the 15M Bullish setup because 4H bias is BEARISH!
    setups = orchestrator.scan_market(timeframe_dfs)
    assert len(setups) == 0
