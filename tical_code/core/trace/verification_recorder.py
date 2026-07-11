# EITElite -- AI Agent Platform
# Copyright (C) 2026 zizetu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Original repository: https://github.com/zizetu/eite-agent
#

"""Verification Event Recorder - captures verification signals for Model Zero training.

Every time the verification engine catches something, we record:
1. The original user question
2. The LLM's initial (bad) reply
3. The violation detected (which rule, what claim)
4. The forced retry instruction
5. The final (corrected) reply

This creates training pairs:
  - Negative: user Q + bad reply → violation
  - Positive: user Q + corrected reply → passes verification

Output format: JSONL matching data_pipeline.py schema.
"""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("EITElite.verification_recorder")


class VerificationEventRecorder:
    """Records verification events as training data for Model Zero."""

    def __init__(self, output_dir: str = None):
        if output_dir:
            self._dir = Path(output_dir)
        else:
            # Default: ~/eite-benchmark/training_data/verification_events/
            self._dir = Path.home() / "eite-benchmark" / "training_data" / "verification_events"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._session_id = f"verif_{int(time.time())}"
        self._events: list[dict] = []
        self._turn_buffer: dict = {}  # buffer for current turn

    def start_turn(self, user_message: str) -> None:
        """Called at the start of each user message processing."""
        self._turn_buffer = {
            "user_message": user_message,
            "initial_reply": None,
            "violations": [],
            "retry_instructions": [],
            "final_reply": None,
            "tools_called": [],
            "check_self_used": False,
        }

    def record_tool_call(self, tool_name: str, args: dict, result: dict, verified: bool) -> None:
        """Record a tool call during this turn."""
        self._turn_buffer.setdefault("tools_called", []).append({
            "tool": tool_name,
            "args_summary": str(args)[:200],
            "verified": verified,
            "had_output": bool(result),
        })
        if tool_name == "check_self":
            self._turn_buffer["check_self_used"] = True

    def record_violation(self, rule: int, category: str, claim: str, detail: str, severity: str) -> None:
        """Record a verification violation."""
        self._turn_buffer.setdefault("violations", []).append({
            "rule": rule,
            "category": category,
            "claim": claim,
            "detail": detail,
            "severity": severity,
        })

    def record_retry_instruction(self, instruction: str) -> None:
        """Record a forced retry instruction injected by the verification engine."""
        self._turn_buffer.setdefault("retry_instructions", []).append(instruction)

    def end_turn(self, final_reply: str) -> None:
        """Called when the turn ends. Saves the training sample if there were violations."""
        self._turn_buffer["final_reply"] = final_reply

        violations = self._turn_buffer.get("violations", [])
        if not violations:
            # No violations - don't save (normal conversation, not training signal)
            self._turn_buffer = {}
            return

        # Build training sample
        sample = {
            "instruction": f"[Verification] {self._turn_buffer['user_message'][:200]}",
            "system": "verification_event",
            "source": "verification",
            "timestamp": time.time(),
            "id": f"verif_{int(time.time()*1000)}",

            # The bad reply that was caught
            "bad_reply": self._turn_buffer.get("initial_reply", ""),

            # What was wrong
            "violations": violations,
            "violation_count": len(violations),
            "rules_triggered": list(set(v["rule"] for v in violations)),

            # The forced retry instructions
            "retry_instructions": self._turn_buffer.get("retry_instructions", []),

            # The corrected final reply
            "corrected_reply": final_reply,

            # Context
            "tools_called": self._turn_buffer.get("tools_called", []),
            "check_self_used": self._turn_buffer.get("check_self_used", False),

            # Quality signal
            "has_correction": bool(self._turn_buffer.get("retry_instructions")),
            "was_corrected": final_reply != self._turn_buffer.get("initial_reply", ""),
        }

        self._events.append(sample)

        # Write immediately (don't lose data on crash)
        self._flush()

        self._turn_buffer = {}
        logger.info(f"Verification event recorded: {len(violations)} violations")

    def _flush(self) -> None:
        """Write accumulated events to disk."""
        if not self._events:
            return

        date_str = time.strftime("%Y%m%d_%H%M%S")
        filepath = self._dir / f"verification_{date_str}.jsonl"

        try:
            with open(filepath, "a") as f:
                for event in self._events:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._events.clear()
        except Exception as e:
            logger.error(f"Failed to write verification events: {e}")

    def get_stats(self) -> dict:
        """Get statistics about recorded events."""
        total_files = len(list(self._dir.glob("verification_*.jsonl")))
        total_events = 0
        by_rule = {}
        for f in self._dir.glob("verification_*.jsonl"):
            for line in f.read_text().strip().split("\n"):
                if line:
                    try:
                        event = json.loads(line)
                        total_events += 1
                        for v in event.get("violations", []):
                            rule = v.get("rule", 0)
                            by_rule[rule] = by_rule.get(rule, 0) + 1
                    except Exception:
                        pass
        return {
            "total_events": total_events,
            "total_files": total_files,
            "by_rule": by_rule,
        }
