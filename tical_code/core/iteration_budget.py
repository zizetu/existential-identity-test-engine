"""Per-turn iteration budget — prevent runaway tool loops.

Caps the number of LLM+tool iterations per turn to prevent runaway API
costs. When the cap is reached, the caller should force a summary reply
instead of continuing tool calls.

Thread-safe: all state mutations are protected by a threading.Lock.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)


class IterationBudget:
    """Thread-safe iteration budget with consume/refund mechanism.

    Tracks iterations per turn and enforces a hard cap with three guard
    rails: max iterations, wall-clock timeout, and consecutive failure
    count.  Thread-safe — safe to share across async and sync callers.

    Attributes:
        max: The maximum number of iterations allowed.
        count: The number of iterations consumed so far.
        start_time: The timestamp when the budget was last reset.
        max_wall_time: Maximum wall-clock seconds per turn.
        max_consecutive_failures: Maximum consecutive failures before
            forced stop.
    """

    def __init__(
        self,
        max_total: int = 10,
        max_consecutive_failures: int = 5,
        max_wall_time: float = 1200.0,
    ):
        """Initialize the budget.

        Args:
            max_total: Maximum iterations before the budget is exhausted.
            max_consecutive_failures: Maximum consecutive failures before
                forced stop (default 5).
            max_wall_time: Maximum wall-clock seconds before forced stop
                (default 1200 = 20 minutes).
        """
        self.max = max_total
        self.max_consecutive_failures = max_consecutive_failures
        self.max_wall_time = max_wall_time
        self._count = 0
        self._consecutive_failures = 0
        self._start_time = 0.0
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Reset the iteration counter, failures, and start time."""
        with self._lock:
            self._count = 0
            self._consecutive_failures = 0
            self._start_time = time.time()
        logger.debug("Iteration budget reset (max=%d)", self.max)

    def consume(self) -> bool:
        """Mark one iteration as used.

        Returns:
            True if the budget is still available after consuming,
            False if the budget has been exhausted (by count, time,
            or consecutive failures).
        """
        with self._lock:
            if self._count >= self.max:
                return False
            if time.time() - self._start_time > self.max_wall_time:
                logger.warning("Iteration budget: wall-clock timeout")
                return False
            if self._consecutive_failures >= self.max_consecutive_failures:
                logger.warning(
                    "Iteration budget: %d consecutive failures",
                    self._consecutive_failures,
                )
                return False
            self._count += 1
            remaining = self.max - self._count
            if remaining <= 0:
                logger.warning(
                    "Iteration budget exhausted: %d/%d iterations consumed",
                    self._count, self.max,
                )
                return False
            logger.debug(
                "Iteration consumed: %d/%d (remaining=%d)",
                self._count, self.max, remaining,
            )
            return True

    def refund(self) -> None:
        """Refund one iteration — call on transient errors (network, rate-limit)."""
        with self._lock:
            if self._count > 0:
                self._count -= 1
                logger.debug("Iteration refunded: now %d/%d", self._count, self.max)

    def record_failure(self) -> None:
        """Record a consecutive failure."""
        with self._lock:
            self._consecutive_failures += 1

    def record_success(self) -> None:
        """Reset the consecutive failure counter."""
        with self._lock:
            self._consecutive_failures = 0

    @property
    def iteration(self) -> int:
        """Return the current iteration count (1-based)."""
        with self._lock:
            return self._count

    @property
    def remaining(self) -> int:
        """Return the number of iterations still available."""
        with self._lock:
            return max(0, self.max - self._count)

    @property
    def elapsed(self) -> float:
        """Return seconds elapsed since the last reset."""
        with self._lock:
            start = self._start_time
        return time.time() - start if start else 0.0

    @property
    def exhausted(self) -> bool:
        """Return True if the budget has been fully consumed."""
        with self._lock:
            return self._count >= self.max

    @property
    def count(self) -> int:
        """Return the raw count (for backward compat)."""
        with self._lock:
            return self._count

    @property
    def start_time(self) -> float:
        """Return the start timestamp."""
        with self._lock:
            return self._start_time