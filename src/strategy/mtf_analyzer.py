from typing import List, Optional
import pandas as pd
from .models import Direction, SwingType
from .config import StrategyConfig
from .swings import find_confirmed_swings
from .structure import check_candle_for_structure_break
from .fvg import check_candle_for_fvg
from .sweeps import check_candle_for_sweeps
from .mtf_models import BiasState, HigherTimeframeBias, AreaOfInterest


class MultiTimeframeAnalyzer:
    """
    Evaluates Higher-Timeframe context (4H Directional Bias & 1H AOI).
    Prevents lower timeframes from overriding higher-timeframe context.
    """

    def __init__(self, config: StrategyConfig = StrategyConfig()):
        self.config = config

    def determine_4h_bias(self, df_4h: pd.DataFrame) -> HigherTimeframeBias:
        """
        Determines 4H Directional Bias based on confirmed swing structure and MSS:
        - If the latest confirmed MSS was Bullish and latest swing low holds -> BULLISH
        - If the latest confirmed MSS was Bearish and latest swing high holds -> BEARISH
        - Fallback: evaluates latest swing highs & lows (Higher High/Higher Low vs Lower High/Lower Low)
        """
        if df_4h.empty or len(df_4h) < 10:
            return HigherTimeframeBias(
                direction=BiasState.NEUTRAL,
                timestamp=pd.Timestamp.now(tz="UTC"),
                reason="Insufficient 4H candle data",
            )

        swings = find_confirmed_swings(df_4h, self.config)
        if not swings:
            return HigherTimeframeBias(
                direction=BiasState.NEUTRAL,
                timestamp=pd.Timestamp(df_4h.iloc[-1]["timestamp"]),
                reason="No confirmed 4H swings identified",
            )

        # Look for most recent structure break (MSS/BOS) across 4H candles
        latest_break = None
        for idx in range(len(df_4h)):
            candle = df_4h.iloc[idx]
            confirmed_swings = [s for s in swings if s.confirmed_at_index <= idx]
            breaks = check_candle_for_structure_break(idx, candle, confirmed_swings, self.config)
            if breaks:
                latest_break = breaks[-1]

        last_candle_time = pd.Timestamp(df_4h.iloc[-1]["timestamp"])

        if latest_break:
            bias_dir = BiasState.BULLISH if latest_break.direction == Direction.BULLISH else BiasState.BEARISH
            return HigherTimeframeBias(
                direction=bias_dir,
                timestamp=last_candle_time,
                reason=f"Confirmed 4H {latest_break.structure_type.value.upper()} ({latest_break.direction.value}) at candle {latest_break.candle_index}",
                last_structure_price=latest_break.break_price,
            )

        # Fallback to swing progression
        high_swings = [s for s in swings if s.swing_type == SwingType.SWING_HIGH]
        low_swings = [s for s in swings if s.swing_type == SwingType.SWING_LOW]

        if len(high_swings) >= 2 and len(low_swings) >= 2:
            hh = high_swings[-1].price > high_swings[-2].price
            hl = low_swings[-1].price > low_swings[-2].price
            lh = high_swings[-1].price < high_swings[-2].price
            ll = low_swings[-1].price < low_swings[-2].price

            if hh and hl:
                return HigherTimeframeBias(
                    direction=BiasState.BULLISH,
                    timestamp=last_candle_time,
                    reason="4H market structure showing consecutive Higher Highs & Higher Lows",
                )
            elif lh and ll:
                return HigherTimeframeBias(
                    direction=BiasState.BEARISH,
                    timestamp=last_candle_time,
                    reason="4H market structure showing consecutive Lower Highs & Lower Lows",
                )

        return HigherTimeframeBias(
            direction=BiasState.NEUTRAL,
            timestamp=last_candle_time,
            reason="4H structure is range-bound / neutral",
        )

    def find_1h_areas_of_interest(
        self,
        df_1h: pd.DataFrame,
        bias_4h: HigherTimeframeBias,
    ) -> List[AreaOfInterest]:
        """
        Identifies active 1H Areas of Interest (AOI) aligned with the 4H Bias:
        - For Bullish Bias: unmitigated 1H Bullish FVGs or recent 1H Swing Low sweeps
        - For Bearish Bias: unmitigated 1H Bearish FVGs or recent 1H Swing High sweeps
        """
        if df_1h.empty or len(df_1h) < 10 or bias_4h.direction == BiasState.NEUTRAL:
            return []

        target_dir = Direction.BULLISH if bias_4h.direction == BiasState.BULLISH else Direction.BEARISH
        aois: List[AreaOfInterest] = []

        # 1. Scan for recent 1H FVGs aligned with bias
        for idx in range(2, len(df_1h)):
            fvg = check_candle_for_fvg(idx, df_1h, self.config)
            if fvg and fvg.direction == target_dir:
                # Check if FVG was mitigated by subsequent candles
                is_mitigated = False
                for future_idx in range(idx + 1, len(df_1h)):
                    future_c = df_1h.iloc[future_idx]
                    if target_dir == Direction.BULLISH and float(future_c["low"]) <= fvg.bottom_price:
                        is_mitigated = True
                        break
                    elif target_dir == Direction.BEARISH and float(future_c["high"]) >= fvg.top_price:
                        is_mitigated = True
                        break

                if not is_mitigated:
                    aois.append(
                        AreaOfInterest(
                            direction=target_dir,
                            aoi_type="1H_FVG",
                            top_price=fvg.top_price,
                            bottom_price=fvg.bottom_price,
                            timestamp=fvg.timestamp,
                            reference_fvg=fvg,
                        )
                    )

        # 2. Scan for recent 1H Liquidity Sweeps aligned with bias
        swings_1h = find_confirmed_swings(df_1h, self.config)
        for idx in range(len(df_1h)):
            c = df_1h.iloc[idx]
            confirmed = [s for s in swings_1h if s.confirmed_at_index <= idx]
            sweeps = check_candle_for_sweeps(idx, c, confirmed, "1h", self.config)
            for s in sweeps:
                if s.direction == target_dir:
                    # AOI zone around the swept level
                    aois.append(
                        AreaOfInterest(
                            direction=target_dir,
                            aoi_type="1H_SWEEP",
                            top_price=max(s.swept_level, s.penetration_price),
                            bottom_price=min(s.swept_level, s.penetration_price),
                            timestamp=s.timestamp,
                            reference_swing=s.target_swing,
                        )
                    )

        return aois
