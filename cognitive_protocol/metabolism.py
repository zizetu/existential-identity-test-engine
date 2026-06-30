"""CognitiveMetabolism - cognitive metabolism

Not periodic cleanup, but continuous metabolism.
Each signal has its own half-life, determined not by time but by verification count.
Verified = half-life extended, unverified = natural decay.

ESSENCE has the longest half-life, RAW has the shortest.
No need to manually decide what to delete - metabolism decides on its own.

Like cellular metabolism: old cells die naturally, new cells grow naturally.
No one is going through a checklist deleting cells - the metabolic system runs continuously.
"""

import time
import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

from cognitive_protocol.signal import Signal, SignalSource, SignalQuality


class MetabolismConfig:
    """Metabolism configuration"""
    # Decay threshold: signals below this value are archived (not deleted)
    ARCHIVE_THRESHOLD = 0.05
    # Metabolism interval (seconds)
    METABOLISM_INTERVAL = 3600  # 1 hour
    # Verification boost
    VERIFY_BOOST = 0.05
    # Verification failure penalty
    VERIFY_PENALTY = 0.10
    # Half-life extension factor (per successful verification)
    HALF_LIFE_EXTEND = 1.5
    # Half-life shrink factor (per failed verification)
    HALF_LIFE_SHRINK = 0.5
    # Maximum half-life (days)
    MAX_HALF_LIFE = 365.0
    # Minimum half-life (days)
    MIN_HALF_LIFE = 0.1
    # Archive storage path
    ARCHIVE_PATH = os.path.expanduser("~/.tical/signal_archive")


class CognitiveMetabolism:
    """Cognitive metabolism system - let signals metabolize themselves

    Metabolism is not disk cleanup, it is a natural cognitive process:
    - Unverified signals decay naturally
    - Verified signals gain longer life
    - Signals decayed to threshold are archived (not deleted, they sink into the RAW layer)

    Usage:
        metabolism = CognitiveMetabolism()

        # Register a signal
        metabolism.register(signal)

        # Verify a signal (success/failure)
        metabolism.verify(signal_id, success=True)

        # Run one metabolism cycle
        metabolism.run_cycle()

        # Query active signals
        active = metabolism.get_active_signals()
    """

    def __init__(self, config: Optional[MetabolismConfig] = None):
        self.config = config or MetabolismConfig()
        self._signals: Dict[str, Signal] = {}  # id -> Signal
        self._archive: List[Dict] = []
        self._last_metabolism = time.time()

    def register(self, signal: Signal, signal_id: Optional[str] = None) -> str:
        """Register a signal into the metabolism system

        Args:
            signal: the signal to register
            signal_id: optional ID, auto-generated if not provided

        Returns:
            signal ID
        """
        if signal_id is None:
            signal_id = f"{signal.source.value}_{int(signal.created_at * 1000)}"
        self._signals[signal_id] = signal
        return signal_id

    def verify(self, signal_id: str, success: bool = True) -> Optional[Signal]:
        """Verify a signal - changes the signal's vitality

        Verification passed: confidence boosted, half-life extended
        Verification failed: confidence dropped, half-life shortened
        """
        if signal_id not in self._signals:
            return None

        signal = self._signals[signal_id]
        new_signal = signal.verify(success, self.config.VERIFY_BOOST)
        self._signals[signal_id] = new_signal
        return new_signal

    def run_cycle(self, now: Optional[float] = None) -> Dict[str, int]:
        """Run one metabolism cycle

        Iterates all signals, computes decayed confidence,
        archives expired signals.

        Returns:
            {"active": int, "archived": int, "expired": int}
        """
        if now is None:
            now = time.time()

        to_archive = []
        active_count = 0

        for sid, signal in self._signals.items():
            current_conf = signal.current_confidence(now)
            if current_conf < self.config.ARCHIVE_THRESHOLD:
                to_archive.append(sid)
            else:
                active_count += 1

        # Archive
        archived_count = 0
        for sid in to_archive:
            signal = self._signals.pop(sid)
            self._archive.append({
                "signal": signal.to_dict(),
                "archived_at": now,
                "final_confidence": signal.current_confidence(now),
            })
            archived_count += 1

        self._last_metabolism = now

        return {
            "active": active_count,
            "archived": archived_count,
            "total_registered": len(self._signals) + archived_count,
        }

    def get_active_signals(self, min_confidence: float = 0.0,
                           source: Optional[SignalSource] = None,
                           now: Optional[float] = None) -> List[Tuple[str, Signal]]:
        """Query active signals

        Args:
            min_confidence: minimum confidence threshold
            source: optional source filter
            now: current time

        Returns:
            [(signal_id, Signal), ...] sorted by current confidence descending
        """
        if now is None:
            now = time.time()

        results = []
        for sid, signal in self._signals.items():
            current_conf = signal.current_confidence(now)
            if current_conf >= min_confidence:
                if source is None or signal.source == source:
                    results.append((sid, signal, current_conf))

        # Sort by current confidence descending
        results.sort(key=lambda x: x[2], reverse=True)
        return [(sid, sig) for sid, sig, _ in results]

    def get_signal_by_id(self, signal_id: str) -> Optional[Signal]:
        """Query signal by ID"""
        return self._signals.get(signal_id)

    def get_metabolism_stats(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Metabolism statistics"""
        if now is None:
            now = time.time()

        source_stats = defaultdict(lambda: {"count": 0, "avg_confidence": 0.0})
        quality_stats = defaultdict(int)

        for signal in self._signals.values():
            current_conf = signal.current_confidence(now)
            source_stats[signal.source.value]["count"] += 1
            source_stats[signal.source.value]["avg_confidence"] += current_conf
            quality_stats[signal.quality.value] += 1

        # Compute averages
        for src in source_stats:
            count = source_stats[src]["count"]
            if count > 0:
                source_stats[src]["avg_confidence"] = round(
                    source_stats[src]["avg_confidence"] / count, 4
                )

        return {
            "total_active": len(self._signals),
            "total_archived": len(self._archive),
            "by_source": dict(source_stats),
            "by_quality": dict(quality_stats),
            "last_metabolism": self._last_metabolism,
        }

    def save_state(self, path: Optional[str] = None) -> str:
        """Persist metabolism state"""
        path = path or os.path.join(
            self.config.ARCHIVE_PATH, "metabolism_state.json"
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)

        state = {
            "signals": {sid: s.to_dict() for sid, s in self._signals.items()},
            "archive": self._archive[-100:],  # Keep only last 100 archived entries
            "last_metabolism": self._last_metabolism,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        return path

    def load_state(self, path: Optional[str] = None) -> int:
        """Load metabolism state, returns number of signals loaded"""
        path = path or os.path.join(
            self.config.ARCHIVE_PATH, "metabolism_state.json"
        )
        if not os.path.exists(path):
            return 0

        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)

        self._signals = {
            sid: Signal.from_dict(s) for sid, s in state.get("signals", {}).items()
        }
        self._archive = state.get("archive", [])
        self._last_metabolism = state.get("last_metabolism", time.time())

        return len(self._signals)
