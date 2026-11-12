from typing import List, Optional
import pandas as pd
from .models import (
    Direction,
    SetupStage,
    SetupCandidate,
    SwingPoint,
    LiquiditySweep,
    MarketStructureBreak,
    FairValueGap,
)
from .config import StrategyConfig
from .swings import find_confirmed_swings
from .sweeps import check_candle_for_sweeps
from .structure import check_candle_for_structure_break
from .fvg import check_candle_for_fvg, is_fvg_retraced


class SequenceTracker:
    """
    State machine that processes completed candles chronologically and enforces:
    
    1. Confirmed Liquidity Level exists (fractal swing).
    2. Liquidity Sweep occurs (wick beyond, body closes inside).
    3. Valid MSS/BOS occurs AFTER the sweep.
    4. Eligible FVG forms AFTER the MSS.
    5. Price retraces into the FVG entry area.
    6. System marks the setup as a candidate for review.

    Invalid event ordering is strictly rejected.
    """

    def __init__(self, timeframe: str = "15m", config: StrategyConfig = StrategyConfig()):
        self.timeframe = timeframe
        self.config = config

    def analyze(self, df: pd.DataFrame) -> List[SetupCandidate]:
        """
        Runs full sequence analysis over a chronological DataFrame of closed candles.
        Returns all completed or active setup candidates.
        """
        if df.empty or len(df) < (self.config.swing_left_bars + self.config.swing_right_bars + 3):
            return []

        # 1. Pre-calculate all confirmed swings (each swing has confirmed_at_index recorded)
        all_swings = find_confirmed_swings(df, self.config)

        candidates: List[SetupCandidate] = []
        active_candidates: List[SetupCandidate] = []

        # Chronologically step candle by candle
        for idx in range(len(df)):
            candle = df.iloc[idx]
            candle_time = pd.Timestamp(candle["timestamp"])

            # Swings available/confirmed strictly on or before this candle index
            confirmed_swings = [s for s in all_swings if s.confirmed_at_index <= idx]

            # -------------------------------------------------------------
            # A. Process Existing Active Candidates
            # -------------------------------------------------------------
            remaining_active: List[SetupCandidate] = []
            for setup in active_candidates:
                # Check maximum candle expiry
                if setup.sweep and (idx - setup.sweep.candle_index) > self.config.max_setup_candles:
                    setup.stage = SetupStage.INVALIDATED
                    setup.audit_notes.append(
                        f"Candle {idx} ({candle_time}): Expired - exceeded max setup candles ({self.config.max_setup_candles})."
                    )
                    candidates.append(setup)
                    continue

                # Invalidation check: price breaches the sweep extreme wick before entry
                if setup.direction == Direction.BULLISH:
                    if float(candle["low"]) < setup.sweep.penetration_price:
                        setup.stage = SetupStage.INVALIDATED
                        setup.audit_notes.append(
                            f"Candle {idx} ({candle_time}): Invalidated - low ({candle['low']:.2f}) broke sweep extreme ({setup.sweep.penetration_price:.2f})."
                        )
                        candidates.append(setup)
                        continue
                elif setup.direction == Direction.BEARISH:
                    if float(candle["high"]) > setup.sweep.penetration_price:
                        setup.stage = SetupStage.INVALIDATED
                        setup.audit_notes.append(
                            f"Candle {idx} ({candle_time}): Invalidated - high ({candle['high']:.2f}) broke sweep extreme ({setup.sweep.penetration_price:.2f})."
                        )
                        candidates.append(setup)
                        continue

                # Stage 1: Waiting for MSS after Sweep
                if setup.stage == SetupStage.SWEEP_CONFIRMED:
                    # Anchor MSS to relevant recent swings formed prior to or during the sweep wave
                    # (Avoid matching against distant, unrelated old peaks)
                    recent_swings = [s for s in confirmed_swings if s.candle_index >= (setup.sweep.candle_index - 30)]
                    breaks = check_candle_for_structure_break(idx, candle, recent_swings, self.config)
                    matching_break = next((b for b in breaks if b.direction == setup.direction), None)
                    if matching_break:
                        # Enforce chronological ordering: MSS must occur on or after sweep candle
                        if matching_break.candle_index >= setup.sweep.candle_index:
                            setup.structure_break = matching_break
                            setup.stage = SetupStage.MSS_CONFIRMED
                            setup.audit_notes.append(
                                f"Candle {idx} ({candle_time}): Confirmed {setup.direction.value} MSS past level {matching_break.broken_swing.price:.2f}."
                            )
                        else:
                            setup.audit_notes.append(
                                f"Candle {idx}: Rejected out-of-order MSS ({matching_break.candle_index} < {setup.sweep.candle_index})."
                            )

                # Stage 2: Waiting for FVG after MSS
                elif setup.stage == SetupStage.MSS_CONFIRMED:
                    fvg = check_candle_for_fvg(idx, df, self.config)
                    if fvg and fvg.direction == setup.direction:
                        # Enforce: FVG must complete after or concurrently with the MSS displacement
                        if fvg.candle_index >= setup.structure_break.candle_index:
                            setup.fvg = fvg
                            setup.stage = SetupStage.FVG_FORMED
                            setup.audit_notes.append(
                                f"Candle {idx} ({candle_time}): Formed {setup.direction.value} FVG [{fvg.bottom_price:.2f} - {fvg.top_price:.2f}]."
                            )
                        else:
                            setup.audit_notes.append(
                                f"Candle {idx}: Rejected out-of-order FVG before MSS."
                            )

                # Stage 3: Waiting for Retracement into FVG
                elif setup.stage == SetupStage.FVG_FORMED:
                    # Retracement candle must be strictly after the FVG completion candle
                    if idx > setup.fvg.candle_index:
                        if is_fvg_retraced(candle, setup.fvg, self.config.fvg_entry_trigger):
                            setup.stage = SetupStage.ENTRY_RETRACED
                            setup.entry_timestamp = candle_time
                            setup.entry_price = setup.fvg.consequent_encroachment if self.config.fvg_entry_trigger == "consequent_encroachment" else (setup.fvg.top_price if setup.direction == Direction.BULLISH else setup.fvg.bottom_price)
                            setup.stop_loss = setup.sweep.penetration_price
                            setup.is_valid = True
                            setup.audit_notes.append(
                                f"Candle {idx} ({candle_time}): Retraced into FVG. Setup completed and marked as CANDIDATE FOR REVIEW."
                            )
                            candidates.append(setup)
                            continue

                remaining_active.append(setup)

            active_candidates = remaining_active

            # -------------------------------------------------------------
            # B. Check for New Liquidity Sweeps on Current Candle
            # -------------------------------------------------------------
            new_sweeps = check_candle_for_sweeps(idx, candle, confirmed_swings, self.timeframe, self.config)
            for sweep in new_sweeps:
                new_candidate = SetupCandidate(
                    direction=sweep.direction,
                    timeframe=self.timeframe,
                    stage=SetupStage.SWEEP_CONFIRMED,
                    sweep=sweep,
                    audit_notes=[
                        f"Candle {idx} ({candle_time}): Confirmed {sweep.direction.value} liquidity sweep of {sweep.target_swing.swing_type.value} level {sweep.swept_level:.2f} with wick {sweep.penetration_price:.2f}."
                    ],
                )
                active_candidates.append(new_candidate)

        # Include any still-active candidates
        candidates.extend(active_candidates)
        return candidates
