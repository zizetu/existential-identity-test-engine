# -*- coding: utf-8 -*-
"""Decision Trace - Record the Agent's decision trajectory

Not memory, not a log, not a snapshot - it's the skeleton of identity.
What's recorded is not what the Agent knows, but how it chooses to act.
"""

import json
import time
import random
from typing import Optional


class DecisionTrace:
    """A single decision trace"""

    def __init__(self, action: str, context: dict = None, result: dict = None):
        self.timestamp = time.time()
        self.action = action
        self.context = context or {}
        self.result = result or {}
        self.habits_matched: list[str] = []
        self.habit_scores: dict[str, float] = {}

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "action": self.action,
            "context": self.context,
            "result": self.result,
            "habits_matched": self.habits_matched,
            "habit_scores": self.habit_scores,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DecisionTrace":
        t = cls(d.get("action", ""))
        t.timestamp = d.get("timestamp", time.time())
        t.context = d.get("context", {})
        t.result = d.get("result", {})
        t.habits_matched = d.get("habits_matched", [])
        t.habit_scores = d.get("habit_scores", {})
        return t


class DecisionTraceManager:
    """Decision trace manager"""

    def __init__(self, max_traces: int = 1000):
        self.traces: list[DecisionTrace] = []
        self.max_traces = max_traces

    def add(self, trace: DecisionTrace):
        self.traces.append(trace)
        if len(self.traces) > self.max_traces:
            self.traces = self.traces[-self.max_traces:]

    def mask(self, ratio: float = 0.3):
        """Randomly delete {ratio} of traces to simulate memory loss.
        Preserves decision identity but marks reasoning content.
        Simulates: habits remembered but specific context forgotten.
        """
        count = int(len(self.traces) * ratio)
        if count == 0:
            return
        indices = sorted(random.sample(range(len(self.traces)), count), reverse=True)
        for i in indices:
            self.traces.pop(i)

    def wipe(self):
        """Complete deletion"""
        self.traces.clear()

    def export(self, path: str):
        """Export to JSON file"""
        data = [t.to_dict() for t in self.traces]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def import_from(self, path: str):
        """Import from JSON file"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.traces = [DecisionTrace.from_dict(d) for d in data]

    def stats(self) -> dict:
        """Statistics: habit distribution across all traces"""
        habit_counts = {}
        for t in self.traces:
            for h in t.habits_matched:
                habit_counts[h] = habit_counts.get(h, 0) + 1
        return {
            "total_traces": len(self.traces),
            "habit_distribution": habit_counts,
        }
