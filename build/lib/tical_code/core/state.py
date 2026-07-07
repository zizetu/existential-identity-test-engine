"""Core cognitive state representations including beliefs, hypotheses, and mood vectors."""

from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Deque
from collections import deque
import uuid

class BeliefStrength(Enum):
    """Confidence levels for belief propositions."""
    SPECULATIVE = 0.2
    TENTATIVE = 0.4
    MODERATE = 0.6
    STRONG = 0.8
    CONFIRMED = 1.0

@dataclass
class Hypothesis:
    """Testable proposition with supporting evidence."""
    claim: str
    confidence: float = 0.5  # 0.0-1.0
    evidence: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    updated_at: float = field(default_factory=lambda: datetime.now().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Hypothesis':
        """Create from dict."""
        return cls(**data)

@dataclass
class Belief:
    """Established belief with relationships to other beliefs."""
    claim: str
    strength: BeliefStrength = BeliefStrength.TENTATIVE
    sources: List[str] = field(default_factory=list)
    supports: List[str] = field(default_factory=list)
    contradicts: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to serializable dict."""
        data = asdict(self)
        data['strength'] = self.strength.name
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Belief':
        """Create from dict."""
        data['strength'] = BeliefStrength[data['strength']]
        return cls(**data)

class BeliefGraph:
    """Graph of interconnected beliefs with relationship types."""
    
    def __init__(self):
        self._beliefs: Dict[str, Belief] = {}
        self._edges: Dict[str, Dict[str, str]] = {}  # {src: {dst: edge_type}}

    def add_belief(self, belief: Belief) -> None:
        """Add or update a belief in the graph."""
        self._beliefs[belief.claim] = belief

    def add_edge(self, src: str, dst: str, edge_type: str) -> None:
        """Add relationship between beliefs (supports/contradicts/causes/implies)."""
        if src not in self._beliefs or dst not in self._beliefs:
            raise ValueError("Both source and destination must exist in graph")
        if edge_type not in {'supports', 'contradicts', 'causes', 'implies'}:
            raise ValueError("Invalid edge type")
        
        if src not in self._edges:
            self._edges[src] = {}
        self._edges[src][dst] = edge_type

    def get_beliefs_by_strength(self, min_strength: BeliefStrength) -> List[Belief]:
        """Get beliefs meeting minimum confidence threshold."""
        return [b for b in self._beliefs.values() if b.strength.value >= min_strength.value]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize graph to dict."""
        return {
            'beliefs': {k: v.to_dict() for k, v in self._beliefs.items()},
            'edges': self._edges
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BeliefGraph':
        """Deserialize from dict."""
        graph = cls()
        graph._beliefs = {k: Belief.from_dict(v) for k, v in data['beliefs'].items()}
        graph._edges = data.get('edges', {})
        return graph

@dataclass
class MoodVector:
    """Agent's emotional state vector with decay mechanism."""
    caution: float = 0.5  # 0.0-1.0
    confidence: float = 0.5  # 0.0-1.0
    curiosity: float = 0.5  # 0.0-1.0

    def decay(self, rate: float = 0.1) -> None:
        """Gradually return toward neutral baseline."""
        self.caution = self._decay_value(self.caution, rate)
        self.confidence = self._decay_value(self.confidence, rate)
        self.curiosity = self._decay_value(self.curiosity, rate)

    def _decay_value(self, value: float, rate: float) -> float:
        """Helper for exponential decay toward 0.5."""
        return 0.5 + (value - 0.5) * (1 - rate)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MoodVector':
        """Create from dict."""
        return cls(**data)

@dataclass
class Decision:
    """Record of an action decision with alternatives considered."""
    action: str
    rationale: str
    alternatives: List[str] = field(default_factory=list)
    hypothesis_refs: List[str] = field(default_factory=list)
    confidence: float = 0.5  # 0.0-1.0
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Decision':
        """Create from dict."""
        return cls(**data)
