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
# Original repository: https://github.com/zizetu/existential-identity-test-engine
#

"""
ToolStrategy - Tool call strategy management (iteration phase control, efficiency detection)

Provides the concrete implementation of ToolStrategyProtocol for
the DecisionEngine's tool call strategy subsystem.

Key responsibilities:
1. Phase-based iteration control (gather → execute → summarize)
2. Tool classification (read/write/summary)
3. Phase-bound tool allowance checking
4. Jaccard similarity efficiency detection for repetitive tool calls
5. Iteration state tracking

Author: Tical (Zize Tu)
Version: see tical_code.__version__
"""

import logging
from typing import Dict, List, Optional, Tuple

from .decision_engine import (
    TOOL_CATEGORY_READ,
    TOOL_CATEGORY_WRITE,
    ToolIterationState,
)

logger = logging.getLogger(__name__)

# Default phase boundaries (iterations, 1-based)
DEFAULT_GATHER_BOUNDARY = 2
DEFAULT_EXECUTE_BOUNDARY = 4

# Jaccard similarity threshold for efficiency detection
JACCARD_EFFICIENCY_THRESHOLD = 0.85

# Minimum result text length for meaningful Jaccard comparison
MIN_RESULT_LENGTH_FOR_JACCARD = 20

# Known read-type tool name prefixes
READ_TOOL_PREFIXES = [
    "read", "list", "search", "get", "find", "show", "check",
    "view", "cat", "head", "tail", "grep", "stat", "ls", "ll",
    "inspect", "lookup", "fetch", "query", "load", "describe",
    "validate", "verify", "examine", "review",
]

# Known write-type tool name prefixes
WRITE_TOOL_PREFIXES = [
    "write", "patch", "edit", "create", "delete", "remove",
    "update", "set", "save", "store", "upload", "publish",
    "mk", "rm", "cp", "mv", "install", "exec", "run",
    "build", "deploy", "compile", "format",
]

# Known summary-type tools
SUMMARY_TOOLS = [
    "summarize", "report", "finish", "done", "complete",
    "end_task", "stop",
]


class ToolStrategy:
    """Tool call strategy management - iteration phase control, efficiency detection.

    Implements ToolStrategyProtocol to provide concrete strategy logic:

    Attributes:
        max_iterations: Maximum allowed tool iterations before forcing summarize.
        gather_boundary: Last iteration in gather phase (default 2).
        execute_boundary: Last iteration in execution phase (default 4).
    """

    def __init__(self, max_iterations: int = 5):
        self.max_iterations = max_iterations
        self.gather_boundary = min(DEFAULT_GATHER_BOUNDARY, max_iterations)
        self.execute_boundary = min(DEFAULT_EXECUTE_BOUNDARY, max_iterations)

    def get_phase(self, iteration: int) -> str:
        """Determine current phase based on iteration count.

        Args:
            iteration: 1-based iteration count.

        Returns:
            Phase name: "gather" / "execute" / "summarize".
        """
        if iteration <= self.gather_boundary:
            return "gather"
        elif iteration <= self.execute_boundary:
            return "execute"
        else:
            return "summarize"

    def classify_tool(self, tool_name: str) -> str:
        """Classify tool as read / write / summary.

        Args:
            tool_name: Tool name.

        Returns:
            Tool category: TOOL_CATEGORY_READ / TOOL_CATEGORY_WRITE / "summary".
        """
        if not tool_name:
            return TOOL_CATEGORY_READ

        name_lower = tool_name.lower()

        # Check summary tools first
        if name_lower in SUMMARY_TOOLS:
            return "summary"

        # Check write prefixes
        for prefix in WRITE_TOOL_PREFIXES:
            if name_lower.startswith(prefix):
                return TOOL_CATEGORY_WRITE

        # Check read prefixes
        for prefix in READ_TOOL_PREFIXES:
            if name_lower.startswith(prefix):
                return TOOL_CATEGORY_READ

        # Default: treat as read to be safe
        return TOOL_CATEGORY_READ

    def check_tool_allowed(
        self,
        tool_name: str,
        iteration: int,
        result_text: Optional[str] = None,
        state: Optional[ToolIterationState] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Check whether tool is allowed in current iteration.

        Phase rules:
        - Gather phase: only read tools allowed (write tools = violation)
        - Execute phase: both read and write tools allowed
        - Summarize phase: only summary tools allowed (no read/write)

        Args:
            tool_name: Tool name.
            iteration: 1-based iteration count.
            result_text: Optional tool result for efficiency check.
            state: Optional iteration state for efficiency check.

        Returns:
            (allowed, reason) - allowed=True means allow, reason is None or rejection cause.
        """
        phase = self.get_phase(iteration)
        category = self.classify_tool(tool_name)

        if phase == "gather":
            if category == TOOL_CATEGORY_WRITE:
                return False, f"Write tool '{tool_name}' not allowed in gather phase (iter {iteration})"
            return True, None

        elif phase == "execute":
            return True, None

        elif phase == "summarize":
            if category != "summary":
                return False, f"Non-summary tool '{tool_name}' not allowed in summarize phase (iter {iteration})"
            return True, None

        return True, None

    def check_efficiency(
        self,
        tool_name: str,
        result_text: str,
        state: ToolIterationState,
    ) -> Tuple[bool, Optional[str]]:
        """Efficiency detection: terminate if consecutive results are similar.

        Uses Jaccard similarity on consecutive tool result texts.

        Args:
            tool_name: Current tool name.
            result_text: Tool execution result text.
            state: Iteration status tracking state.

        Returns:
            (efficient, reason) - efficient=False indicates low efficiency.
        """
        if not state.last_results or len(state.last_results) < 1:
            return True, None

        # Compare with the most recent result
        prev_result = state.last_results[-1]

        if not result_text or not prev_result:
            return True, None

        if len(result_text) < MIN_RESULT_LENGTH_FOR_JACCARD or len(prev_result) < MIN_RESULT_LENGTH_FOR_JACCARD:
            return True, None

        similarity = self._jaccard_similarity(result_text, prev_result)

        if similarity > JACCARD_EFFICIENCY_THRESHOLD:
            logger.info(
                "[ToolStrategy] Low efficiency: tool '%s' produced result %.0f%% similar to previous result",
                tool_name, similarity * 100,
            )
            return False, f"Low efficiency: result is {similarity:.0%} similar to previous iteration"

        return True, None

    def get_iteration_state(self, iteration: int) -> ToolIterationState:
        """Create iteration status object.

        Args:
            iteration: 1-based iteration count.

        Returns:
            ToolIterationState instance with phase populated.
        """
        state = ToolIterationState()
        state.iteration = iteration
        state.phase = self.get_phase(iteration)
        return state

    @staticmethod
    def _jaccard_similarity(text1: str, text2: str) -> float:
        """Compute Jaccard similarity between two text strings.

        Uses word-level tokenization with whitespace splitting.

        Args:
            text1: First text.
            text2: Second text.

        Returns:
            Jaccard similarity score (0.0 - 1.0).
        """
        tokens1 = set(text1.split())
        tokens2 = set(text2.split())

        if not tokens1 and not tokens2:
            return 1.0
        if not tokens1 or not tokens2:
            return 0.0

        intersection = tokens1 & tokens2
        union = tokens1 | tokens2

        return len(intersection) / len(union)
