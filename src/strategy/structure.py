from typing import List, Optional
import pandas as pd
from .models import SwingPoint, SwingType, MarketStructureBreak, StructureType, Direction
from .config import StrategyConfig


def check_candle_for_structure_break(
    candle_idx: int,
    candle: pd.Series,
    active_swings: List[SwingPoint],
    config: StrategyConfig = StrategyConfig(),
) -> List[MarketStructureBreak]:
    """
    Checks if a given closed candle formed a valid MSS or BOS.

    Rules:
    - Candle index MUST be strictly greater than swing.confirmed_at_index.
    - Default confirmation: Candle BODY CLOSE beyond the swing level.
      * Bullish MSS/BOS: candle['close'] > swing_high.price.
      * Bearish MSS/BOS: candle['close'] < swing_low.price.
    - Wick penetration without body close is rejected.
    """
    breaks: List[MarketStructureBreak] = []
    c_close = candle["close"]
    c_timestamp = candle["timestamp"]

    for swing in active_swings:
        if candle_idx <= swing.confirmed_at_index:
            continue

        if swing.swing_type == SwingType.SWING_HIGH:
            # Bullish break requires candle close above swing high
            if c_close > swing.price:
                displacement = (c_close - swing.price) / swing.price
                if displacement >= config.min_displacement_pct:
                    breaks.append(
                        MarketStructureBreak(
                            structure_type=StructureType.MSS,
                            direction=Direction.BULLISH,
                            broken_swing=swing,
                            break_price=float(c_close),
                            candle_index=candle_idx,
                            timestamp=pd.Timestamp(c_timestamp),
                            displacement_pct=displacement,
                        )
                    )

        elif swing.swing_type == SwingType.SWING_LOW:
            # Bearish break requires candle close below swing low
            if c_close < swing.price:
                displacement = (swing.price - c_close) / swing.price
                if displacement >= config.min_displacement_pct:
                    breaks.append(
                        MarketStructureBreak(
                            structure_type=StructureType.MSS,
                            direction=Direction.BEARISH,
                            broken_swing=swing,
                            break_price=float(c_close),
                            candle_index=candle_idx,
                            timestamp=pd.Timestamp(c_timestamp),
                            displacement_pct=displacement,
                        )
                    )

    return breaks
