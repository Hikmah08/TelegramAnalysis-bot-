from typing import List, Dict, Any, Optional
import math
from .mtf_models import MultiTimeframeSetup
from .models import Direction


class SetupRanker:
    """
    Ranks qualified ICT/SMC trade opportunities and filters duplicates.
    Prioritizes confluence, risk-to-reward ratio, and clarity of market structure.
    """

    @staticmethod
    def calculate_confluence_score(setup: MultiTimeframeSetup) -> float:
        """
        Calculates an objective confluence score based on structural factors:
        - 4H Bias confirmation: +30 pts
        - 1H Area of Interest alignment: +25 pts
        - 15M Clean Sweep & Body-Close MSS: +20 pts
        - 5M Confirmation present: +15 pts
        - 1M Refinement present: +10 pts
        - Target R:R Bonus: +5 pts per R beyond 4:1
        """
        score = 0.0

        # Higher Timeframe Bias
        if setup.bias_4h and setup.bias_4h.direction.value in ("bullish", "bearish"):
            score += 30.0

        # Area of Interest
        if setup.aoi_1h:
            score += 25.0

        # Setup 15M
        if setup.setup_15m and setup.setup_15m.is_valid:
            score += 20.0

        # 5M Confirmation
        if setup.confirmation_5m and setup.confirmation_5m.is_valid:
            score += 15.0

        # 1M Refinement
        if setup.refinement_1m and setup.refinement_1m.is_valid:
            score += 10.0

        # Risk-to-Reward calculation
        entry = setup.entry_price
        sl = setup.stop_loss
        risk_dist = abs(entry - sl)
        if risk_dist > 0:
            tp_dist = risk_dist * 4.0
            rr = tp_dist / risk_dist
            if rr >= 4.0:
                score += (rr - 4.0) * 5.0

        return score

    @classmethod
    def rank_and_deduplicate(
        cls,
        setups: List[MultiTimeframeSetup],
        max_results: int = 2,
    ) -> List[MultiTimeframeSetup]:
        """
        Deduplicates nearly identical setups (same direction and entry within 0.1%),
        ranks by confluence score, and returns the top `max_results` (default 2).
        """
        if not setups:
            return []

        # Deduplicate
        unique_setups: List[MultiTimeframeSetup] = []
        for s in setups:
            is_dup = False
            for u in unique_setups:
                if s.direction == u.direction:
                    # If entry price is within 0.1% of another setup, treat as duplicate
                    price_diff_pct = abs(s.entry_price - u.entry_price) / max(s.entry_price, 1e-6)
                    if price_diff_pct < 0.001:
                        is_dup = True
                        break
            if not is_dup:
                unique_setups.append(s)

        # Sort descending by confluence score
        sorted_setups = sorted(
            unique_setups,
            key=lambda s: cls.calculate_confluence_score(s),
            reverse=True,
        )

        return sorted_setups[:max_results]
