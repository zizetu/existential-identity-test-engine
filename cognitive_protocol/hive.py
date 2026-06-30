"""HiveProtocol - Collective Wisdom Aggregation Protocol

Big model companies use massive data + RLHF → suppress noise with scale
We use calibrated signals + Hive → defeat noise with precision

Key difference:
- RLHF treats feedback as labels: user likes → reinforce (but likes may be due to sycophancy)
- Hive treats feedback as noisy observations: calibrated facts → collective wisdom (noise blocked at entry)

Every piece of data in Hive carries its own quality label (confidence),
No need to hedge noise with scale, because noise is already filtered at dual-layer calibration.

Data flow:
  User feedback → Layer 1 calibration → pure fact signal ─┐
  AI output  → Layer 2 calibration → confidence-tagged knowledge ─┤→ Hive aggregation → collective wisdom
                                         │
  Only signals with confidence >= HIVE_THRESHOLD  │
  can enter the Hive. Insufficient ones stay in local metabolism.     ─┘
"""

import time
import json
import math
import hashlib
import os
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

from cognitive_protocol.signal import Signal, SignalSource, SignalQuality


# ============ Hive Configuration ============

class HiveConfig:
    """Hive aggregation configuration"""
    # Minimum confidence threshold to enter Hive
    HIVE_THRESHOLD = 0.6
    # Minimum occurrences for collective pattern discovery
    PATTERN_MIN_OCCURRENCES = 3
    # Pattern confidence decay (days)
    PATTERN_HALF_LIFE = 90.0
    # Persistence path
    HIVE_DB_PATH = os.path.expanduser("~/.tical/hive_collective.json")


@dataclass
class CollectivePattern:
    """Collective wisdom pattern - cross-project universal pattern aggregated from multiple calibrated signals

    Difference from ESSENCE:
    - ESSENCE is a single Agent's own essence
    - CollectivePattern is collective wisdom jointly verified by multiple Agents

    Higher entry threshold, because collective wisdom must be more reliable than individual experience.
    """
    pattern_id: str
    content: str                     # Pattern content
    confidence: float                # Collective confidence
    source_count: int                # Number of signals contributing to this pattern
    agent_count: int                 # Number of Agents contributing to this pattern
    category: str                    # Pattern category (axiom/scar/practice/insight)
    first_seen: float                # First seen time
    last_seen: float                 # Last seen time
    verified_count: int = 0          # Verified count
    half_life: float = 90.0          # Half-life (days)
    contributing_signals: List[str] = field(default_factory=list)  # Contributing signal IDs
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_axiom(self) -> bool:
        """Whether it reaches axiom level"""
        return self.confidence >= 0.9 and self.verified_count >= 5

    @property
    def is_scar(self) -> bool:
        """Whether it reaches scar level (low frequency, high cost)"""
        return self.category == "scar" and self.confidence >= 0.7

    def current_confidence(self, now: Optional[float] = None) -> float:
        """Current confidence accounting for time decay"""
        if now is None:
            now = time.time()
        elapsed_days = (now - self.last_seen) / 86400.0
        decay = math.exp(-0.693 * elapsed_days / self.half_life)
        return self.confidence * decay


class HiveProtocol:
    """Collective wisdom aggregation protocol

    Usage:
        hive = HiveProtocol()

        # Submit calibrated signal
        hive.submit(signal, agent_id="kael")

        # Query collective wisdom
        patterns = hive.query("hallucination")

        # Run pattern discovery
        new_patterns = hive.discover_patterns()

    Core principles:
    - Only calibrated signals can enter Hive
    - Pattern discovery based on multi-Agent cross-validation
    - Collective confidence = weighted aggregation of individual confidences
    - Noise blocked at entry, no need for scale hedging
    """

    def __init__(self, config: Optional[HiveConfig] = None):
        self.config = config or HiveConfig()
        self._signal_pool: Dict[str, Dict] = {}  # signal_hash -> {signal, agent_id, timestamp}
        self._patterns: Dict[str, CollectivePattern] = {}
        self._content_index: Dict[str, List[str]] = defaultdict(list)  # keyword -> pattern_ids

    def submit(self, signal: Signal, agent_id: str = "unknown") -> Optional[str]:
        """Submit calibrated signal to Hive

        Only signals with confidence >= HIVE_THRESHOLD can enter.
        Returns None if insufficient, but not discarded - stays in local metabolism system.

        Returns:
            Signal ID in Hive, or None (signal quality insufficient)
        """
        # Threshold check
        if signal.confidence < self.config.HIVE_THRESHOLD:
            return None

        # Signal dedup (same content + same source = duplicate submission)
        content_hash = hashlib.md5(
            f"{signal.payload}:{signal.source.value}".encode()
        ).hexdigest()[:12]

        if content_hash in self._signal_pool:
            # Already exists: increment source count
            existing = self._signal_pool[content_hash]
            existing["source_count"] = existing.get("source_count", 1) + 1
            existing["last_seen"] = time.time()
            # Multiple Agents contribute same signal → boost confidence
            if agent_id not in existing.get("contributing_agents", []):
                existing.setdefault("contributing_agents", []).append(agent_id)
                existing["confidence"] = min(1.0, existing["confidence"] + 0.05)
            return content_hash

        # New signal
        self._signal_pool[content_hash] = {
            "signal": signal,
            "agent_id": agent_id,
            "source_count": 1,
            "contributing_agents": [agent_id],
            "first_seen": time.time(),
            "last_seen": time.time(),
            "confidence": signal.confidence,
        }

        # Update content index (simple keyword extraction)
        self._index_signal(content_hash, signal.payload)

        return content_hash

    def discover_patterns(self) -> List[CollectivePattern]:
        """Discover collective wisdom patterns from signal pool

        Pattern discovery rules:
        1. Same content mentioned by multiple signals → likely a pattern
        2. Mentioned by multiple Agents → cross-validation, confidence boost
        3. Verified signals → more likely a pattern
        4. High-impact facts → priority extraction
        """
        new_patterns = []

        for content_hash, entry in self._signal_pool.items():
            signal = entry["signal"]
            source_count = entry["source_count"]
            agent_count = len(entry.get("contributing_agents", []))
            confidence = entry["confidence"]

            # Pattern discovery conditions:
            # - Occurrences >= minimum threshold OR multi-Agent cross-validation
            # - Confidence sufficiently high
            is_pattern = (
                source_count >= self.config.PATTERN_MIN_OCCURRENCES or
                agent_count >= 2
            ) and confidence >= self.config.HIVE_THRESHOLD

            if not is_pattern:
                continue

            # Determine pattern category
            category = self._classify_pattern(signal, entry)

            # Calculate collective confidence
            # Multi-Agent cross-validation = significant boost
            collective_confidence = min(1.0, confidence + 0.1 * (agent_count - 1))

            # Create pattern
            pattern_id = f"pattern_{content_hash}"
            if pattern_id not in self._patterns:
                pattern = CollectivePattern(
                    pattern_id=pattern_id,
                    content=str(signal.payload)[:200],
                    confidence=collective_confidence,
                    source_count=source_count,
                    agent_count=agent_count,
                    category=category,
                    first_seen=entry["first_seen"],
                    last_seen=entry["last_seen"],
                    verified_count=signal.verified_count,
                    contributing_signals=[content_hash],
                    metadata=signal.calibration_trace,
                )
                self._patterns[pattern_id] = pattern
                new_patterns.append(pattern)

                # Update content index
                self._index_pattern(pattern_id, pattern.content)

        return new_patterns

    def query(self, keyword: str, min_confidence: float = 0.5,
              category: Optional[str] = None) -> List[CollectivePattern]:
        """Query collective wisdom

        Args:
            keyword: Search keyword
            min_confidence: Minimum confidence
            category: Optional category filter

        Returns:
            Matching collective wisdom patterns, sorted by confidence descending
        """
        now = time.time()
        results = []

        # Keyword matching
        matched_ids = set()
        for kw in keyword.split():
            kw = kw.lower()
            for pid in self._content_index.get(kw, []):
                matched_ids.add(pid)

        # Full-text search (direct match for short patterns)
        for pid, pattern in self._patterns.items():
            if keyword.lower() in pattern.content.lower():
                matched_ids.add(pid)

        for pid in matched_ids:
            if pid not in self._patterns:
                continue
            pattern = self._patterns[pid]
            current_conf = pattern.current_confidence(now)
            if current_conf < min_confidence:
                continue
            if category and pattern.category != category:
                continue
            results.append(pattern)

        results.sort(key=lambda p: p.current_confidence(now), reverse=True)
        return results

    def verify_pattern(self, pattern_id: str, success: bool = True) -> Optional[CollectivePattern]:
        """Verify collective wisdom pattern"""
        if pattern_id not in self._patterns:
            return None

        pattern = self._patterns[pattern_id]
        if success:
            pattern.verified_count += 1
            pattern.confidence = min(1.0, pattern.confidence + 0.03)
            pattern.half_life = min(365.0, pattern.half_life * 1.3)
            pattern.last_seen = time.time()
        else:
            pattern.confidence = max(0.1, pattern.confidence - 0.05)
            pattern.half_life = max(1.0, pattern.half_life * 0.7)

        return pattern

    def get_hive_stats(self) -> Dict[str, Any]:
        """Hive statistics"""
        now = time.time()
        by_category = defaultdict(int)
        by_quality = defaultdict(int)
        total_active = 0

        for pattern in self._patterns.values():
            if pattern.current_confidence(now) >= 0.3:
                total_active += 1
                by_category[pattern.category] += 1
                if pattern.is_axiom:
                    by_quality["axiom"] += 1
                elif pattern.is_scar:
                    by_quality["scar"] += 1
                else:
                    by_quality["practice"] += 1

        return {
            "signal_pool_size": len(self._signal_pool),
            "total_patterns": len(self._patterns),
            "active_patterns": total_active,
            "by_category": dict(by_category),
            "by_quality": dict(by_quality),
            "hive_threshold": self.config.HIVE_THRESHOLD,
        }

    def save_state(self, path: Optional[str] = None) -> str:
        """Persist Hive state"""
        path = path or self.config.HIVE_DB_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)

        state = {
            "signal_pool": {},
            "patterns": {},
        }
        for k, v in self._signal_pool.items():
            state["signal_pool"][k] = {
                "signal": v["signal"].to_dict(),
                "agent_id": v["agent_id"],
                "source_count": v["source_count"],
                "contributing_agents": v.get("contributing_agents", []),
                "first_seen": v["first_seen"],
                "last_seen": v["last_seen"],
                "confidence": v["confidence"],
            }
        for pid, p in self._patterns.items():
            state["patterns"][pid] = {
                "pattern_id": p.pattern_id,
                "content": p.content,
                "confidence": p.confidence,
                "source_count": p.source_count,
                "agent_count": p.agent_count,
                "category": p.category,
                "first_seen": p.first_seen,
                "last_seen": p.last_seen,
                "verified_count": p.verified_count,
                "half_life": p.half_life,
            }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return path

    def load_state(self, path: Optional[str] = None) -> int:
        """Load Hive state"""
        path = path or self.config.HIVE_DB_PATH
        if not os.path.exists(path):
            return 0

        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)

        # Load signal pool
        for k, v in state.get("signal_pool", {}).items():
            self._signal_pool[k] = {
                "signal": Signal.from_dict(v["signal"]),
                "agent_id": v["agent_id"],
                "source_count": v.get("source_count", 1),
                "contributing_agents": v.get("contributing_agents", []),
                "first_seen": v.get("first_seen", time.time()),
                "last_seen": v.get("last_seen", time.time()),
                "confidence": v.get("confidence", 0.5),
            }

        # Load patterns
        for pid, p in state.get("patterns", {}).items():
            self._patterns[pid] = CollectivePattern(
                pattern_id=p["pattern_id"],
                content=p["content"],
                confidence=p["confidence"],
                source_count=p["source_count"],
                agent_count=p["agent_count"],
                category=p["category"],
                first_seen=p["first_seen"],
                last_seen=p["last_seen"],
                verified_count=p.get("verified_count", 0),
                half_life=p.get("half_life", 90.0),
            )

        return len(self._patterns)

    def _classify_pattern(self, signal: Signal, entry: Dict) -> str:
        """Pattern classification"""
        # Scar pattern: high-impact negative experience
        calibration = signal.calibration_trace
        if calibration.get("fact_impact", 0) > 0.6:
            return "scar"
        # Axiom pattern: ESSENCE source
        if signal.source == SignalSource.ESSENCE:
            return "axiom"
        # Practice pattern: has verification record
        if signal.verified_count >= 3:
            return "practice"
        # Default: insight
        return "insight"

    def _index_signal(self, content_hash: str, content: str):
        """Simple keyword index"""
        words = content.lower().split()
        # Chinese: character-level index
        cn_chars = [c for c in content if '\u4e00' <= c <= '\u9fa5']
        for c in cn_chars:
            self._content_index[c].append(content_hash)
        # English: word-level index
        for w in words:
            if len(w) >= 2:
                self._content_index[w].append(content_hash)

    def _index_pattern(self, pattern_id: str, content: str):
        """Pattern index"""
        words = content.lower().split()
        cn_chars = [c for c in content if '\u4e00' <= c <= '\u9fa5']
        for c in cn_chars:
            self._content_index[c].append(pattern_id)
        for w in words:
            if len(w) >= 2:
                self._content_index[w].append(pattern_id)
