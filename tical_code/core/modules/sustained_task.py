"""Persistent task queue with auto-recovery for EITElite.

This module provides a SQLite-backed task queue that survives process
restarts. Tasks transition through a well-defined state machine and
support automatic retries with exponential backoff.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


class TaskState(Enum):
    """Lifecycle states for a sustained task."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


@dataclass
class TaskRecord:
    """Persistent representation of a single task."""

    task_id: str
    created_at: float
    updated_at: float
    state: TaskState
    description: str
    current_step: int = 0
    total_steps: int = 0
    max_retries: int = 3
    retry_count: int = 0
    last_error: Optional[str] = None
    result: Optional[str] = None
    metadata: str = "{}"

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = asdict(self)
        data["state"] = self.state.value
        try:
            data["metadata"] = json.loads(self.metadata) if self.metadata else {}
        except (ValueError, TypeError):
            data["metadata"] = {}
        return data

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskRecord":
        return cls(
            task_id=row["task_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            state=TaskState(row["state"]),
            description=row["description"],
            current_step=int(row["current_step"] or 0),
            total_steps=int(row["total_steps"] or 0),
            max_retries=int(row["max_retries"] or 3),
            retry_count=int(row["retry_count"] or 0),
            last_error=row["last_error"],
            result=row["result"],
            metadata=row["metadata"] if row["metadata"] else "{}",
        )


class SustainedTaskManager:
    """Singleton manager for persistent, recoverable tasks."""

    _instance: Optional["SustainedTaskManager"] = None

    def __new__(cls, *args: Any, **kwargs: Any) -> "SustainedTaskManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: str = "~/.EITElite/sustained_tasks.db") -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized: bool = True
        self._db_path: str = os.path.expanduser(db_path)
        parent = os.path.dirname(self._db_path)
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection = sqlite3.connect(
            self._db_path, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._db_lock: asyncio.Lock = asyncio.Lock()
        self._task_locks: Dict[str, asyncio.Lock] = {}
        self._locks_guard: asyncio.Lock = asyncio.Lock()
        self._init_schema()

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (primarily for testing)."""
        cls._instance = None

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    created_at REAL,
                    updated_at REAL,
                    state TEXT,
                    description TEXT,
                    current_step INTEGER DEFAULT 0,
                    total_steps INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    retry_count INTEGER DEFAULT 0,
                    last_error TEXT,
                    result TEXT,
                    metadata TEXT DEFAULT '{}'
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at)"
            )

    async def _get_lock(self, task_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            if task_id not in self._task_locks:
                self._task_locks[task_id] = asyncio.Lock()
            return self._task_locks[task_id]

    @staticmethod
    def _now() -> float:
        return time.time()

    async def _execute(self, query: str, params: Tuple[Any, ...] = ()) -> sqlite3.Cursor:
        async with self._db_lock:
            return self._conn.execute(query, params)

    async def _commit(self) -> None:
        async with self._db_lock:
            self._conn.commit()

    async def _fetchone(
        self, query: str, params: Tuple[Any, ...] = ()
    ) -> Optional[sqlite3.Row]:
        async with self._db_lock:
            return self._conn.execute(query, params).fetchone()

    async def _fetchall(
        self, query: str, params: Tuple[Any, ...] = ()
    ) -> List[sqlite3.Row]:
        async with self._db_lock:
            return self._conn.execute(query, params).fetchall()

    async def submit(
        self,
        description: str,
        max_retries: int = 3,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Submit a new pending task and return its identifier."""
        task_id = str(uuid.uuid4())
        now = self._now()
        meta_str = json.dumps(metadata) if metadata else "{}"
        await self._execute(
            """
            INSERT INTO tasks
            (task_id, created_at, updated_at, state, description,
             current_step, total_steps, max_retries, retry_count,
             last_error, result, metadata)
            VALUES (?, ?, ?, ?, ?, 0, 0, ?, 0, NULL, NULL, ?)
            """,
            (
                task_id,
                now,
                now,
                TaskState.PENDING.value,
                description,
                max_retries,
                meta_str,
            ),
        )
        await self._commit()
        return task_id

    async def get_status(self, task_id: str) -> Optional[TaskRecord]:
        """Return the current record for a task, or None if missing."""
        row = await self._fetchone(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        )
        if row is None:
            return None
        return TaskRecord.from_row(row)

    async def list_tasks(
        self, state: Optional[TaskState] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """List tasks optionally filtered by state, newest first."""
        if limit <= 0:
            return []
        if state is not None:
            rows = await self._fetchall(
                "SELECT * FROM tasks WHERE state = ? ORDER BY created_at DESC LIMIT ?",
                (state.value, limit),
            )
        else:
            rows = await self._fetchall(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [TaskRecord.from_row(r).to_dict() for r in rows]

    async def recover_pending_tasks(self) -> List[TaskRecord]:
        """Recover tasks left in RUNNING or RETRYING state after a crash.

        Such tasks are reset to PENDING so they can be re-executed.
        Returns the list of recovered task records.
        """
        recoverable: List[TaskRecord] = []
        now = self._now()
        for target_state in (TaskState.RUNNING, TaskState.RETRYING):
            rows = await self._fetchall(
                "SELECT * FROM tasks WHERE state = ?",
                (target_state.value,),
            )
            for row in rows:
                await self._execute(
                    "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                    (TaskState.PENDING.value, now, row["task_id"]),
                )
                recoverable.append(TaskRecord.from_row(row))
        await self._commit()
        return recoverable

    async def _update_state(
        self,
        task_id: str,
        state: TaskState,
        last_error: Optional[str] = None,
        result: Optional[str] = None,
        retry_count: Optional[int] = None,
        current_step: Optional[int] = None,
        total_steps: Optional[int] = None,
    ) -> None:
        fields: List[str] = ["state = ?", "updated_at = ?"]
        params: List[Any] = [state.value, self._now()]
        if last_error is not None:
            fields.append("last_error = ?")
            params.append(last_error)
        if result is not None:
            fields.append("result = ?")
            params.append(result)
        if retry_count is not None:
            fields.append("retry_count = ?")
            params.append(retry_count)
        if current_step is not None:
            fields.append("current_step = ?")
            params.append(current_step)
        if total_steps is not None:
            fields.append("total_steps = ?")
            params.append(total_steps)
        params.append(task_id)
        await self._execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?",
            tuple(params),
        )
        await self._commit()

    async def _handle_failure(self, task_id: str, error_message: str) -> None:
        """Apply retry-with-backoff or fail permanently based on retry budget."""
        record = await self.get_status(task_id)
        if record is None:
            return
        new_retry_count: int = record.retry_count + 1
        if new_retry_count < record.max_retries:
            backoff: float = float(2 ** new_retry_count)
            await self._update_state(
                task_id,
                TaskState.RETRYING,
                last_error=error_message,
                retry_count=new_retry_count,
            )
            await asyncio.sleep(backoff)
            await self._update_state(task_id, TaskState.PENDING)
        else:
            await self._update_state(
                task_id,
                TaskState.FAILED,
                last_error=error_message,
                retry_count=new_retry_count,
            )

    async def run_task(
        self,
        task_id: str,
        coro_factory: Callable[[], Awaitable[Dict[str, Any]]],
        timeout: float = 300.0,
    ) -> Dict[str, Any]:
        """Execute a task with timeout, retry, and persistence semantics.

        Returns a dict describing the outcome. Raises on unrecoverable
        failure or timeout so callers can react; the database always
        reflects the final persisted state.
        """
        lock = await self._get_lock(task_id)
        async with lock:
            record = await self.get_status(task_id)
            if record is None:
                raise ValueError(f"Task not found: {task_id}")
            if record.state == TaskState.COMPLETED:
                return {
                    "task_id": task_id,
                    "status": "already_completed",
                    "result": record.result,
                }
            await self._update_state(task_id, TaskState.RUNNING)
            try:
                result: Dict[str, Any] = await asyncio.wait_for(
                    coro_factory(), timeout=timeout
                )
            except asyncio.TimeoutError as exc:
                err_msg = f"Timeout after {timeout}s: {exc}"
                await self._handle_failure(task_id, err_msg)
                raise asyncio.TimeoutError(err_msg) from exc
            except asyncio.CancelledError:
                await self._update_state(
                    task_id,
                    TaskState.PENDING,
                    last_error="Cancelled before completion",
                )
                raise
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
                await self._handle_failure(task_id, err_msg)
                raise

            result_str: str
            if isinstance(result, (dict, list)):
                result_str = json.dumps(result)
            elif isinstance(result, str):
                result_str = result
            else:
                result_str = json.dumps({"value": str(result)})
            await self._update_state(
                task_id,
                TaskState.COMPLETED,
                result=result_str,
                last_error=None,
            )
            return {"task_id": task_id, "status": "completed", "result": result}

    async def cancel_task(self, task_id: str) -> bool:
        """Mark a non-terminal task as cancelled (FAILED).

        Returns True if the task was cancelled, False if it was missing
        or already in a terminal state.
        """
        lock = await self._get_lock(task_id)
        async with lock:
            record = await self.get_status(task_id)
            if record is None:
                return False
            if record.state in (TaskState.COMPLETED, TaskState.FAILED):
                return False
            await self._update_state(
                task_id,
                TaskState.FAILED,
                last_error="Cancelled by user",
            )
            return True

    def cleanup_old_tasks(self, days: int = 30) -> int:
        """Delete completed/failed tasks older than ``days`` days.

        Returns the number of deleted rows. Synchronous because it is
        typically run as a maintenance operation outside the event loop.
        """
        if days < 0:
            return 0
        cutoff: float = self._now() - (float(days) * 86400.0)
        with self._conn:
            cursor = self._conn.execute(
                """
                DELETE FROM tasks
                WHERE updated_at < ?
                AND state IN (?, ?)
                """,
                (
                    cutoff,
                    TaskState.COMPLETED.value,
                    TaskState.FAILED.value,
                ),
            )
            return int(cursor.rowcount)

    def close(self) -> None:
        """Close the underlying database connection."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass