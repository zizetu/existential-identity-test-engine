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
# Original repository: https://github.com/zizetu/tical-agent
#

"""
Task State Machine - Autonomous task lifecycle management
==========================================================

Each task is a state file: tasks/{task_id}/state.json
Atomic write on every step, crash-safe by design.

State transitions:
    pending -> running -> completed
                       -> failed (with error log)
                       -> paused (manual intervention)

Author: Tical (Zize Tu)
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TASK_STATES = ("pending", "running", "completed", "failed", "paused")
MAX_STEP_LIMIT = 500  # safety cap to prevent infinite loops
DEFAULT_TASKS_DIR = "~/.tical-code/tasks"


def _resolve_tasks_dir(workspace: str = "") -> Path:
    """Resolve tasks directory from workspace or default."""
    if workspace:
        return Path(workspace) / "tasks"
    return Path(os.path.expanduser(DEFAULT_TASKS_DIR))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TaskContextWindow:
    """Sliding context window for a task (populated by context_manager in Phase 2)."""
    recent_actions: List[Dict] = field(default_factory=list)
    summary: str = ""
    open_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "recent_actions": self.recent_actions,
            "summary": self.summary,
            "open_files": self.open_files,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "TaskContextWindow":
        return cls(
            recent_actions=d.get("recent_actions", []),
            summary=d.get("summary", ""),
            open_files=d.get("open_files", []),
        )


@dataclass
class TaskState:
    """Complete task state, persisted to disk."""

    task_id: str
    goal: str
    status: str = "pending"  # pending | running | completed | failed | paused
    step: int = 0
    plan: List[str] = field(default_factory=list)
    current_plan_step: int = 0
    context_window: TaskContextWindow = field(default_factory=TaskContextWindow)
    artifacts: List[str] = field(default_factory=list)
    errors: List[Dict] = field(default_factory=list)
    model_family: str = ""
    max_steps: int = 50
    created_at: str = ""
    updated_at: str = ""
    metadata: Dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at

    def is_done(self) -> bool:
        return self.status in ("completed", "failed")

    def is_stuck(self) -> bool:
        """Detect if task is stuck (too many steps, repeated errors)."""
        if self.step >= self.max_steps:
            return True
        # Check for repeated identical errors (3+ in a row)
        if len(self.errors) >= 3:
            last_three = self.errors[-3:]
            if all(e.get("type") == last_three[0].get("type") for e in last_three):
                return True
        return False

    def add_action(self, action: Dict) -> None:
        """Record an action in the context window."""
        self.context_window.recent_actions.append(action)
        # Keep only last 50 actions in memory
        if len(self.context_window.recent_actions) > 50:
            self.context_window.recent_actions = self.context_window.recent_actions[-50:]

    def add_error(self, error_type: str, detail: str, step: int) -> None:
        """Record an error at current step."""
        self.errors.append({
            "type": error_type,
            "detail": detail,
            "step": step,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        # Keep only last 20 errors
        if len(self.errors) > 20:
            self.errors = self.errors[-20:]

    def touch(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "step": self.step,
            "plan": self.plan,
            "current_plan_step": self.current_plan_step,
            "context_window": self.context_window.to_dict(),
            "artifacts": self.artifacts,
            "errors": self.errors,
            "model_family": self.model_family,
            "max_steps": self.max_steps,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "TaskState":
        cw = TaskContextWindow.from_dict(d.get("context_window", {}))
        return cls(
            task_id=d["task_id"],
            goal=d.get("goal", ""),
            status=d.get("status", "pending"),
            step=d.get("step", 0),
            plan=d.get("plan", []),
            current_plan_step=d.get("current_plan_step", 0),
            context_window=cw,
            artifacts=d.get("artifacts", []),
            errors=d.get("errors", []),
            model_family=d.get("model_family", ""),
            max_steps=d.get("max_steps", 50),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            metadata=d.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def create_task(
    goal: str,
    task_id: Optional[str] = None,
    plan: Optional[List[str]] = None,
    model_family: str = "",
    max_steps: int = 50,
    workspace: str = "",
    metadata: Optional[Dict] = None,
) -> TaskState:
    """Create a new task and persist it immediately.

    Args:
        goal: Task goal description (one sentence).
        task_id: Optional ID; auto-generated as t{YYYYMMDD}_{seq} if omitted.
        plan: Optional step-by-step plan.
        model_family: Model family to use (e.g. "mimo", "deepseek").
        max_steps: Maximum steps before forced stop.
        workspace: Project workspace directory.
        metadata: Additional key-value metadata.

    Returns:
        New TaskState (already persisted).
    """
    tasks_dir = _resolve_tasks_dir(workspace)

    if task_id is None:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        seq = _next_sequence(tasks_dir, today)
        task_id = f"t{today}_{seq:03d}"

    state = TaskState(
        task_id=task_id,
        goal=goal,
        status="pending",
        plan=plan or [],
        model_family=model_family,
        max_steps=max_steps,
        metadata=metadata or {},
    )

    save_state(state, workspace)
    logger.info("Task created: %s goal=%r max_steps=%d", task_id, goal[:80], max_steps)
    return state


def load_state(task_id: str, workspace: str = "") -> Optional[TaskState]:
    """Load task state from disk.

    Returns None if task not found or JSON is corrupt.
    """
    tasks_dir = _resolve_tasks_dir(workspace)
    state_file = tasks_dir / task_id / "state.json"

    if not state_file.exists():
        return None

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return TaskState.from_dict(data)
    except (json.JSONDecodeError, KeyError, OSError) as e:
        logger.warning("Failed to load task state %s: %s", task_id, e)
        return None


def save_state(state: TaskState, workspace: str = "") -> bool:
    """Atomically persist task state to disk.

    Write process: tmp file first, then os.rename (atomic on same filesystem).

    Returns True on success.
    """
    tasks_dir = _resolve_tasks_dir(workspace)
    task_dir = tasks_dir / state.task_id
    state_file = task_dir / "state.json"
    tmp_file = task_dir / "state.json.tmp"

    state.touch()

    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.rename(str(tmp_file), str(state_file))
        return True
    except (OSError, IOError) as e:
        logger.error("Failed to save task state %s: %s", state.task_id, e)
        # Clean up tmp
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass
        return False


def list_active_tasks(workspace: str = "") -> List[TaskState]:
    """List all tasks in 'running' or 'pending' status.

    Returns empty list if no active tasks.
    """
    tasks_dir = _resolve_tasks_dir(workspace)
    active = []

    if not tasks_dir.is_dir():
        return active

    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        state_file = task_dir / "state.json"
        if not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if data.get("status") in ("running", "pending"):
                active.append(TaskState.from_dict(data))
        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.debug("Skipping corrupt task dir %s: %s", task_dir.name, e)

    # Sort by created_at ascending (oldest first)
    active.sort(key=lambda s: s.created_at)
    return active


def list_all_tasks(workspace: str = "", limit: int = 50) -> List[Dict]:
    """List all tasks (summary form), most recent first."""
    tasks_dir = _resolve_tasks_dir(workspace)
    result = []

    if not tasks_dir.is_dir():
        return result

    for task_dir in sorted(tasks_dir.iterdir(), reverse=True):
        if not task_dir.is_dir():
            continue
        state_file = task_dir / "state.json"
        if not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            result.append({
                "task_id": data.get("task_id", task_dir.name),
                "goal": data.get("goal", "")[:120],
                "status": data.get("status", "?"),
                "step": data.get("step", 0),
                "errors": len(data.get("errors", [])),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
            })
        except (json.JSONDecodeError, KeyError, OSError):
            pass
        if len(result) >= limit:
            break

    return result


def complete_task(state: TaskState, workspace: str = "") -> bool:
    """Mark task as completed."""
    state.status = "completed"
    return save_state(state, workspace)


def fail_task(state: TaskState, reason: str, workspace: str = "") -> bool:
    """Mark task as failed with a reason."""
    state.status = "failed"
    state.add_error("fatal", reason, state.step)
    return save_state(state, workspace)


def pause_task(state: TaskState, workspace: str = "") -> bool:
    """Pause a task (manual intervention needed)."""
    state.status = "paused"
    return save_state(state, workspace)


def resume_task(task_id: str, workspace: str = "") -> Optional[TaskState]:
    """Resume a paused task."""
    state = load_state(task_id, workspace)
    if state and state.status == "paused":
        state.status = "running"
        save_state(state, workspace)
        return state
    return None


def cleanup_completed(workspace: str = "", max_age_hours: int = 72) -> int:
    """Remove completed/failed task directories older than max_age_hours.

    Returns count of removed tasks.
    """
    tasks_dir = _resolve_tasks_dir(workspace)
    cutoff = time.time() - max_age_hours * 3600
    removed = 0

    if not tasks_dir.is_dir():
        return removed

    for task_dir in tasks_dir.iterdir():
        if not task_dir.is_dir():
            continue
        state_file = task_dir / "state.json"
        if not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if data.get("status") in ("completed", "failed"):
                # Check age via updated_at
                updated = data.get("updated_at", "")
                if updated:
                    try:
                        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                        if dt.timestamp() < cutoff:
                            import shutil
                            shutil.rmtree(str(task_dir), ignore_errors=True)
                            removed += 1
                    except (ValueError, OSError):
                        pass
        except (json.JSONDecodeError, KeyError):
            pass

    if removed:
        logger.info("Cleaned up %d completed task directories", removed)
    return removed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_sequence(tasks_dir: Path, prefix: str) -> int:
    """Find next available sequence number for task ID."""
    seq = 1
    if tasks_dir.is_dir():
        existing = [
            d.name for d in tasks_dir.iterdir()
            if d.is_dir() and d.name.startswith(prefix)
        ]
        if existing:
            nums = []
            for name in existing:
                try:
                    nums.append(int(name.split("_")[-1]))
                except (ValueError, IndexError):
                    pass
            if nums:
                seq = max(nums) + 1
    return seq


def is_task_request(content: str) -> bool:
    """Determine if a message is requesting a task (vs casual conversation).

    Heuristics:
    - Contains [TASK] prefix → explicit task marker
    - Contains multi-step instructions (numbered list, "first... then...")
    - Contains actionable verbs at the start (build, create, fix, deploy, run, refactor)

    Returns True if the message looks like a task request.
    """
    if not content:
        return False
    content_stripped = content.strip()

    # Explicit marker
    if content_stripped.upper().startswith("[TASK]"):
        return True

    # Heuristic: multi-step indicators
    multi_step_markers = [
        "step 1", "first", "1.", "first,",
        "step", "first step",
    ]
    content_lower = content_stripped.lower()
    if any(m in content_lower for m in multi_step_markers):
        return True

    # Heuristic: task verbs at start
    task_verbs = [
        "build", "create", "fix", "deploy", "run", "refactor",
        "migrate", "optimize", "implement", "add", "remove",
        "update", "upgrade", "configure", "setup", "install",
        "upgrade", "configure", "install",
    ]
    first_word = content_lower.split()[0] if content_lower.split() else ""
    if first_word in task_verbs:
        return True

    return False
