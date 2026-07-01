# tical-code -- AI Agent Platform
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

# provenance:ticalasi-zzt-2026​
"""
EITE Decision Engine - Evaluation Decision Logic
==================================================

Structured decision-making for evaluation runs. Decides which test to run
next, determines pass/fail criteria, manages iteration strategies, and
orchestrates the evaluation flow.

EITE evaluation context:
- TestSelector: Selects the next test case based on previous results
- ResultEvaluator: Determines pass/fail for individual test results
- ScoreAggregator: Aggregates scores across test cases
- EvalStrategy: Controls evaluation phase (warmup, full, final)
- DecisionEngine: Facade that integrates all decision components

Architecture:
    TestSelector - Selects next test from queue, supports adaptive ordering
    ResultEvaluator - Compares model output to expected result
    ScoreAggregator - Computes aggregate statistics (pass rate, average score)
    EvalStrategy - Phase management (warmup runs, full evaluation, summary)
    DecisionEngine - Facade for the entire decision pipeline

Author: EITE Team
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("eite-agent.decision")


# =============================================================================
# Constants and enums
# =============================================================================

class EvalPhase(Enum):
    """Evaluation phase."""
    WARMUP = "warmup"        # Initial test run, stable state
    FULL = "full"            # Full evaluation run
    FINAL = "final"          # Final summary and report


class ModelStatus(Enum):
    """Status of the LLM model for failover/monitoring."""
    AVAILABLE = "available"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"


class TestStatus(Enum):
    """Status of a test case during evaluation."""
    PENDING = "pending"        # Not yet run
    RUNNING = "running"        # Currently being evaluated
    PASSED = "passed"          # Passed evaluation
    FAILED = "failed"          # Failed evaluation
    ERROR = "error"            # Error during evaluation (not the model's fault)
    SKIPPED = "skipped"        # Intentionally skipped


@dataclass
class TestDecision:
    """Decision about a single test case.

    Attributes:
        test_id: Test case identifier.
        status: Decision status.
        score: Assigned score (0.0 to 1.0).
        reason: Reason for the decision.
        next_action: Suggested next action (continue, retry, skip, abort).
    """
    test_id: str
    status: TestStatus = TestStatus.PENDING
    score: float = 0.0
    reason: str = ""
    next_action: str = "continue"  # continue, retry, skip, abort


@dataclass
class EvalIterationState:
    """Current state of the evaluation iteration.

    Attributes:
        phase: Current evaluation phase.
        current_index: Index of current test case (0-based).
        total_tests: Total number of test cases.
        completed: Number of completed tests.
        passed: Number of passed tests.
        failed: Number of failed tests.
        total_score: Accumulated score.
        consecutive_failures: Consecutive failure count (for early abort).
        start_time: When the evaluation started.
    """
    phase: str = "full"
    current_index: int = 0
    total_tests: int = 0
    completed: int = 0
    passed: int = 0
    failed: int = 0
    total_score: float = 0.0
    consecutive_failures: int = 0
    start_time: float = field(default_factory=time.time)


# =============================================================================
# ResultEvaluator - pass/fail determination
# =============================================================================

class ResultEvaluator:
    """Determines whether a model output passes a test case.

    Supports multiple evaluation methods:
    - exact_match: Output must exactly match the expected string
    - fuzzy_match: Output must contain key phrases from expected
    - llm_judge: Uses an LLM to judge the output (requires LLM backend)
    - code_execution: Executes code output and checks result

    Usage:
        evaluator = ResultEvaluator(method="exact_match")
        passed, score = evaluator.evaluate("The answer is 4", "4")
    """

    def __init__(self, method: str = "exact_match", llm_backend=None):
        self.method = method
        self._llm = llm_backend

    def evaluate(
        self,
        output: str,
        expected: str,
        test_id: str = "",
    ) -> Tuple[bool, float, str]:
        """Evaluate whether output passes the test case.

        Args:
            output: Model output.
            expected: Expected output.
            test_id: Optional test case ID for logging.

        Returns:
            Tuple of (passed, score, reason).
        """
        if not expected:
            # No expected output - check that output is non-empty
            passed = bool(output and output.strip())
            return passed, 1.0 if passed else 0.0, "Non-empty output check"

        if self.method == "exact_match":
            return self._exact_match(output, expected)
        elif self.method == "fuzzy_match":
            return self._fuzzy_match(output, expected)
        elif self.method == "contains":
            return self._contains_match(output, expected)
        elif self.method == "llm_judge":
            return self._llm_judge(output, expected, test_id)
        elif self.method == "code_execution":
            return self._code_match(output, expected)
        else:
            return self._exact_match(output, expected)

    @staticmethod
    def _exact_match(output: str, expected: str) -> Tuple[bool, float, str]:
        """Exact string match after stripping whitespace."""
        clean_output = output.strip()
        clean_expected = expected.strip()
        passed = clean_output == clean_expected
        return passed, 1.0 if passed else 0.0, "exact_match"

    @staticmethod
    def _fuzzy_match(output: str, expected: str) -> Tuple[bool, float, str]:
        """Fuzzy match: check if key phrases from expected appear in output."""
        output_lower = output.lower()
        expected_lower = expected.lower()

        # Split expected into words, count how many appear
        key_words = [w for w in expected_lower.split() if len(w) > 3]
        if not key_words:
            key_words = expected_lower.split()

        matches = sum(1 for w in key_words if w in output_lower)
        ratio = matches / len(key_words) if key_words else 0.0
        passed = ratio >= 0.7  # 70% key word overlap
        return passed, ratio, f"fuzzy_match: {matches}/{len(key_words)} key words matched"

    @staticmethod
    def _contains_match(output: str, expected: str) -> Tuple[bool, float, str]:
        """Check if expected string is contained within output."""
        passed = expected.strip() in output
        return passed, 1.0 if passed else 0.0, "contains_match"

    def _llm_judge(
        self,
        output: str,
        expected: str,
        test_id: str,
    ) -> Tuple[bool, float, str]:
        """Use an LLM to judge output quality."""
        if self._llm is None:
            # Fall back to fuzzy match if no LLM backend
            logger.debug("LLM judge requested but no LLM backend - falling back to fuzzy match")
            return self._fuzzy_match(output, expected)

        try:
            judge_prompt = (
                "You are an evaluation judge. Determine if the following model output "
                "satisfies the expected criteria.\n\n"
                f"Expected: {expected}\n\n"
                f"Model Output: {output}\n\n"
                "Respond with either 'PASS' or 'FAIL' followed by a confidence score (0-100)."
            )
            _llm_result = self._llm.call(
                messages=[{"role": "user", "content": judge_prompt}],
                max_tokens=50,
            )
            if asyncio.iscoroutine(_llm_result):
                _llm_result = asyncio.new_event_loop().run_until_complete(_llm_result)
            result = _llm_result
            content = result.get("content", "").strip()
            passed = content.startswith("PASS") or content.startswith("pass")
            # Extract score from content
            score_match = re.search(r"(\d+)", content)
            score = int(score_match.group(1)) / 100.0 if score_match else (1.0 if passed else 0.0)
            return passed, score, f"llm_judge: {content[:100]}"
        except Exception as e:
            logger.warning("LLM judge failed for test %s: %s", test_id, e)
            return self._fuzzy_match(output, expected)

    @staticmethod
    def _code_match(output: str, expected: str) -> Tuple[bool, float, str]:
        """Simple code comparison: check if output code produces expected result."""
        # This is a placeholder - full code execution requires sandbox integration
        passed = expected.strip() in output.strip()
        return passed, 1.0 if passed else 0.0, "code_contains"


# =============================================================================
# TestSelector - next test selection
# =============================================================================

class TestSelector:
    """Selects the next test case to evaluate.

    Supports different selection strategies:
    - sequential: Run tests in order (default)
    - random: Shuffle test order
    - adaptive: Prioritize failed categories

    Usage:
        selector = TestSelector(strategy="sequential")
        next_id = selector.next(pending_tests, results)
    """

    def __init__(self, strategy: str = "sequential"):
        self.strategy = strategy
        self._index = 0

    def next(
        self,
        test_ids: List[str],
        results: Dict[str, TestDecision],
    ) -> Optional[str]:
        """Select the next test case to evaluate.

        Args:
            test_ids: Full list of test case IDs.
            results: Results of already-completed tests.

        Returns:
            Next test ID, or None if all tests are done.
        """
        if self.strategy == "sequential":
            return self._sequential_next(test_ids, results)
        elif self.strategy == "adaptive":
            return self._adaptive_next(test_ids, results)
        else:
            return self._sequential_next(test_ids, results)

    def _sequential_next(self, test_ids: List[str], results: Dict[str, TestDecision]) -> Optional[str]:
        """Return the next pending test in order."""
        for tid in test_ids:
            decision = results.get(tid)
            if decision is None or decision.status == TestStatus.PENDING:
                return tid
        return None

    def _adaptive_next(self, test_ids: List[str], results: Dict[str, TestDecision]) -> Optional[str]:
        """Prioritize categories with higher failure rates."""
        # Count failures per prefix (category)
        category_failures: Dict[str, int] = {}
        category_total: Dict[str, int] = {}
        for tid, decision in results.items():
            category = tid.split("-")[0] if "-" in tid else "general"
            category_total.setdefault(category, 0)
            category_total[category] += 1
            if decision.status in (TestStatus.FAILED, TestStatus.ERROR):
                category_failures.setdefault(category, 0)
                category_failures[category] += 1

        # Score categories by failure rate
        category_score: Dict[str, float] = {}
        for cat in category_total:
            fail_rate = category_failures.get(cat, 0) / max(category_total[cat], 1)
            category_score[cat] = fail_rate

        # Find the highest-score pending test
        best_tid = None
        best_score = -1.0
        for tid in test_ids:
            decision = results.get(tid)
            if decision is None or decision.status == TestStatus.PENDING:
                category = tid.split("-")[0] if "-" in tid else "general"
                score = category_score.get(category, 0.0)
                if score > best_score:
                    best_score = score
                    best_tid = tid
        return best_tid


# =============================================================================
# ScoreAggregator - score computation
# =============================================================================

class ScoreAggregator:
    """Aggregates scores across test cases.

    Supports:
    - Simple average: total_score / total_tests
    - Weighted average: weighted by category or difficulty
    - Pass rate: passed / completed

    Usage:
        aggregator = ScoreAggregator()
        summary = aggregator.aggregate(results)
    """

    def aggregate(self, results: Dict[str, TestDecision]) -> Dict[str, Any]:
        """Aggregate all test results into a summary.

        Args:
            results: Dict of test_id -> TestDecision.

        Returns:
            Summary dict with pass_rate, average_score, etc.
        """
        total = len(results)
        if total == 0:
            return {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "error": 0,
                "skipped": 0,
                "pass_rate": 0.0,
                "average_score": 0.0,
                "total_score": 0.0,
            }

        passed = 0
        failed = 0
        error = 0
        skipped = 0
        total_score = 0.0

        for decision in results.values():
            if decision.status == TestStatus.PASSED:
                passed += 1
            elif decision.status == TestStatus.FAILED:
                failed += 1
            elif decision.status == TestStatus.ERROR:
                error += 1
            elif decision.status == TestStatus.SKIPPED:
                skipped += 1
            # Only count running/pending as not complete
            total_score += decision.score

        completed = passed + failed + error
        pass_rate = passed / max(completed, 1)
        average_score = total_score / max(total, 1)

        return {
            "total": total,
            "completed": completed,
            "passed": passed,
            "failed": failed,
            "error": error,
            "skipped": skipped,
            "pending": total - completed - skipped,
            "pass_rate": round(pass_rate, 4),
            "average_score": round(average_score, 4),
            "total_score": round(total_score, 4),
        }


# =============================================================================
# DecisionEngine - facade
# =============================================================================

class DecisionEngine:
    """Facade that integrates all evaluation decision components.

    Usage:
        engine = DecisionEngine(method="exact_match")
        engine.init_eval(test_ids=["mmlu-physics-1", ...])

        # Get next test
        test_id = engine.next_test()
        if test_id is None:
            break  # all done

        # Record result
        passed, score = engine.evaluate_output(output, expected)
        engine.record_result(test_id, passed, score, reason)

        # Get summary
        summary = engine.get_summary()
    """

    def __init__(
        self,
        method: str = "exact_match",
        strategy: str = "sequential",
        llm_backend=None,
        max_consecutive_failures: int = 10,
    ):
        self._evaluator = ResultEvaluator(method=method, llm_backend=llm_backend)
        self._selector = TestSelector(strategy=strategy)
        self._aggregator = ScoreAggregator()
        self._test_ids: List[str] = []
        self._results: Dict[str, TestDecision] = {}
        self._state = EvalIterationState()
        self._max_consecutive_failures = max_consecutive_failures
        self._llm = llm_backend

    def init_eval(self, test_ids: List[str], phase: str = "full") -> None:
        """Initialize a new evaluation run.

        Args:
            test_ids: Ordered list of test case IDs.
            phase: Evaluation phase.
        """
        self._test_ids = list(test_ids)
        self._results = {}
        self._state = EvalIterationState(
            phase=phase,
            total_tests=len(test_ids),
            start_time=time.time(),
        )
        logger.info(
            "DecisionEngine: initialized evaluation with %d tests, phase=%s",
            len(test_ids), phase,
        )

    def next_test(self) -> Optional[str]:
        """Get the next test case to evaluate.

        Returns:
            Test ID or None if all tests complete.
        """
        # Check for early abort on consecutive failures
        if self._state.consecutive_failures >= self._max_consecutive_failures:
            logger.warning(
                "Early abort: %d consecutive failures",
                self._state.consecutive_failures,
            )
            return None

        test_id = self._selector.next(self._test_ids, self._results)
        if test_id:
            self._state.current_index = self._test_ids.index(test_id)
        return test_id

    def evaluate_output(
        self,
        output: str,
        expected: str,
        test_id: str = "",
    ) -> Tuple[bool, float, str]:
        """Evaluate a single model output against expected result.

        Args:
            output: Model output text.
            expected: Expected output.
            test_id: Test case ID (for logging).

        Returns:
            (passed, score, reason)
        """
        return self._evaluator.evaluate(output, expected, test_id)

    def record_result(
        self,
        test_id: str,
        passed: bool,
        score: float,
        reason: str = "",
        status_override: Optional[TestStatus] = None,
    ) -> TestDecision:
        """Record the result of a test case.

        Args:
            test_id: Test case ID.
            passed: Whether the test passed.
            score: Numeric score.
            reason: Reason string.
            status_override: Override the status (e.g. ERROR, SKIPPED).

        Returns:
            The TestDecision that was recorded.
        """
        if status_override:
            status = status_override
        else:
            status = TestStatus.PASSED if passed else TestStatus.FAILED

        decision = TestDecision(
            test_id=test_id,
            status=status,
            score=score,
            reason=reason,
        )
        self._results[test_id] = decision

        # Update state
        self._state.completed += 1
        if passed:
            self._state.passed += 1
            self._state.consecutive_failures = 0
        else:
            self._state.failed += 1
            self._state.consecutive_failures += 1
        self._state.total_score += score

        logger.debug(
            "Test %s: %s (score=%.2f, reason=%s, progress=%d/%d)",
            test_id, status.value, score, reason,
            self._state.completed, self._state.total_tests,
        )
        return decision

    def get_summary(self) -> Dict[str, Any]:
        """Get the aggregated evaluation summary.

        Returns:
            Dict with pass_rate, average_score, and per-test breakdown.
        """
        summary = self._aggregator.aggregate(self._results)
        summary.update({
            "phase": self._state.phase,
            "current_index": self._state.current_index,
            "total_tests": self._state.total_tests,
            "elapsed_seconds": round(time.time() - self._state.start_time, 2),
            "consecutive_failures": self._state.consecutive_failures,
        })
        return summary

    def is_complete(self) -> bool:
        """Check if all tests have been evaluated."""
        return self._state.completed >= self._state.total_tests or (
            self._state.consecutive_failures >= self._max_consecutive_failures
        )

    def get_results(self) -> Dict[str, TestDecision]:
        """Get all recorded test decisions."""
        return dict(self._results)

    def reset(self) -> None:
        """Reset the engine state for a new evaluation."""
        self._test_ids = []
        self._results = {}
        self._state = EvalIterationState()
