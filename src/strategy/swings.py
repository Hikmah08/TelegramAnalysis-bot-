from typing import List, Tuple
import pandas as pd
from .models import SwingPoint, SwingType
from .config import StrategyConfig


def find_confirmed_swings(
    df: pd.DataFrame,
    config: StrategyConfig = StrategyConfig(),
) -> List[SwingPoint]:
    """
    Identifies confirmed fractal swing highs and lows strictly without look-ahead bias.

    For an N-left, N-right fractal (default 2-left, 2-right = 5 bars):
    - Candle `i` is the candidate central bar.
    - Candle `i` is evaluated against indices `[i - left_bars, i + right_bars]`.
    - CRITICAL: A swing at index `i` is CONFIRMED ONLY at index `i + right_bars`
      when candle `i + right_bars` has fully closed.
    """
    if len(df) < (config.swing_left_bars + config.swing_right_bars + 1):
        return []

    swings: List[SwingPoint] = []
    left = config.swing_left_bars
    right = config.swing_right_bars

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    timestamps = pd.to_datetime(df["timestamp"]).to_numpy()
    close_times = pd.to_datetime(df["close_time"]).to_numpy()

    total_candles = len(df)

    # We iterate such that candidate index i has sufficient bars to its right
    for i in range(left, total_candles - right):
        center_high = highs[i]
        center_low = lows[i]

        # Check Swing High
        is_swing_high = True
        for j in range(i - left, i + right + 1):
            if j != i and highs[j] >= center_high:
                is_swing_high = False
                break

        # Check Swing Low
        is_swing_low = True
        for j in range(i - left, i + right + 1):
            if j != i and lows[j] <= center_low:
                is_swing_low = False
                break

        confirmation_idx = i + right
        confirmed_time = pd.Timestamp(close_times[confirmation_idx])

        if is_swing_high:
            swings.append(
                SwingPoint(
                    swing_type=SwingType.SWING_HIGH,
                    price=float(center_high),
                    candle_index=i,
                    timestamp=pd.Timestamp(timestamps[i]),
                    confirmed_at_index=confirmation_idx,
                    confirmed_at_time=confirmed_time,
                )
            )

        if is_swing_low:
            swings.append(
                SwingPoint(
                    swing_type=SwingType.SWING_LOW,
                    price=float(center_low),
                    candle_index=i,
                    timestamp=pd.Timestamp(timestamps[i]),
                    confirmed_at_index=confirmation_idx,
                    confirmed_at_time=confirmed_time,
                )
            )

    # Sort swings chronologically by confirmation index
    swings.sort(key=lambda s: (s.confirmed_at_index, s.candle_index))
    return swings
