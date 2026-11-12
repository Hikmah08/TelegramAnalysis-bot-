from dataclasses import dataclass
from enum import Enum
from typing import Optional, List
import pandas as pd


class SwingType(str, Enum):
    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"


class StructureType(str, Enum):
    MSS = "mss"  # Market Structure Shift (change of character)
    BOS = "bos"  # Break of Structure (continuation)


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass(frozen=True)
class SwingPoint:
    """Represents a confirmed swing high or low."""
    swing_type: SwingType
    price: float
    candle_index: int
    timestamp: pd.Timestamp
    confirmed_at_index: int       # The candle index at which this swing is confirmed
    confirmed_at_time: pd.Timestamp  # The close_time of the confirmation candle


@dataclass(frozen=True)
class LiquiditySweep:
    """Represents a validated liquidity sweep."""
    direction: Direction          # Bullish: swept swing low; Bearish: swept swing high
    swept_level: float            # The price of the swing point that was swept
    penetration_price: float      # The extreme wick price beyond the level
    candle_index: int
    timestamp: pd.Timestamp
    timeframe: str
    target_swing: SwingPoint


@dataclass(frozen=True)
class MarketStructureBreak:
    """Represents a confirmed MSS or BOS."""
    structure_type: StructureType
    direction: Direction
    broken_swing: SwingPoint
    break_price: float            # Candle close price that confirmed the break
    candle_index: int
    timestamp: pd.Timestamp
    displacement_pct: float


@dataclass(frozen=True)
class FairValueGap:
    """Represents a 3-candle Fair Value Gap (imbalance)."""
    direction: Direction
    top_price: float
    bottom_price: float
    consequent_encroachment: float  # 50% midpoint of the gap
    candle_index: int             # Index of the 3rd candle that completed the gap
    timestamp: pd.Timestamp       # Timestamp of the 3rd candle


class SetupStage(str, Enum):
    PENDING_SWEEP = "pending_sweep"
    SWEEP_CONFIRMED = "sweep_confirmed"
    MSS_CONFIRMED = "mss_confirmed"
    FVG_FORMED = "fvg_formed"
    ENTRY_RETRACED = "entry_retraced"
    INVALIDATED = "invalidated"


@dataclass
class SetupCandidate:
    """Represents a qualified or disqualified ICT/SMC setup candidate."""
    direction: Direction
    timeframe: str
    stage: SetupStage
    sweep: Optional[LiquiditySweep] = None
    structure_break: Optional[MarketStructureBreak] = None
    fvg: Optional[FairValueGap] = None
    entry_price: Optional[float] = None
    entry_timestamp: Optional[pd.Timestamp] = None
    stop_loss: Optional[float] = None
    is_valid: bool = False
    audit_notes: List[str] = None

    def __post_init__(self):
        if self.audit_notes is None:
            self.audit_notes = []
