"""Distributed cognitive workspace with CRDT-based state synchronization."""

from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set
from collections import defaultdict, deque
import json
import uuid
from .state import BeliefGraph, BeliefStrength, Hypothesis, MoodVector, Decision, Belief

@dataclass
class WorkspaceEntry:
    """Immutable workspace entry with vector clock for conflict resolution."""
    id: str  # uuid4
    topic: str
    payload: Any
    timestamp: float  # creation time
    ttl: float  # seconds until expiration
    version: int = 1
    vector_clock: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to serializable dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkspaceEntry':
        """Create from dict."""
        return cls(**data)

@dataclass
class CognitiveState:
    """Snapshot of agent's current cognitive state."""
    goal: str = ""
    hypotheses: Dict[str, Hypothesis] = field(default_factory=dict)
    beliefs: BeliefGraph = field(default_factory=BeliefGraph)
    mood: MoodVector = field(default_factory=MoodVector)
    active_context: Dict[str, Any] = field(default_factory=dict)
    uncertainty: List[str] = field(default_factory=list)
    decision_trace: Deque[Decision] = field(default_factory=lambda: deque(maxlen=100))
    last_updated: float = field(default_factory=lambda: datetime.now().timestamp())
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert to serializable dict."""
        data = asdict(self)
        data['beliefs'] = self.beliefs.to_dict()
        data['mood'] = self.mood.to_dict()
        data['hypotheses'] = {k: v.to_dict() for k, v in self.hypotheses.items()}
        data['decision_trace'] = [d.to_dict() for d in self.decision_trace]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CognitiveState':
        """Create from dict."""
        state = cls()
        state.goal = data.get('goal', "")
        state.hypotheses = {k: Hypothesis.from_dict(v) for k, v in data.get('hypotheses', {}).items()}
        state.beliefs = BeliefGraph.from_dict(data.get('beliefs', {}))
        state.mood = MoodVector.from_dict(data.get('mood', {}))
        state.active_context = data.get('active_context', {})
        state.uncertainty = data.get('uncertainty', [])
        state.decision_trace = deque(
            [Decision.from_dict(d) for d in data.get('decision_trace', [])],
            maxlen=100
        )
        state.last_updated = data.get('last_updated', datetime.now().timestamp())
        state.version = data.get('version', 1)
        return state

class Workspace:
    """Distributed cognitive workspace with persistence and pub/sub."""

    _instance: Optional['Workspace'] = None

    def __init__(self, node_id: str, persist_path: Path, enabled: bool = True):
        self.node_id = node_id
        self.persist_path = persist_path
        self.enabled = enabled
        self._entries: Dict[str, WorkspaceEntry] = {}
        self._state = CognitiveState()
        self._subscribers: Dict[str, Set[Callable]] = defaultdict(set)
        self._vector_clock: Dict[str, int] = {node_id: 0}
        self._load()

    @classmethod
    def get_workspace(cls, node_id: str, persist_path: Path) -> 'Workspace':
        """Singleton accessor."""
        if cls._instance is None:
            cls._instance = cls(node_id, persist_path)
        return cls._instance

    def write(self, topic: str, payload: Any, ttl: float = 3600) -> None:
        """Create new workspace entry with LWW-CRDT semantics."""
        if not self.enabled:
            return

        entry = WorkspaceEntry(
            id=str(uuid.uuid4()),
            topic=topic,
            payload=payload,
            timestamp=datetime.now().timestamp(),
            ttl=ttl,
            vector_clock=self._vector_clock.copy()
        )
        self._entries[entry.id] = entry
        self._bump_clock()
        self._notify(topic, entry)
        self._persist()

    def read(self, topic: Optional[str] = None) -> Any:
        """Read entries by topic or return full cognitive state."""
        if not self.enabled:
            return None if topic else CognitiveState()

        if topic is None:
            return self._state

        return [e.payload for e in self._entries.values() if e.topic == topic]

    def subscribe(self, topic: str, callback: Callable) -> None:
        """Register callback for topic updates (including wildcard '*')."""
        self._subscribers[topic].add(callback)

    def update_state(self, **kwargs) -> None:
        """Update cognitive state fields."""
        if not self.enabled:
            return

        for k, v in kwargs.items():
            if not hasattr(self._state, k):
                continue
            # Type guard for list fields — reject non-list values
            if k in ("uncertainty",) and not isinstance(v, list):
                continue
            setattr(self._state, k, v)
        self._state.last_updated = datetime.now().timestamp()
        self._persist()

    def get_state(self) -> CognitiveState:
        """Get current cognitive state snapshot."""
        return self._state

    def get_prompt_summary(self) -> str:
        """Generate formatted summary for LLM system prompt."""
        if not self.enabled:
            return "Cognitive system disabled"

        lines = [
            f"Current Goal: {self._state.goal}",
            f"Mood: Caution={self._state.mood.caution:.1f}, Confidence={self._state.mood.confidence:.1f}, Curiosity={self._state.mood.curiosity:.1f}",
            f"Active Hypotheses: {len(self._state.hypotheses)}",
            f"Core Beliefs: {len(self._state.beliefs.get_beliefs_by_strength(BeliefStrength.MODERATE))}",
            f"Recent Decisions: {len(self._state.decision_trace)}",
            f"Uncertainties: {len(self._state.uncertainty)}"
        ]
        return "\n".join(lines)

    def _persist(self) -> None:
        """Save state to disk."""
        try:
            data = {
                'state': self._state.to_dict(),
                'entries': [e.to_dict() for e in self._entries.values()],
                'vector_clock': self._vector_clock
            }
            with open(self.persist_path, 'w') as f:
                json.dump(data, f)
        except (IOError, json.JSONDecodeError) as e:
            print(f"Workspace persist failed: {str(e)}")

    def _load(self) -> None:
        """Load state from disk."""
        if not self.persist_path.exists():
            return

        try:
            with open(self.persist_path, 'r') as f:
                data = json.load(f)
                self._state = CognitiveState.from_dict(data.get('state', {}))
                self._entries = {
                    e['id']: WorkspaceEntry.from_dict(e)
                    for e in data.get('entries', [])
                }
                self._vector_clock = data.get('vector_clock', {self.node_id: 0})
        except (IOError, json.JSONDecodeError) as e:
            print(f"Workspace load failed: {str(e)}")

    # ── Convenience API ─────────────────────────────────────────

    def add_hypothesis(self, claim: str, confidence: float = 0.5,
                       evidence: Optional[List[str]] = None) -> str:
        """Add a hypothesis to the cognitive workspace.

        Args:
            claim: The hypothesis statement.
            confidence: Confidence level 0.0-1.0.
            evidence: Optional supporting evidence items.

        Returns:
            Hypothesis key (UUID string) for later reference.
        """
        if not self.enabled:
            return ""
        h = Hypothesis(claim=claim, confidence=confidence,
                       evidence=evidence or [])
        key = str(uuid.uuid4())
        self._state.hypotheses[key] = h
        self._persist()
        return key

    def add_belief(self, claim: str, confidence: float = 0.5,
                   source: str = "") -> str:
        """Register a belief in the cognitive workspace.

        Args:
            claim: The belief statement.
            confidence: Confidence level 0.0-1.0 (mapped to BeliefStrength).
            source: Optional provenance string.

        Returns:
            The claim string (used as the belief key in the graph).
        """
        if not self.enabled:
            return ""
        strength = BeliefStrength.STRONG
        if confidence < 0.3:
            strength = BeliefStrength.SPECULATIVE
        elif confidence < 0.5:
            strength = BeliefStrength.TENTATIVE
        elif confidence < 0.7:
            strength = BeliefStrength.MODERATE
        elif confidence < 0.9:
            strength = BeliefStrength.STRONG
        else:
            strength = BeliefStrength.CONFIRMED
        b = Belief(claim=claim, strength=strength,
                   sources=[source] if source else [])
        self._state.beliefs.add_belief(b)
        self._persist()
        return claim

    def _gc_expired(self) -> None:
        """Remove expired entries."""
        now = datetime.now().timestamp()
        expired = [
            eid for eid, e in self._entries.items()
            if now > e.timestamp + e.ttl
        ]
        for eid in expired:
            del self._entries[eid]

    def _bump_clock(self) -> None:
        """Increment vector clock for this node."""
        self._vector_clock[self.node_id] = self._vector_clock.get(self.node_id, 0) + 1

    def _notify(self, topic: str, entry: WorkspaceEntry) -> None:
        """Notify subscribers for exact topic and wildcard."""
        for t in [topic, '*']:
            for callback in self._subscribers[t]:
                try:
                    callback(entry)
                except Exception as e:
                    print(f"Subscriber callback failed: {str(e)}")
