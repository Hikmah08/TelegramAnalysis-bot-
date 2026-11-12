from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StrategyConfig:
    """Configurable parameters for ICT/SMC strategy logic."""

    # Fractal Swing Detection
    swing_left_bars: int = 2   # Default 5-bar fractal (2 left, center, 2 right)
    swing_right_bars: int = 2

    # MSS / BOS Validation
    mss_confirmation_mode: Literal["body_close", "wick"] = "body_close"
    min_displacement_pct: float = 0.0  # Optional minimum displacement past swing level (0.0 = any body close)

    # Liquidity Sweep
    sweep_confirmation_mode: Literal["wick_only", "any"] = "wick_only"  # Wick past, body closes inside

    # Fair Value Gap (FVG)
    min_fvg_size_pct: float = 0.0  # Minimum gap size as percentage of price
    fvg_entry_trigger: Literal["touch", "consequent_encroachment"] = "touch"  # touch or 50% CE

    # Invalidation & Confirmation
    max_setup_candles: int = 50  # Maximum candles allowed from sweep to entry retracement
    require_ltf_confirmation: bool = True  # Require 5M confirmation before trade can be taken
