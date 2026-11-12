from typing import Optional
import pandas as pd
from .models import FairValueGap, Direction
from .config import StrategyConfig


def check_candle_for_fvg(
    candle_idx: int,
    df: pd.DataFrame,
    config: StrategyConfig = StrategyConfig(),
) -> Optional[FairValueGap]:
    """
    Checks for a 3-candle Fair Value Gap (imbalance) completing at candle_idx.

    Candle 1 = index - 2
    Candle 2 = index - 1 (displacement candle)
    Candle 3 = index     (confirmation candle)

    Bullish FVG:
      Candle 1 High < Candle 3 Low
      Gap = [Candle 1 High, Candle 3 Low]
      Consequent Encroachment (CE) = (Candle 1 High + Candle 3 Low) / 2

    Bearish FVG:
      Candle 1 Low > Candle 3 High
      Gap = [Candle 3 High, Candle 1 Low]
      Consequent Encroachment (CE) = (Candle 3 High + Candle 1 Low) / 2
    """
    if candle_idx < 2:
        return None

    c1 = df.iloc[candle_idx - 2]
    c2 = df.iloc[candle_idx - 1]
    c3 = df.iloc[candle_idx]

    c1_high = float(c1["high"])
    c1_low = float(c1["low"])
    c3_high = float(c3["high"])
    c3_low = float(c3["low"])

    # Bullish FVG
    if c3_low > c1_high:
        gap_size = c3_low - c1_high
        gap_pct = gap_size / c1_high
        if gap_pct >= config.min_fvg_size_pct:
            ce = (c3_low + c1_high) / 2.0
            return FairValueGap(
                direction=Direction.BULLISH,
                top_price=c3_low,
                bottom_price=c1_high,
                consequent_encroachment=ce,
                candle_index=candle_idx,
                timestamp=pd.Timestamp(c3["timestamp"]),
            )

    # Bearish FVG
    if c1_low > c3_high:
        gap_size = c1_low - c3_high
        gap_pct = gap_size / c1_low
        if gap_pct >= config.min_fvg_size_pct:
            ce = (c1_low + c3_high) / 2.0
            return FairValueGap(
                direction=Direction.BEARISH,
                top_price=c1_low,
                bottom_price=c3_high,
                consequent_encroachment=ce,
                candle_index=candle_idx,
                timestamp=pd.Timestamp(c3["timestamp"]),
            )

    return None


def is_fvg_retraced(
    candle: pd.Series,
    fvg: FairValueGap,
    entry_trigger: str = "touch",
) -> bool:
    """
    Checks if price in `candle` has retraced into the FVG.

    For Bullish FVG:
      - 'touch': low <= fvg.top_price (enters the gap)
      - 'consequent_encroachment': low <= fvg.consequent_encroachment

    For Bearish FVG:
      - 'touch': high >= fvg.bottom_price (enters the gap)
      - 'consequent_encroachment': high >= fvg.consequent_encroachment
    """
    c_low = float(candle["low"])
    c_high = float(candle["high"])

    if fvg.direction == Direction.BULLISH:
        if entry_trigger == "consequent_encroachment":
            return c_low <= fvg.consequent_encroachment
        return c_low <= fvg.top_price

    elif fvg.direction == Direction.BEARISH:
        if entry_trigger == "consequent_encroachment":
            return c_high >= fvg.consequent_encroachment
        return c_high >= fvg.bottom_price

    return False
