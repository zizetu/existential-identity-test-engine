"""Periodic self-reflection and cognitive state monitoring."""

import asyncio
from datetime import datetime
from typing import Dict, List
from .workspace import Workspace

class ReflectionEngine:
    """Automated cognitive state monitoring and adjustment."""
    
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self._last_run: float = 0

    def calibrate_confidence(self) -> List[str]:
        """Check decision history for over/under confidence patterns."""
        findings = []
        state = self.workspace.get_state()
        
        # Check for overconfidence (high confidence but wrong decisions)
        overconfident = [
            d for d in state.decision_trace
            if d.confidence > 0.8 and "failed" in d.action.lower()
        ]
        if overconfident:
            findings.append(f"Detected {len(overconfident)} overconfident decisions")

        # Check for underconfidence (low confidence but correct decisions)
        underconfident = [
            d for d in state.decision_trace
            if d.confidence < 0.3 and "success" in d.action.lower()
        ]
        if underconfident:
            findings.append(f"Detected {len(underconfident)} underconfident decisions")

        return findings

    def check_mood(self) -> List[str]:
        """Detect extreme mood states that may impair reasoning."""
        findings = []
        mood = self.workspace.get_state().mood
        
        if mood.caution > 0.9:
            findings.append("Extreme caution may lead to paralysis")
        if mood.confidence < 0.1:
            findings.append("Very low confidence may impair decision making")
        if mood.curiosity < 0.1:
            findings.append("Low curiosity may limit exploration")
        if mood.curiosity > 0.9:
            findings.append("Extreme curiosity may distract from goals")

        return findings

    def check_attention_stability(self) -> List[str]:
        """Detect rapid goal switching that may indicate focus issues."""
        findings = []
        state = self.workspace.get_state()
        decision_trace = list(state.decision_trace)
        
        if len(decision_trace) < 2:
            return findings
            
        goal_changes = 0
        last_goal = decision_trace[0].action
        for d in decision_trace[1:]:
            if d.action != last_goal:
                goal_changes += 1
                last_goal = d.action
        
        if goal_changes / len(decision_trace) > 0.5:
            findings.append("High goal switching rate (potential attention instability)")

        return findings

    def check_hypothesis_staleness(self, max_age_seconds: float = 3600) -> List[str]:
        """Flag hypotheses that haven't been tested recently."""
        findings = []
        now = datetime.now().timestamp()
        stale = [
            h for h in self.workspace.get_state().hypotheses.values()
            if now - h.updated_at > max_age_seconds
        ]
        
        if stale:
            findings.append(f"{len(stale)} stale hypotheses need testing")

        return findings

    def run_reflection_cycle(self) -> Dict[str, List[str]]:
        """Execute all reflection checks and return findings."""
        self._last_run = datetime.now().timestamp()
        return {
            'confidence': self.calibrate_confidence(),
            'mood': self.check_mood(),
            'attention': self.check_attention_stability(),
            'hypotheses': self.check_hypothesis_staleness()
        }

    async def run_background(self, interval_seconds: float = 300) -> None:
        """Continuous reflection loop running at specified interval."""
        while True:
            try:
                findings = self.run_reflection_cycle()
                if any(findings.values()):
                    self.workspace.write(
                        topic="reflection/findings",
                        payload=findings,
                        ttl=interval_seconds * 2
                    )
            except Exception as e:
                print(f"Reflection cycle failed: {str(e)}")
            
            await asyncio.sleep(interval_seconds)
