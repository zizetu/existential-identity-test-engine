"""Signal - the atomic unit of AI cognition

Every cognitive unit - whether from human input, self-reasoning, or historical experience -
is not "present" or "absent", but diffuses in cognitive space with a 0-1 confidence level.

Signal is not a data wrapper, it is the fundamental physical unit of cognition.
Data without confidence is not a Signal, not cognition, and does not exist in the system.
"""

import time
import math
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List


class SignalSource(Enum):
    """Signal source - determines initial credibility prior"""
    HUMAN_INPUT = "human_input"       # Human direct input, medium initial credibility (contains emotional noise)
    AI_REASONING = "ai_reasoning"     # AI self-reasoning, low initial credibility (potential hallucination)
    MEMORY = "memory"                 # Memory retrieval, initial credibility depends on source
    VERIFICATION = "verification"     # Verified signal, high initial credibility
    ESSENCE = "essence"               # Essence layer signal, highest initial credibility
    SENSOR = "sensor"                 # External sensor/API, medium initial credibility
    CONSTITUTION = "constitution"     # Constitution rule, very high initial credibility but revisable


class SignalQuality(Enum):
    """Signal quality level - auto-mapped from confidence"""
    UNRELIABLE = "unreliable"   # < 0.3, not actionable
    WEAK = "weak"               # 0.3-0.5, reference only
    MODERATE = "moderate"       # 0.5-0.7, cautious action
    STRONG = "strong"           # 0.7-0.9, actionable
    AXIOM = "axiom"             # > 0.9, certain knowledge


# Source prior credibility - initial credibility baseline for different sources
SOURCE_PRIOR = {
    SignalSource.HUMAN_INPUT: 0.5,    # Human input contains emotional noise
    SignalSource.AI_REASONING: 0.3,   # AI reasoning may hallucinate
    SignalSource.MEMORY: 0.4,         # Memory may be outdated/corrupted by compression
    SignalSource.VERIFICATION: 0.8,   # Verified
    SignalSource.ESSENCE: 0.9,        # Essence layer
    SignalSource.SENSOR: 0.5,         # External data
    SignalSource.CONSTITUTION: 0.95,  # Constitution but revisable
}


@dataclass
class Signal:
    """Cognitive atom - the only form of data existence in the system

    Core attributes:
    - payload: actual content
    - confidence: calibrated confidence 0.0-1.0
    - source: signal source
    - verified_count: times verified, determines half-life
    - half_life: decay coefficient, higher = slower decay

    Physical constraints:
    - Signals with confidence < ACTION_THRESHOLD cannot trigger actions
    - Signals with verified_count = 0 have the shortest half-life
    - Each successful verification extends half-life and boosts confidence
    """
    payload: Any
    source: SignalSource
    confidence: float = -1.0
    verified_count: int = 0
    half_life: float = 1.0           # base half-life (days)
    created_at: float = field(default_factory=time.time)
    last_verified: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Raw data for signal calibration (calibration output from Layer 1 / Layer 2)
    calibration_trace: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Initialize: if confidence not specified (<0), use source prior"""
        if self.confidence < 0.0:
            self.confidence = SOURCE_PRIOR.get(self.source, 0.3)

    @property
    def quality(self) -> SignalQuality:
        """Confidence → quality level auto-mapping"""
        if self.confidence < 0.3:
            return SignalQuality.UNRELIABLE
        elif self.confidence < 0.5:
            return SignalQuality.WEAK
        elif self.confidence < 0.7:
            return SignalQuality.MODERATE
        elif self.confidence < 0.9:
            return SignalQuality.STRONG
        else:
            return SignalQuality.AXIOM

    @property
    def can_act(self) -> bool:
        """Whether action can be triggered - signals below threshold cannot act"""
        return self.confidence >= 0.5  # MODERATE and above are actionable

    def current_confidence(self, now: Optional[float] = None) -> float:
        """Current confidence after accounting for time decay

        Half-life mechanism: unverified signals decay naturally
        More verified_count → longer half_life → slower decay
        """
        if now is None:
            now = time.time()
        elapsed_days = (now - self.created_at) / 86400.0
        decay = math.exp(-0.693 * elapsed_days / self.half_life)
        return self.confidence * decay

    def verify(self, success: bool = True, boost: float = 0.05) -> 'Signal':
        """Verify signal - successful verification boosts confidence and half-life

        Args:
            success: whether verification passed
            boost: confidence boost when verification passes

        Returns:
            new Signal (immutable design)
        """
        new_confidence = self.confidence
        new_half_life = self.half_life
        new_verified = self.verified_count

        if success:
            new_confidence = min(1.0, self.confidence + boost)
            new_verified = self.verified_count + 1
            # Each successful verification extends half-life by 50%, capped at 365 days
            new_half_life = min(365.0, self.half_life * 1.5)
        else:
            # Verification failed: confidence drops, half-life shortens
            new_confidence = max(0.0, self.confidence - boost * 2)
            new_half_life = max(0.1, self.half_life * 0.5)

        return Signal(
            payload=self.payload,
            source=self.source,
            confidence=new_confidence,
            verified_count=new_verified,
            half_life=new_half_life,
            created_at=self.created_at,
            last_verified=time.time() if success else self.last_verified,
            metadata=self.metadata.copy(),
            calibration_trace=self.calibration_trace.copy(),
        )

    def is_expired(self, now: Optional[float] = None) -> bool:
        """Whether signal has decayed to meaninglessness"""
        return self.current_confidence(now) < 0.05

    def to_dict(self) -> Dict[str, Any]:
        """Serialize"""
        return {
            "payload": self.payload,
            "source": self.source.value,
            "confidence": self.confidence,
            "verified_count": self.verified_count,
            "half_life": self.half_life,
            "created_at": self.created_at,
            "last_verified": self.last_verified,
            "quality": self.quality.value,
            "metadata": self.metadata,
            "calibration_trace": self.calibration_trace,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Signal':
        """Deserialize"""
        return cls(
            payload=data["payload"],
            source=SignalSource(data["source"]),
            confidence=data.get("confidence", 0.0),
            verified_count=data.get("verified_count", 0),
            half_life=data.get("half_life", 1.0),
            created_at=data.get("created_at", time.time()),
            last_verified=data.get("last_verified"),
            metadata=data.get("metadata", {}),
            calibration_trace=data.get("calibration_trace", {}),
        )
