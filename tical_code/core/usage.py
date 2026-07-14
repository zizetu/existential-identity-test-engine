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
EITE Usage Tracker - Evaluation Statistics
============================================

Tracks evaluation usage statistics: test runs, token consumption,
API call counts, pass/fail rates, and storage metrics for evaluation
results.

Features:
- SQLite persistence (zero new dependencies)
- Token usage tracking per evaluation run
- Test result statistics (pass/fail counts, scores)
- API call latency tracking
- Evaluation session management
- Cleanup of old records

Author: EITE Team
"""
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("eite-agent.usage")


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class UsageRecord:
    """A single usage record."""
    record_id: str
    session_id: str
    timestamp: float
    event_type: str  # eval_run, token_usage, api_call, test_result
    category: str    # eval_start, eval_end, input_tokens, output_tokens, api_request, pass, fail
    value: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "category": self.category,
            "value": self.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UsageRecord":
        return cls(
            record_id=data["record_id"],
            session_id=data["session_id"],
            timestamp=data["timestamp"],
            event_type=data["event_type"],
            category=data["category"],
            value=data["value"],
            metadata=data.get("metadata", {}),
        )


@dataclass
class UsageSummary:
    """Usage summary statistics for a period or evaluation run."""
    period_start: float
    period_end: float
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    api_call_count: int
    eval_run_count: int
    tests_passed: int
    tests_failed: int
    total_score: float
    average_score: float
    active_sessions: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "api_call_count": self.api_call_count,
            "eval_run_count": self.eval_run_count,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "total_score": self.total_score,
            "average_score": self.average_score,
            "active_sessions": self.active_sessions,
        }


# =============================================================================
# Usage Tracker
# =============================================================================

class UsageTracker:
    """Track and report evaluation usage statistics.

    Features:
    - SQLite-backed persistence
    - Token usage tracking (input/output)
    - API call counting with latency
    - Test result tracking (pass/fail, scores)
    - Evaluation session management
    - Period-based and session-based summaries

    Usage:
        tracker = UsageTracker()
        tracker.record_eval_run("eval-123", "mmlu", status="start")
        tracker.record_tokens(input_tokens=100, output_tokens=50, model="gpt-4o")
        tracker.record_test_result("test-1", passed=True, score=1.0)
        summary = tracker.get_summary()
    """

    SCHEMA_VERSION = 1

    def __init__(self, db_path: str = "~/.eite-agent/usage.db"):
        self.db_path = os.path.expanduser(db_path)
        self._local = threading.local()
        self._lock = threading.RLock()
        self._session_id: Optional[str] = None
        self._session_start: Optional[float] = None

        # Ensure directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._init_db()
        logger.info("UsageTracker initialized at %s", self.db_path)

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0,
            )
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self) -> None:
        """Initialize database schema."""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Usage records table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usage_records (
                record_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                category TEXT NOT NULL,
                value REAL NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
        """)

        # Indexes for faster queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_session
            ON usage_records(session_id, timestamp)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_category
            ON usage_records(category, timestamp)
        """)

        # Session summary table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_summary (
                session_id TEXT PRIMARY KEY,
                start_time REAL NOT NULL,
                end_time REAL,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                api_call_count INTEGER DEFAULT 0,
                tests_passed INTEGER DEFAULT 0,
                tests_failed INTEGER DEFAULT 0,
                total_score REAL DEFAULT 0.0,
                is_active INTEGER DEFAULT 1
            )
        """)

        conn.commit()
        logger.debug("UsageTracker database schema initialized")

    def _generate_id(self) -> str:
        """Generate unique record ID using uuid."""
        import uuid
        return str(uuid.uuid4())

    def start_session(self, session_id: str = None) -> str:
        """Start tracking a new evaluation session.

        Args:
            session_id: Optional session ID (auto-generated if not provided).

        Returns:
            Session ID.
        """
        import uuid
        with self._lock:
            self._session_id = session_id or str(uuid.uuid4())
            self._session_start = time.time()

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT session_id FROM session_summary WHERE session_id = ?",
            (self._session_id,),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT INTO session_summary (session_id, start_time, is_active) VALUES (?, ?, 1)",
                (self._session_id, self._session_start),
            )
            conn.commit()

        logger.debug("UsageTracker session started: %s", self._session_id)
        return self._session_id

    def end_session(self) -> None:
        """End the current evaluation session."""
        if not self._session_id:
            return
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE session_summary SET end_time = ?, is_active = 0 WHERE session_id = ?",
            (time.time(), self._session_id),
        )
        conn.commit()
        with self._lock:
            logger.debug("UsageTracker session ended: %s", self._session_id)
            self._session_id = None
            self._session_start = None

    def record_eval_run(
        self,
        eval_id: str,
        benchmark: str,
        status: str = "start",
        session_id: str = None,
    ) -> None:
        """Record an evaluation run event.

        Args:
            eval_id: Evaluation run ID.
            benchmark: Benchmark name.
            status: "start", "end", or "abort".
            session_id: Session identifier.
        """
        sid = session_id or self._ensure_session()
        now = time.time()
        conn = self._get_conn()
        cursor = conn.cursor()

        record = UsageRecord(
            record_id=self._generate_id(),
            session_id=sid,
            timestamp=now,
            event_type="eval_run",
            category=f"eval_{status}",
            value=1.0,
            metadata={"eval_id": eval_id, "benchmark": benchmark},
        )
        cursor.execute(
            "INSERT INTO usage_records (record_id, session_id, timestamp, event_type, category, value, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record.record_id, sid, now, record.event_type, record.category,
             record.value, json.dumps(record.metadata)),
        )
        conn.commit()

    def record_tokens(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = "",
        session_id: str = None,
    ) -> None:
        """Record token usage.

        Args:
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.
            model: Model name.
            session_id: Session identifier.
        """
        sid = session_id or self._ensure_session()
        now = time.time()
        meta = {"model": model} if model else {}
        conn = self._get_conn()
        cursor = conn.cursor()

        if input_tokens > 0:
            cursor.execute(
                "INSERT INTO usage_records VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self._generate_id(), sid, now, "token_usage", "input_tokens",
                 float(input_tokens), json.dumps(meta)),
            )
        if output_tokens > 0:
            cursor.execute(
                "INSERT INTO usage_records VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self._generate_id(), sid, now, "token_usage", "output_tokens",
                 float(output_tokens), json.dumps(meta)),
            )

        cursor.execute(
            "UPDATE session_summary SET total_input_tokens = total_input_tokens + ?, "
            "total_output_tokens = total_output_tokens + ? WHERE session_id = ?",
            (input_tokens, output_tokens, sid),
        )
        conn.commit()

    def record_api_call(
        self,
        model: str = "",
        endpoint: str = "",
        latency_ms: float = None,
        status: str = "success",
        session_id: str = None,
    ) -> None:
        """Record an API call.

        Args:
            model: Model used.
            endpoint: API endpoint.
            latency_ms: Response latency in milliseconds.
            status: Call status (success/error).
            session_id: Session identifier.
        """
        sid = session_id or self._ensure_session()
        now = time.time()
        meta = {"model": model, "endpoint": endpoint, "status": status}
        if latency_ms is not None:
            meta["latency_ms"] = latency_ms

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO usage_records VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self._generate_id(), sid, now, "api_call", "api_request",
             1.0, json.dumps(meta)),
        )
        cursor.execute(
            "UPDATE session_summary SET api_call_count = api_call_count + 1 WHERE session_id = ?",
            (sid,),
        )
        conn.commit()

    def record_test_result(
        self,
        test_id: str,
        passed: bool,
        score: float = 0.0,
        test_name: str = "",
        session_id: str = None,
    ) -> None:
        """Record a test result.

        Args:
            test_id: Test case ID.
            passed: Whether the test passed.
            score: Test score.
            test_name: Human-readable test name.
            session_id: Session identifier.
        """
        sid = session_id or self._ensure_session()
        now = time.time()
        category = "pass" if passed else "fail"
        meta = {"test_id": test_id, "test_name": test_name, "score": score}

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO usage_records VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self._generate_id(), sid, now, "test_result", category,
             float(score), json.dumps(meta)),
        )

        if passed:
            cursor.execute(
                "UPDATE session_summary SET tests_passed = tests_passed + 1, "
                "total_score = total_score + ? WHERE session_id = ?",
                (score, sid),
            )
        else:
            cursor.execute(
                "UPDATE session_summary SET tests_failed = tests_failed + 1 "
                "WHERE session_id = ?",
                (sid,),
            )
        conn.commit()

    def _ensure_session(self) -> str:
        """Ensure a session is active (thread-safe)."""
        with self._lock:
            if not self._session_id:
                self.start_session()
            return self._session_id

    def get_summary(
        self,
        period_start: float = None,
        period_end: float = None,
        session_id: str = None,
    ) -> UsageSummary:
        """Get usage summary for a time period or session.

        Args:
            period_start: Start timestamp (default: 24 hours ago).
            period_end: End timestamp (default: now).
            session_id: Filter by session (optional).

        Returns:
            UsageSummary object.
        """
        period_end = period_end or time.time()
        period_start = period_start or (period_end - 86400)

        conn = self._get_conn()
        cursor = conn.cursor()

        # Token totals
        cursor.execute(
            "SELECT category, SUM(value) as total FROM usage_records "
            "WHERE timestamp BETWEEN ? AND ? AND event_type = 'token_usage' "
            "AND (? IS NULL OR session_id = ?) GROUP BY category",
            (period_start, period_end, session_id, session_id),
        )
        token_totals = {row["category"]: int(row["total"]) for row in cursor.fetchall()}
        input_tokens = token_totals.get("input_tokens", 0)
        output_tokens = token_totals.get("output_tokens", 0)

        # API call count
        cursor.execute(
            "SELECT COUNT(*) as count FROM usage_records "
            "WHERE timestamp BETWEEN ? AND ? AND event_type = 'api_call' "
            "AND (? IS NULL OR session_id = ?)",
            (period_start, period_end, session_id, session_id),
        )
        api_call_count = cursor.fetchone()["count"]

        # Eval run count
        cursor.execute(
            "SELECT COUNT(*) as count FROM usage_records "
            "WHERE timestamp BETWEEN ? AND ? AND event_type = 'eval_run' "
            "AND (? IS NULL OR session_id = ?)",
            (period_start, period_end, session_id, session_id),
        )
        eval_run_count = cursor.fetchone()["count"]

        # Test results
        cursor.execute(
            "SELECT category, COUNT(*) as count, SUM(value) as total_score FROM usage_records "
            "WHERE timestamp BETWEEN ? AND ? AND event_type = 'test_result' "
            "AND (? IS NULL OR session_id = ?) GROUP BY category",
            (period_start, period_end, session_id, session_id),
        )
        tests_passed = 0
        tests_failed = 0
        total_score = 0.0
        for row in cursor.fetchall():
            if row["category"] == "pass":
                tests_passed = row["count"]
                total_score = row["total_score"] or 0.0
            elif row["category"] == "fail":
                tests_failed = row["count"]

        # Session counts
        cursor.execute(
            "SELECT COUNT(*) as count FROM session_summary WHERE start_time BETWEEN ? AND ?",
            (period_start, period_end),
        )
        active_sessions = cursor.fetchone()["count"]

        total_tests = tests_passed + tests_failed
        average_score = total_score / max(total_tests, 1)

        return UsageSummary(
            period_start=period_start,
            period_end=period_end,
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            api_call_count=api_call_count,
            eval_run_count=eval_run_count,
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            total_score=total_score,
            average_score=round(average_score, 4),
            active_sessions=active_sessions,
        )

    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        """Get usage summary for a specific session.

        Args:
            session_id: Session identifier.

        Returns:
            Summary dictionary or empty dict if session not found.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM session_summary WHERE session_id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        if not row:
            return {}

        return {
            "session_id": row["session_id"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "total_input_tokens": row["total_input_tokens"],
            "total_output_tokens": row["total_output_tokens"],
            "api_call_count": row["api_call_count"],
            "tests_passed": row["tests_passed"],
            "tests_failed": row["tests_failed"],
            "total_score": row["total_score"],
            "is_active": bool(row["is_active"]),
        }

    def cleanup_old_records(self, days: int = 30) -> int:
        """Delete records older than the specified number of days.

        Also removes orphaned session_summary entries.

        Args:
            days: Delete records older than this many days.

        Returns:
            Number of records deleted.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        cutoff = time.time() - (days * 86400)

        cursor.execute("DELETE FROM usage_records WHERE timestamp < ?", (cutoff,))
        deleted = cursor.rowcount

        # Clean up orphaned sessions
        cursor.execute(
            "DELETE FROM session_summary WHERE session_id NOT IN "
            "(SELECT DISTINCT session_id FROM usage_records)",
        )
        orphan_sessions = cursor.rowcount
        conn.commit()

        if orphan_sessions:
            logger.info(
                "Cleaned up %d old records and %d orphan session(s)",
                deleted, orphan_sessions,
            )
        else:
            logger.info("Cleaned up %d old records", deleted)
        return deleted


# =============================================================================
# Standalone Functions
# =============================================================================

_global_tracker: Optional[UsageTracker] = None
_global_tracker_lock = threading.RLock()


def get_tracker() -> UsageTracker:
    """Get global tracker instance (thread-safe)."""
    global _global_tracker
    if _global_tracker is None:
        with _global_tracker_lock:
            if _global_tracker is None:
                _global_tracker = UsageTracker()
    return _global_tracker


def record_tokens(**kwargs) -> None:
    """Convenience function to record tokens."""
    return get_tracker().record_tokens(**kwargs)


def record_api_call(**kwargs) -> None:
    """Convenience function to record API call."""
    return get_tracker().record_api_call(**kwargs)


def record_test_result(**kwargs) -> None:
    """Convenience function to record test result."""
    return get_tracker().record_test_result(**kwargs)


def get_usage_summary(**kwargs) -> Dict[str, Any]:
    """Convenience function to get usage summary."""
    return get_tracker().get_summary(**kwargs).to_dict()
