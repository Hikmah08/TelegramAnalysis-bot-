from dataclasses import dataclass
from enum import Enum
from typing import Optional, List
import pandas as pd
from .models import Direction, SwingPoint, FairValueGap, SetupCandidate


class BiasState(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class HigherTimeframeBias:
    """Represents the 4H directional bias."""
    direction: BiasState
    timestamp: pd.Timestamp
    reason: str
    last_structure_price: Optional[float] = None


@dataclass(frozen=True)
class AreaOfInterest:
    """Represents a 1H Area of Interest (AOI)."""
    direction: Direction
    aoi_type: str  # "1H_FVG" or "1H_SWEEP"
    top_price: float
    bottom_price: float
    timestamp: pd.Timestamp
    reference_fvg: Optional[FairValueGap] = None
    reference_swing: Optional[SwingPoint] = None

    def contains_price(self, price: float) -> bool:
        return self.bottom_price <= price <= self.top_price


@dataclass
class MultiTimeframeSetup:
    """
    Complete qualified setup passing all 5 timeframe hierarchy stages:
    4H Bias -> 1H AOI -> 15M Setup -> 5M Confirmation -> 1M Refinement
    """
    direction: Direction
    bias_4h: HigherTimeframeBias
    aoi_1h: AreaOfInterest
    setup_15m: SetupCandidate
    confirmation_5m: Optional[SetupCandidate] = None
    refinement_1m: Optional[SetupCandidate] = None
    entry_price: float = 0.0
    stop_loss: float = 0.0
    timestamp: Optional[pd.Timestamp] = None
    audit_trail: List[str] = None

    def __post_init__(self):
        if self.audit_trail is None:
            self.audit_trail = []
