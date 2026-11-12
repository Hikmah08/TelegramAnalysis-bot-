from typing import Dict, List, Optional
import pandas as pd
from .models import Direction, SetupStage, SetupCandidate
from .config import StrategyConfig
from .sequence_tracker import SequenceTracker
from .mtf_models import (
    BiasState,
    HigherTimeframeBias,
    AreaOfInterest,
    MultiTimeframeSetup,
)
from .mtf_analyzer import MultiTimeframeAnalyzer


class MultiTimeframeOrchestrator:
    """
    Coordinates the full 5-timeframe hierarchy:
    4H directional bias -> 1H area of interest -> 15M setup formation -> 5M confirmation -> 1M entry refinement.

    Rule: Lower timeframes MUST NOT override higher-timeframe context.
    """

    def __init__(self, config: StrategyConfig = StrategyConfig()):
        self.config = config
        self.analyzer = MultiTimeframeAnalyzer(config)

    def scan_market(
        self,
        timeframe_dfs: Dict[str, pd.DataFrame],
    ) -> List[MultiTimeframeSetup]:
        """
        Processes dictionary of DataFrames: {"4h": df, "1h": df, "15m": df, "5m": df, "1m": df}.
        Enforces top-down filtering and produces fully qualified candidate setups.
        """
        required_tfs = ["4h", "1h", "15m", "5m", "1m"]
        for tf in required_tfs:
            if tf not in timeframe_dfs or timeframe_dfs[tf].empty:
                return []

        # Step 1: 4H Directional Bias
        df_4h = timeframe_dfs["4h"]
        bias_4h = self.analyzer.determine_4h_bias(df_4h)
        if bias_4h.direction == BiasState.NEUTRAL:
            return []  # No clear higher-timeframe direction, stop evaluation

        target_direction = Direction.BULLISH if bias_4h.direction == BiasState.BULLISH else Direction.BEARISH

        # Step 2: 1H Area of Interest (AOI)
        df_1h = timeframe_dfs["1h"]
        aois_1h = self.analyzer.find_1h_areas_of_interest(df_1h, bias_4h)
        if not aois_1h:
            return []  # No active AOI on 1H to anchor setups

        # Step 3: 15M Setup Formation (Sweep -> MSS -> FVG -> Retracement)
        df_15m = timeframe_dfs["15m"]
        tracker_15m = SequenceTracker(timeframe="15m", config=self.config)
        setups_15m = tracker_15m.analyze(df_15m)

        # Filter 15M setups: Must match 4H bias and touch/intersect an active 1H AOI
        qualified_mtf_setups: List[MultiTimeframeSetup] = []

        for setup_15 in setups_15m:
            if not setup_15.is_valid or setup_15.direction != target_direction:
                continue  # Lower timeframe counter-trend setup rejected!

            # Check if 15M sweep or entry falls inside any 1H AOI
            matched_aoi: Optional[AreaOfInterest] = None
            for aoi in aois_1h:
                sweep_in_aoi = aoi.contains_price(setup_15.sweep.swept_level) or aoi.contains_price(setup_15.sweep.penetration_price)
                entry_in_aoi = setup_15.entry_price and aoi.contains_price(setup_15.entry_price)
                if sweep_in_aoi or entry_in_aoi:
                    matched_aoi = aoi
                    break

            if not matched_aoi:
                continue  # Setup occurred outside of 1H Area of Interest

            # Step 4: 5M Confirmation
            df_5m = timeframe_dfs["5m"]
            tracker_5m = SequenceTracker(timeframe="5m", config=self.config)
            # Scan 5M candles on or after the 15M sweep timestamp
            df_5m_window = df_5m[df_5m["timestamp"] >= setup_15.sweep.timestamp]
            setups_5m = tracker_5m.analyze(df_5m_window)
            conf_5m = next((s for s in setups_5m if s.direction == target_direction and s.is_valid), None)

            # Strict Confirmation requirement: If configured, reject trades lacking 5M confirmation!
            if self.config.require_ltf_confirmation and not conf_5m:
                continue  # Disqualified: 5M confirmation not present

            # Step 5: 1M Entry Refinement
            df_1m = timeframe_dfs["1m"]
            tracker_1m = SequenceTracker(timeframe="1m", config=self.config)
            ref_1m: Optional[SetupCandidate] = None
            if conf_5m and conf_5m.fvg:
                df_1m_window = df_1m[df_1m["timestamp"] >= conf_5m.fvg.timestamp]
                setups_1m = tracker_1m.analyze(df_1m_window)
                ref_1m = next((s for s in setups_1m if s.direction == target_direction and s.is_valid), None)

            # Determine refined entry and stop loss
            final_entry = ref_1m.entry_price if ref_1m else (conf_5m.entry_price if conf_5m else setup_15.entry_price)
            final_stop = ref_1m.stop_loss if ref_1m else (conf_5m.stop_loss if conf_5m else setup_15.stop_loss)

            audit = [
                f"1. 4H Bias: {bias_4h.direction.value.upper()} ({bias_4h.reason})",
                f"2. 1H AOI: {matched_aoi.aoi_type} [{matched_aoi.bottom_price:.2f} - {matched_aoi.top_price:.2f}]",
                f"3. 15M Setup: Confirmed at {setup_15.entry_timestamp} (Sweep {setup_15.sweep.swept_level:.2f}, MSS past {setup_15.structure_break.broken_swing.price:.2f})",
                f"4. 5M Confirmation: {'CONFIRMED at ' + str(conf_5m.entry_timestamp) if conf_5m else 'PENDING'}",
                f"5. 1M Refinement: {'REFINED at ' + str(ref_1m.entry_timestamp) if ref_1m else 'USING 15M/5M LEVEL'}",
            ]

            mtf_setup = MultiTimeframeSetup(
                direction=target_direction,
                bias_4h=bias_4h,
                aoi_1h=matched_aoi,
                setup_15m=setup_15,
                confirmation_5m=conf_5m,
                refinement_1m=ref_1m,
                entry_price=final_entry,
                stop_loss=final_stop,
                timestamp=ref_1m.entry_timestamp if ref_1m else (conf_5m.entry_timestamp if conf_5m else setup_15.entry_timestamp),
                audit_trail=audit,
            )
            qualified_mtf_setups.append(mtf_setup)

        return qualified_mtf_setups
