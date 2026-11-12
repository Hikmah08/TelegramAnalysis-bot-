import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta
from src.strategy import (
    StrategyConfig,
    SwingType,
    Direction,
    SetupStage,
    find_confirmed_swings,
    check_candle_for_sweeps,
    check_candle_for_structure_break,
    check_candle_for_fvg,
    SequenceTracker,
)


def make_candle_df(candles_data):
    """Helper to convert list of [open, high, low, close] into DataFrame with valid UTC timestamps."""
    base_time = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
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


def test_fractal_swing_detection_and_confirmation_lag():
    # 5 bars where bar 2 is a peak swing high
    data = [
        [100, 105, 99, 102],   # 0
        [102, 108, 101, 106],  # 1
        [106, 120, 105, 115],  # 2 - Central peak Swing High
        [115, 116, 110, 112],  # 3
        [112, 114, 108, 110],  # 4 - Right bar 2 (Swing 2 confirmed here)
        [110, 111, 107, 109],  # 5
    ]
    df = make_candle_df(data)
    swings = find_confirmed_swings(df, StrategyConfig(swing_left_bars=2, swing_right_bars=2))

    high_swings = [s for s in swings if s.swing_type == SwingType.SWING_HIGH]
    assert len(high_swings) == 1
    assert high_swings[0].candle_index == 2
    assert high_swings[0].price == 120.0
    # Crucial anti-lookahead check: confirmed at index 4 (i + right_bars)
    assert high_swings[0].confirmed_at_index == 4


def test_liquidity_sweep_wick_only():
    # Setup a swing low
    data = [
        [100, 105, 95, 102],   # 0
        [102, 103, 93, 101],   # 1
        [101, 102, 85, 98],    # 2 - Central Swing Low at 85
        [98, 100, 90, 99],     # 3
        [99, 104, 91, 103],    # 4 - Confirmed here
        # Candle 5: wicks below 85 down to 80, but closes back inside at 88
        [103, 104, 80, 88],    # 5
    ]
    df = make_candle_df(data)
    swings = find_confirmed_swings(df, StrategyConfig(swing_left_bars=2, swing_right_bars=2))
    sweeps = check_candle_for_sweeps(5, df.iloc[5], swings, "15m", StrategyConfig())

    assert len(sweeps) == 1
    assert sweeps[0].direction == Direction.BULLISH
    assert sweeps[0].swept_level == 85.0
    assert sweeps[0].penetration_price == 80.0


def test_body_close_beyond_disqualifies_sweep():
    # Candle closes BELOW the swing low (breakout / continuation, NOT a sweep)
    data = [
        [100, 105, 95, 102],   # 0
        [102, 103, 93, 101],   # 1
        [101, 102, 85, 98],    # 2 - Central Swing Low at 85
        [98, 100, 90, 99],     # 3
        [99, 104, 91, 103],    # 4 - Confirmed here
        # Candle 5: wicks below 85 and CLOSES below 85 at 82
        [103, 103, 80, 82],    # 5
    ]
    df = make_candle_df(data)
    swings = find_confirmed_swings(df, StrategyConfig(swing_left_bars=2, swing_right_bars=2))
    sweeps = check_candle_for_sweeps(5, df.iloc[5], swings, "15m", StrategyConfig())

    # Disqualified: close is below level
    assert len(sweeps) == 0


def test_mss_requires_candle_body_close():
    data = [
        [100, 105, 99, 102],   # 0
        [102, 108, 101, 106],  # 1
        [106, 115, 105, 112],  # 2 - Swing High at 115
        [112, 114, 108, 110],  # 3
        [110, 111, 107, 109],  # 4 - Confirmed here
        # Candle 5: wicks above 115 to 118, but closes at 114 (wick-only, no body close)
        [109, 118, 108, 114],  # 5
        # Candle 6: closes above 115 at 117 (body close)
        [114, 119, 113, 117],  # 6
    ]
    df = make_candle_df(data)
    swings = find_confirmed_swings(df, StrategyConfig(swing_left_bars=2, swing_right_bars=2))

    # Candle 5 should NOT confirm MSS
    breaks_5 = check_candle_for_structure_break(5, df.iloc[5], swings, StrategyConfig())
    assert len(breaks_5) == 0

    # Candle 6 SHOULD confirm MSS
    breaks_6 = check_candle_for_structure_break(6, df.iloc[6], swings, StrategyConfig())
    assert len(breaks_6) == 1
    assert breaks_6[0].direction == Direction.BULLISH
    assert breaks_6[0].break_price == 117.0


def test_bullish_complete_sequence():
    """
    Complete valid Bullish Sequence:
    1. Swing Low confirmed at 85
    2. Swing High confirmed at 110
    3. Sweep of Swing Low (wick to 82, close 87)
    4. MSS: candle body close above Swing High (close at 112)
    5. FVG forms during expansion
    6. Retracement into FVG -> Setup marked as candidate for review
    """
    candles = [
        # Form initial Swing High at idx 2 (110)
        [95, 100, 94, 98],    # 0
        [98, 105, 97, 103],   # 1
        [103, 110, 102, 108], # 2 - Swing High (110)
        [108, 109, 104, 105], # 3
        [105, 106, 95, 96],   # 4 - Swing High confirmed here

        # Form Swing Low at idx 5 (85)
        [96, 98, 85, 89],     # 5 - Swing Low (85)
        [89, 94, 88, 93],     # 6
        [93, 97, 91, 95],     # 7 - Swing Low confirmed here

        # Step 1: Liquidity Sweep of Swing Low at idx 8
        [95, 96, 80, 88],     # 8 - Sweep of 85 (wick 80, close 88)

        # Step 2: Bullish displacement & MSS at idx 10
        # Candle 9 (C1 for FVG): High = 100
        [88, 100, 87, 99],    # 9
        # Candle 10 (C2): Massive displacement, closes at 115 (MSS past 110)
        [99, 116, 98, 115],   # 10
        # Candle 11 (C3 for FVG): Low = 105. High of C1 (100) < Low of C3 (105) -> FVG [100 - 105]
        [115, 120, 105, 118], # 11 - FVG formed

        # Step 3: Retracement into FVG [100 - 105] at idx 12
        [118, 119, 103, 114], # 12 - Low 103 enters FVG [100, 105]!
    ]
    df = make_candle_df(candles)
    tracker = SequenceTracker(timeframe="15m", config=StrategyConfig(swing_left_bars=2, swing_right_bars=2))
    candidates = tracker.analyze(df)

    valid_candidates = [c for c in candidates if c.is_valid and c.stage == SetupStage.ENTRY_RETRACED]
    assert len(valid_candidates) == 1
    c = valid_candidates[0]
    assert c.direction == Direction.BULLISH
    assert c.sweep.swept_level == 85.0
    assert c.structure_break.broken_swing.price == 110.0
    assert c.fvg.bottom_price == 100.0
    assert c.fvg.top_price == 105.0
    assert c.entry_price == 105.0
    assert c.stop_loss == 80.0  # Sweep low extreme


def test_invalid_event_sequence_rejected():
    """
    If an FVG and retracement occur WITHOUT a prior liquidity sweep,
    it must NOT qualify as a complete setup.
    """
    candles = [
        [95, 100, 94, 98],    # 0
        [98, 105, 97, 103],   # 1
        [103, 110, 102, 108], # 2 - Swing High
        [108, 109, 104, 105], # 3
        [105, 106, 95, 96],   # 4 - Confirmed
        # No sweep occurs! Just a move up
        [96, 101, 95, 100],   # 5
        [100, 116, 99, 115],  # 6
        [115, 120, 108, 119], # 7
        [119, 119, 105, 116], # 8
    ]
    df = make_candle_df(candles)
    tracker = SequenceTracker(timeframe="15m", config=StrategyConfig(swing_left_bars=2, swing_right_bars=2))
    candidates = tracker.analyze(df)

    # No candidate should have reached ENTRY_RETRACED
    valid_candidates = [c for c in candidates if c.is_valid]
    assert len(valid_candidates) == 0
