from typing import List, Optional
import pandas as pd
from .models import SwingPoint, SwingType, LiquiditySweep, Direction
from .config import StrategyConfig


def check_candle_for_sweeps(
    candle_idx: int,
    candle: pd.Series,
    active_swings: List[SwingPoint],
    timeframe: str,
    config: StrategyConfig = StrategyConfig(),
) -> List[LiquiditySweep]:
    """
    Checks if a given closed candle swept any previously confirmed swing levels.

    Rules:
    - Candle index MUST be strictly greater than swing.confirmed_at_index.
    - Bullish Sweep: Low penetrates below swing_low.price, BUT Close >= swing_low.price.
      (Wick below, close back inside).
    - Bearish Sweep: High penetrates above swing_high.price, BUT Close <= swing_high.price.
      (Wick above, close back inside).
    - If candle body closes beyond the level, it does NOT qualify as a sweep.
    """
    sweeps: List[LiquiditySweep] = []
    c_open = candle["open"]
    c_high = candle["high"]
    c_low = candle["low"]
    c_close = candle["close"]
    c_timestamp = candle["timestamp"]

    for swing in active_swings:
        # A swing cannot be swept before it is confirmed
        if candle_idx <= swing.confirmed_at_index:
            continue

        if swing.swing_type == SwingType.SWING_LOW:
            # Bullish Liquidity Sweep condition:
            # Low wicks below swing low price
            if c_low < swing.price:
                # Body must close back inside (>= swing price)
                if config.sweep_confirmation_mode == "wick_only":
                    if c_close >= swing.price:
                        sweeps.append(
                            LiquiditySweep(
                                direction=Direction.BULLISH,
                                swept_level=swing.price,
                                penetration_price=float(c_low),
                                candle_index=candle_idx,
                                timestamp=pd.Timestamp(c_timestamp),
                                timeframe=timeframe,
                                target_swing=swing,
                            )
                        )
                else:
                    sweeps.append(
                        LiquiditySweep(
                            direction=Direction.BULLISH,
                            swept_level=swing.price,
                            penetration_price=float(c_low),
                            candle_index=candle_idx,
                            timestamp=pd.Timestamp(c_timestamp),
                            timeframe=timeframe,
                            target_swing=swing,
                        )
                    )

        elif swing.swing_type == SwingType.SWING_HIGH:
            # Bearish Liquidity Sweep condition:
            # High wicks above swing high price
            if c_high > swing.price:
                # Body must close back inside (<= swing price)
                if config.sweep_confirmation_mode == "wick_only":
                    if c_close <= swing.price:
                        sweeps.append(
                            LiquiditySweep(
                                direction=Direction.BEARISH,
                                swept_level=swing.price,
                                penetration_price=float(c_high),
                                candle_index=candle_idx,
                                timestamp=pd.Timestamp(c_timestamp),
                                timeframe=timeframe,
                                target_swing=swing,
                            )
                        )
                else:
                    sweeps.append(
                        LiquiditySweep(
                            direction=Direction.BEARISH,
                            swept_level=swing.price,
                            penetration_price=float(c_high),
                            candle_index=candle_idx,
                            timestamp=pd.Timestamp(c_timestamp),
                            timeframe=timeframe,
                            target_swing=swing,
                        )
                    )

    return sweeps
