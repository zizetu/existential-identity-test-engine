"""Self-evolving engine for EITE-agent.

Singleton SQLite-backed engine that records error patterns and usage insights,
enabling the agent to learn from past failures and successes. Integrates with
the agent's error handling loop and sustained task manager.

Schema:
  error_patterns — tracks recurring errors by type+context hash, auto-resolves
                   when success_rate >= 0.8 (exponential moving average)
  usage_insights — stores categorized success observations with confidence
                   scoring based on evidence count
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = ":memory:"

# ── schema ──────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS error_patterns (
    pattern_id TEXT PRIMARY KEY,
    error_type TEXT NOT NULL,
    trigger_context TEXT NOT NULL,
    frequency INTEGER DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    resolved INTEGER DEFAULT 0,
    resolution TEXT,
    success_rate REAL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_ep_frequency ON error_patterns(frequency DESC);
CREATE INDEX IF NOT EXISTS idx_ep_resolved  ON error_patterns(resolved);
CREATE INDEX IF NOT EXISTS idx_ep_type      ON error_patterns(error_type);

CREATE TABLE IF NOT EXISTS usage_insights (
    insight_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    confidence REAL DEFAULT 0.1,
    evidence_count INTEGER DEFAULT 1,
    created_at REAL NOT NULL,
    last_applied REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ui_category  ON usage_insights(category);
CREATE INDEX IF NOT EXISTS idx_ui_confidence ON usage_insights(confidence DESC);

CREATE TABLE IF NOT EXISTS resolutions (
    resolution_id TEXT PRIMARY KEY,
    error_type TEXT NOT NULL,
    resolution_text TEXT NOT NULL,
    success_rate REAL DEFAULT 0.0,
    times_used INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    last_used REAL
);

CREATE INDEX IF NOT EXISTS idx_res_error_type ON resolutions(error_type);
"""


class SelfEvolveEngine:
    """Singleton engine for tracking error patterns and usage insights."""

    _instance: Optional["SelfEvolveEngine"] = None

    def __new__(cls, db_path: str = DEFAULT_DB_PATH) -> "SelfEvolveEngine":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        if getattr(self, "_initialized", False):
            return
        self._db_path: str = db_path
        self._conn: sqlite3.Connection = sqlite3.connect(
            db_path, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._db_lock: asyncio.Lock = asyncio.Lock()
        self._ensure_db()
        self._initialized: bool = True
        logger.info("SelfEvolveEngine initialized (db=%s)", db_path)

    # ── internals ────────────────────────────────────────────────────────

    @staticmethod
    def _now() -> float:
        return time.time()

    def _ensure_db(self) -> None:
        try:
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.error("Schema creation failed: %s", exc)
            raise

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        try:
            c = self._conn.execute(sql, params)
            self._conn.commit()
            return c
        except sqlite3.Error as exc:
            logger.error("SQL failed: %s | SQL=%.200s", exc, sql)
            raise

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
        try:
            return self._conn.execute(sql, params).fetchone()
        except sqlite3.Error as exc:
            logger.error("Query failed: %s | SQL=%.200s", exc, sql)
            raise

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.error("Query failed: %s | SQL=%.200s", exc, sql)
            raise

    # ── public API ────────────────────────────────────────────────────────

    async def record_error(self, error_type: str, context: dict[str, Any]) -> None:
        """Record an error occurrence, creating or updating a pattern."""
        async with self._db_lock:
            trigger_context: str = json.dumps(context, sort_keys=True, default=str)
            pattern_id: str = str(
                uuid.uuid5(uuid.NAMESPACE_DNS, f"{error_type}:{trigger_context}")
            )
            now: float = self._now()

            existing = self._fetchone(
                "SELECT pattern_id, frequency FROM error_patterns WHERE pattern_id = ?",
                (pattern_id,),
            )

            if existing is not None:
                new_freq: int = existing["frequency"] + 1
                self._execute(
                    "UPDATE error_patterns SET frequency = ?, last_seen = ? WHERE pattern_id = ?",
                    (new_freq, now, pattern_id),
                )
            else:
                self._execute(
                    "INSERT INTO error_patterns "
                    "(pattern_id, error_type, trigger_context, frequency, "
                    "first_seen, last_seen, resolved, resolution, success_rate) "
                    "VALUES (?, ?, ?, 1, ?, ?, 0, NULL, 0.0)",
                    (pattern_id, error_type, trigger_context, now, now),
                )

    async def record_success(self, context: dict[str, Any]) -> None:
        """Record a successful operation. Updates error patterns and insights."""
        async with self._db_lock:
            now: float = self._now()
            trigger_context: str = json.dumps(context, sort_keys=True, default=str)
            category: str = str(context.get("category", "general"))
            description: str = str(context.get("description", ""))

            # ── update success_rate for matching unresolved patterns ──
            matching = self._fetchall(
                "SELECT pattern_id, success_rate FROM error_patterns "
                "WHERE trigger_context = ? AND resolved = 0",
                (trigger_context,),
            )
            for row in matching:
                pid: str = row["pattern_id"]
                old_rate: float = row["success_rate"]
                new_rate: float = old_rate * 0.8 + 1.0 * 0.2  # EMA
                self._execute(
                    "UPDATE error_patterns SET success_rate = ?, last_seen = ? WHERE pattern_id = ?",
                    (new_rate, now, pid),
                )
                if new_rate >= 0.8:
                    self._execute(
                        "UPDATE error_patterns SET resolved = 1, resolution = ? WHERE pattern_id = ?",
                        ("Auto-resolved: success rate threshold reached", pid),
                    )
                    logger.info("Pattern %s auto-resolved (rate=%.2f)", pid, new_rate)

            # ── upsert usage insight ──
            insight_id: str = str(
                uuid.uuid5(uuid.NAMESPACE_DNS, f"{category}:{description}")
            )
            existing = self._fetchone(
                "SELECT evidence_count FROM usage_insights WHERE insight_id = ?",
                (insight_id,),
            )

            if existing is not None:
                new_evidence: int = existing["evidence_count"] + 1
                new_confidence: float = min(1.0, new_evidence / 10.0)
                self._execute(
                    "UPDATE usage_insights SET evidence_count = ?, confidence = ?, "
                    "last_applied = ? WHERE insight_id = ?",
                    (new_evidence, new_confidence, now, insight_id),
                )
            else:
                self._execute(
                    "INSERT INTO usage_insights "
                    "(insight_id, category, description, confidence, "
                    "evidence_count, created_at, last_applied) "
                    "VALUES (?, ?, ?, 0.1, 1, ?, ?)",
                    (insight_id, category, description, now, now),
                )

    async def get_frequent_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most frequent unresolved error patterns."""
        async with self._db_lock:
            rows = self._fetchall(
                "SELECT pattern_id, error_type, trigger_context, frequency, "
                "first_seen, last_seen, resolved, resolution, success_rate "
                "FROM error_patterns WHERE resolved = 0 "
                "ORDER BY frequency DESC, last_seen DESC LIMIT ?",
                (limit,),
            )
            return [
                {
                    "pattern_id": r["pattern_id"],
                    "error_type": r["error_type"],
                    "trigger_context": r["trigger_context"],
                    "frequency": r["frequency"],
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "resolved": bool(r["resolved"]),
                    "resolution": r["resolution"],
                    "success_rate": r["success_rate"],
                }
                for r in rows
            ]

    async def get_suggestions(self, min_frequency: int = 3) -> list[str]:
        """Generate human-readable suggestions from frequent error patterns."""
        suggestions: list[str] = []
        try:
            rows = self._fetchall(
                "SELECT error_type, frequency FROM error_patterns "
                "WHERE frequency >= ? ORDER BY frequency DESC",
                (min_frequency,),
            )
            for r in rows:
                et: str = r["error_type"]
                freq: int = r["frequency"]
                lowered: str = et.lower()

                if "timeout" in lowered:
                    suggestions.append(
                        f"Consider switching to faster model for '{et}' (×{freq})"
                    )
                elif "memory" in lowered or "oom" in lowered:
                    suggestions.append(
                        f"Reduce batch size or input length for '{et}' (×{freq})"
                    )
                elif "rate" in lowered or "429" in lowered:
                    suggestions.append(
                        f"Implement rate-limiting or retry logic for '{et}' (×{freq})"
                    )
                elif "auth" in lowered or "401" in lowered or "403" in lowered:
                    suggestions.append(
                        f"Review API credentials for '{et}' (×{freq})"
                    )
                elif "connection" in lowered or "network" in lowered:
                    suggestions.append(
                        f"Add retry with exponential backoff for '{et}' (×{freq})"
                    )
                elif "parse" in lowered or "json" in lowered or "format" in lowered:
                    suggestions.append(
                        f"Add input validation before processing '{et}' (×{freq})"
                    )
                else:
                    suggestions.append(
                        f"Review and handle '{et}' more robustly (×{freq})"
                    )
        except sqlite3.Error as exc:
            logger.error("get_suggestions error: %s", exc)
        return suggestions

    async def get_insights(
        self, category: Optional[str] = None, min_confidence: float = 0.3
    ) -> list[dict[str, Any]]:
        """Return usage insights, optionally filtered."""
        async with self._db_lock:
            if category:
                rows = self._fetchall(
                    "SELECT category, description, confidence, evidence_count, "
                    "created_at, last_applied FROM usage_insights "
                    "WHERE category = ? AND confidence >= ? "
                    "ORDER BY confidence DESC, last_applied DESC",
                    (category, min_confidence),
                )
            else:
                rows = self._fetchall(
                    "SELECT category, description, confidence, evidence_count, "
                    "created_at, last_applied FROM usage_insights "
                    "WHERE confidence >= ? "
                    "ORDER BY confidence DESC, last_applied DESC",
                    (min_confidence,),
                )
            return [
                {
                    "category": r["category"],
                    "description": r["description"],
                    "confidence": r["confidence"],
                    "evidence_count": r["evidence_count"],
                    "created_at": r["created_at"],
                    "last_applied": r["last_applied"],
                }
                for r in rows
            ]

    async def cleanup_old_records(self, days: int = 90) -> int:
        """Delete rows older than *days* from all tracked tables."""
        cutoff: float = self._now() - days * 86400
        total: int = 0
        # table → time_column mapping
        cleanup_map: dict[str, str] = {
            "error_patterns": "last_seen",
            "usage_insights": "last_applied",
            "resolutions": "last_used",
        }
        for table, col in cleanup_map.items():
            try:
                c = self._execute(
                    f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)
                )
                deleted: int = c.rowcount
                total += deleted
                if deleted:
                    logger.debug("Cleaned %d old records from %s", deleted, table)
            except sqlite3.Error as exc:
                logger.warning("Cleanup failed for %s: %s", table, exc)
        return total

    async def get_stats(self) -> dict[str, Any]:
        """Return summary statistics for monitoring."""
        async with self._db_lock:
            total_errors = self._fetchone(
                "SELECT COUNT(*) AS c FROM error_patterns"
            )["c"]
            unresolved = self._fetchone(
                "SELECT COUNT(*) AS c FROM error_patterns WHERE resolved = 0"
            )["c"]
            total_insights = self._fetchone(
                "SELECT COUNT(*) AS c FROM usage_insights"
            )["c"]
            high_conf = self._fetchone(
                "SELECT COUNT(*) AS c FROM usage_insights WHERE confidence >= 0.7"
            )["c"]
            return {
                "total_error_patterns": total_errors,
                "unresolved": unresolved,
                "resolution_rate": round(
                    (total_errors - unresolved) / max(total_errors, 1) * 100, 1
                ),
                "total_insights": total_insights,
                "high_confidence_insights": high_conf,
            }
