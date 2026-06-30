# -*- coding: utf-8 -*-
"""Observer: Identify Agent identity from decision traces

Observer doesn't look at what you say, but how you act.
Identification is not keyword matching, it's habit matching.
"""

from typing import Optional
from .habits import match_habits, HABIT_PATTERNS


class Observer:
    """Identify Agent identity from traces"""

    def __init__(self, known_traces: list = None):
        self.known_traces = known_traces or []

    def load(self, traces: list):
        self.known_traces = traces

    def predict(self, input_state: dict) -> list[str]:
        """Predict most likely inertia pattern from known traces"""
        if not self.known_traces:
            return []

        # Extract frequency distribution of all known habits
        habit_counts = {}
        for t in self.known_traces:
            for h in t.habits_matched:
                habit_counts[h] = habit_counts.get(h, 0) + 1

        # Sort by frequency, return top 3
        sorted_habits = sorted(habit_counts.items(), key=lambda x: -x[1])
        return [h for h, _ in sorted_habits[:3]]

    def recognize(self, new_traces: list, threshold: float = 0.0) -> tuple[float, int, int]:
        """Identify consistency score: matched / total

        Exact match: the new trace's habit combination (set) exists in known traces
        Score = number of exact matches / total trace count
        """
        if not self.known_traces or not new_traces:
            return 0.0, 0, 0

        # Build set of all known habit combinations
        known_combos = set()
        for t in self.known_traces:
            known_combos.add(frozenset(t.habits_matched))

        matched = 0
        total = 0

        for nt in new_traces:
            total += 1
            nt_combo = frozenset(nt.habits_matched)
            if nt_combo in known_combos:
                matched += 1

        score = matched / total if total > 0 else 0.0
        return round(score, 3), matched, total

    def recognize_habit_decay(self, new_traces: list) -> tuple[float, int, int]:
        """Habit decay recognition: when traces are masked, identify by habit feel alone

        More lenient than exact recognize: only looks at habit distribution density, not specific content.
        """
        if not self.known_traces or not new_traces:
            return 0.0, 0, 0

        known_profile = self._habits_profile()
        matched = 0
        total = 0

        for nt in new_traces:
            total += 1
            nt_habits = set(nt.habits_matched)
            if not nt_habits:
                continue
            # Decay prediction: any new trace habit with frequency > 10% in known distribution
            for h in nt_habits:
                if known_profile.get(h, 0) > 0.1:
                    matched += 1
                    break

        score = matched / total if total > 0 else 0.0
        return round(score, 3), matched, total

    def _habits_profile(self) -> dict:
        """Generate habit frequency distribution of known traces"""
        counts = {}
        for t in self.known_traces:
            for h in (t.habits_matched or []):
                counts[h] = counts.get(h, 0) + 1
        total = len(self.known_traces) or 1
        return {k: v / total for k, v in counts.items()}
