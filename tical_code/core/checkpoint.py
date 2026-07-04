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

"""
EITE Checkpoint - Evaluation State Persistence
================================================

Saves and restores evaluation state: current benchmark, test progress,
per-test results, partial scores, and session context. Used for resuming
interrupted evaluation runs and for crash recovery.

Core design (EITE evaluation context):
- Tracks evaluation state (active benchmark, current test index, pass/fail counts)
- Persists per-test results and scores to disk as JSON
- Supports resume: reload evaluation state after worker restart
- No file-level snapshots (EITE does not snapshot workspace files)
- No conversation state (EITE evaluations are stateless between test cases)
- Integrity checksum to detect tampered state files

State content:
1. Evaluation metadata: benchmark name, version, start time
2. Test progress: current test index, total tests, completed count
3. Results: list of per-test results (pass/fail, score, metrics)
4. Configuration: the evaluation config used for this run

Author: EITE Team
"""
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("eite-agent.checkpoint")


# =============================================================================
# Constants
# =============================================================================

# EITE checkpoint directory name
CHECKPOINT_DIR = ".eite-checkpoints"

# Default exclude patterns for state files
DEFAULT_EXCLUDES = {
    ".git", ".env", "node_modules", "__pycache__", ".venv",
    "venv", ".tox", ".mypy_cache", ".pytest_cache",
    "*.pyc", ".DS_Store",
}


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class TestResult:
    """Result of a single evaluation test case.

    Attributes:
        test_id: Unique identifier for this test case.
        test_name: Human-readable test name.
        passed: Whether the test passed.
        score: Numeric score (0.0 to 1.0, or raw score).
        metrics: Dict of additional metrics (latency, tokens, etc.).
        error: Error message if the test failed due to an exception.
        output: Model output for this test case.
        expected: Expected output (if applicable).
        timestamp: When the test was executed.
    """
    test_id: str
    test_name: str
    passed: bool = False
    score: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    output: str = ""
    expected: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "test_id": self.test_id,
            "test_name": self.test_name,
            "passed": self.passed,
            "score": self.score,
            "metrics": self.metrics,
            "error": self.error[:500] if self.error else "",
            "timestamp": self.timestamp,
        }


@dataclass
class EvalState:
    """Complete evaluation state snapshot.

    Attributes:
        eval_id: Unique evaluation run ID (UUID).
        timestamp: When this snapshot was created.
        benchmark: Name of the benchmark being evaluated.
        benchmark_version: Version of the benchmark.
        config: Evaluation configuration dict.
        current_index: Current test case index (0-based).
        total_tests: Total number of test cases.
        completed: Number of completed test cases.
        passed: Number of passed test cases.
        total_score: Accumulated score.
        results: List of completed test results.
        session_id: Associated session ID (if any).
        integrity_hash: SHA-256 checksum for tamper detection.
    """
    eval_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    benchmark: str = ""
    benchmark_version: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    current_index: int = 0
    total_tests: int = 0
    completed: int = 0
    passed: int = 0
    total_score: float = 0.0
    results: List[TestResult] = field(default_factory=list)
    session_id: str = ""
    status: str = "in_progress"  # in_progress | completed | aborted
    integrity_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "eval_id": self.eval_id,
            "timestamp": self.timestamp,
            "benchmark": self.benchmark,
            "benchmark_version": self.benchmark_version,
            "current_index": self.current_index,
            "total_tests": self.total_tests,
            "completed": self.completed,
            "passed": self.passed,
            "total_score": self.total_score,
            "result_count": len(self.results),
            "status": self.status,
            "integrity_hash": self.integrity_hash[:16] if self.integrity_hash else "",
        }


@dataclass
class CheckpointConfig:
    """Evaluation checkpoint configuration.

    Attributes:
        enabled: Whether checkpoints are enabled.
        max_checkpoints: Maximum number of checkpoints to keep.
        workspace: Working directory for checkpoint storage.
        verify_integrity: Whether to verify checksum on load.
    """
    enabled: bool = True
    max_checkpoints: int = 20
    workspace: str = "."
    verify_integrity: bool = True


# =============================================================================
# CheckpointManager - Evaluation state persistence
# =============================================================================

class CheckpointManager:
    """Manages evaluation state checkpoints for resuming and recovery.

    EITE-specific: saves benchmark progress and per-test results as JSON,
    not file-system snapshots. Supports resuming an interrupted evaluation
    from the last checkpoint.

    Usage:
        mgr = CheckpointManager(CheckpointConfig(workspace="."))
        mgr.save(eval_state)
        # ... later, after restart ...
        state = mgr.load(eval_id)
    """

    def __init__(self, config: Optional[CheckpointConfig] = None):
        self.config = config or CheckpointConfig()
        self._checkpoints: List[EvalState] = []
        workspace = os.path.abspath(self.config.workspace)
        self._storage_dir = os.path.join(workspace, CHECKPOINT_DIR)
        self._load_checkpoints_from_disk()

    def _load_checkpoints_from_disk(self) -> None:
        """Rebuild in-memory checkpoint index from disk."""
        if not os.path.isdir(self._storage_dir):
            return
        loaded = 0
        for cp_id in os.listdir(self._storage_dir):
            meta_path = os.path.join(self._storage_dir, cp_id, "meta.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                cp = EvalState(
                    eval_id=meta["eval_id"],
                    timestamp=meta.get("timestamp", 0),
                    benchmark=meta.get("benchmark", ""),
                    benchmark_version=meta.get("benchmark_version", ""),
                    config=meta.get("config", {}),
                    current_index=meta.get("current_index", 0),
                    total_tests=meta.get("total_tests", 0),
                    completed=meta.get("completed", 0),
                    passed=meta.get("passed", 0),
                    total_score=meta.get("total_score", 0.0),
                    session_id=meta.get("session_id", ""),
                    status=meta.get("status", "in_progress"),
                    integrity_hash=meta.get("integrity_hash", ""),
                )
                self._checkpoints.append(cp)
                loaded += 1
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.warning("Failed to load checkpoint %s: %s", cp_id, e)
        if loaded:
            logger.info("Loaded %d evaluation checkpoints from disk", loaded)

    def save(self, state: EvalState) -> str:
        """Persist evaluation state to disk.

        Args:
            state: The evaluation state to save.

        Returns:
            The eval_id of the saved checkpoint.
        """
        if not self.config.enabled:
            logger.debug("Checkpoint: feature not enabled")
            return ""

        # Compute integrity hash
        state.integrity_hash = self._compute_integrity_hash(state)

        # Write to disk
        self._persist_checkpoint(state)

        # Update in-memory index
        existing = [c for c in self._checkpoints if c.eval_id == state.eval_id]
        if existing:
            self._checkpoints.remove(existing[0])
        self._checkpoints.append(state)

        logger.info(
            "Checkpoint saved: eval=%s benchmark=%s progress=%d/%d passed=%d score=%.2f",
            state.eval_id, state.benchmark,
            state.completed, state.total_tests, state.passed, state.total_score,
        )
        return state.eval_id

    def load(self, eval_id: str) -> Optional[EvalState]:
        """Load evaluation state from a persisted checkpoint.

        Args:
            eval_id: The evaluation ID to load.

        Returns:
            EvalState if found, None otherwise.
        """
        cp_dir = os.path.join(self._storage_dir, eval_id)
        meta_path = os.path.join(cp_dir, "meta.json")
        results_path = os.path.join(cp_dir, "results.json")

        if not os.path.isfile(meta_path):
            logger.warning("Checkpoint not found: %s", eval_id)
            return None

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load checkpoint meta: %s", e)
            return None

        # Integrity check
        if self.config.verify_integrity and meta.get("integrity_hash"):
            stored_hash = meta["integrity_hash"]
            meta_no_hash = dict(meta)
            meta_no_hash["integrity_hash"] = ""
            # Recompute against loaded state (without results)
            check_hash = self._compute_hash_from_meta(meta_no_hash)
            if check_hash != stored_hash:
                logger.error(
                    "Checkpoint integrity check failed for %s: expected=%s actual=%s",
                    eval_id, stored_hash[:16], check_hash[:16],
                )
                return None

        state = EvalState(
            eval_id=meta["eval_id"],
            timestamp=meta.get("timestamp", 0),
            benchmark=meta.get("benchmark", ""),
            benchmark_version=meta.get("benchmark_version", ""),
            config=meta.get("config", {}),
            current_index=meta.get("current_index", 0),
            total_tests=meta.get("total_tests", 0),
            completed=meta.get("completed", 0),
            passed=meta.get("passed", 0),
            total_score=meta.get("total_score", 0.0),
            session_id=meta.get("session_id", ""),
            status=meta.get("status", "in_progress"),
            integrity_hash=stored_hash,
        )

        # Load results
        if os.path.isfile(results_path):
            try:
                with open(results_path, "r", encoding="utf-8") as f:
                    results_data = json.load(f)
                for rd in results_data:
                    state.results.append(TestResult(
                        test_id=rd["test_id"],
                        test_name=rd.get("test_name", ""),
                        passed=rd.get("passed", False),
                        score=rd.get("score", 0.0),
                        metrics=rd.get("metrics", {}),
                        error=rd.get("error", ""),
                        timestamp=rd.get("timestamp", 0),
                    ))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load checkpoint results: %s", e)

        logger.info(
            "Checkpoint loaded: eval=%s benchmark=%s progress=%d/%d",
            eval_id, state.benchmark, state.completed, state.total_tests,
        )
        return state

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        """List all checkpoints with summary info."""
        return [cp.to_dict() for cp in self._checkpoints]

    def delete(self, eval_id: str) -> bool:
        """Delete a checkpoint by eval_id.

        Args:
            eval_id: The evaluation ID to delete.

        Returns:
            True if deleted, False if not found.
        """
        cp_dir = os.path.join(self._storage_dir, eval_id)
        if os.path.isdir(cp_dir):
            shutil.rmtree(cp_dir)
        self._checkpoints = [c for c in self._checkpoints if c.eval_id != eval_id]
        logger.info("Checkpoint deleted: %s", eval_id)
        return True

    def prune(self, keep: int = 10) -> int:
        """Remove old checkpoints, keeping only the most recent N.

        Args:
            keep: Number of recent checkpoints to keep.

        Returns:
            Number of deleted checkpoints.
        """
        sorted_cps = sorted(
            self._checkpoints, key=lambda c: c.timestamp, reverse=True
        )
        to_delete = sorted_cps[keep:]
        for cp in to_delete:
            self.delete(cp.eval_id)
        return len(to_delete)

    def reset(self) -> None:
        """Reset all checkpoint state."""
        self._checkpoints.clear()

    # =========================================================================
    # Internal methods
    # =========================================================================

    def _persist_checkpoint(self, state: EvalState) -> None:
        """Write checkpoint to disk as JSON files."""
        cp_dir = os.path.join(self._storage_dir, state.eval_id)
        os.makedirs(cp_dir, exist_ok=True)

        # Write meta (without integrity_hash for computation, then with it)
        meta = state.to_dict()
        meta["benchmark_version"] = state.benchmark_version
        meta["config"] = state.config
        meta["current_index"] = state.current_index
        meta["total_tests"] = state.total_tests
        meta["completed"] = state.completed
        meta["passed"] = state.passed
        meta["total_score"] = state.total_score
        meta["session_id"] = state.session_id
        meta["status"] = state.status
        meta["integrity_hash"] = state.integrity_hash

        meta_path = os.path.join(cp_dir, "meta.json")
        # Atomic write
        tmp = meta_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            os.replace(tmp, meta_path)
        except OSError as e:
            logger.error("Failed to persist checkpoint: %s", e)
            raise

        # Write results separately
        results_path = os.path.join(cp_dir, "results.json")
        tmp = results_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    [r.to_dict() for r in state.results],
                    f, ensure_ascii=False, indent=2,
                )
            os.replace(tmp, results_path)
        except OSError as e:
            logger.error("Failed to persist checkpoint results: %s", e)

        # Prune old checkpoints
        self.prune(keep=self.config.max_checkpoints)

    @staticmethod
    def _compute_integrity_hash(state: EvalState) -> str:
        """Compute SHA-256 integrity hash for an evaluation state."""
        parts = [
            state.eval_id,
            str(state.timestamp),
            state.benchmark,
            str(state.current_index),
            str(state.total_tests),
            str(state.completed),
            str(state.passed),
            str(state.total_score),
        ]
        combined = "|".join(parts)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    @staticmethod
    def _compute_hash_from_meta(meta: Dict[str, Any]) -> str:
        """Compute SHA-256 integrity hash from meta dict (no results)."""
        parts = [
            meta.get("eval_id", ""),
            str(meta.get("timestamp", 0)),
            meta.get("benchmark", ""),
            str(meta.get("current_index", 0)),
            str(meta.get("total_tests", 0)),
            str(meta.get("completed", 0)),
            str(meta.get("passed", 0)),
            str(meta.get("total_score", 0.0)),
        ]
        combined = "|".join(parts)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()
