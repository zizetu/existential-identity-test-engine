"""Hunk-based file version tracker (Grok Build distillation).

Tracks every write/patch as a unified-diff hunk per file per task turn,
stored in SQLite. Supports history queries, rollback to any prior hunk,
and a git-aware baseline when the file is tracked by git.

Compatible with the legacy file_state API: record(), get_touched(), clear_task().

Storage layout:
  ~/.tical/tasks/<task_id>/hunks.db

Zero external dependencies (stdlib only: sqlite3, difflib, pathlib, threading).
"""

from __future__ import annotations

import difflib
import logging
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hunks (
    id INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    diff TEXT NOT NULL,
    author TEXT NOT NULL,
    parent_id INTEGER,
    content_before TEXT,
    content_after TEXT,
    action TEXT DEFAULT 'write',
    size INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hunks_file ON hunks(file_path);
CREATE INDEX IF NOT EXISTS idx_hunks_turn ON hunks(turn_id);
"""

# Module-level trackers: task_id -> HunkTracker
_trackers: Dict[str, "HunkTracker"] = {}
_trackers_lock = threading.Lock()

# In-memory touched index for get_touched compatibility (mirrors latest action)
_touched: Dict[str, Dict[str, dict]] = {}
_touched_lock = threading.Lock()


def _default_db_path(task_id: str) -> Path:
    """Resolve ~/.tical/tasks/<task_id>/hunks.db (or TICAL_HOME/tasks/...)."""
    home = os.environ.get("TICAL_HOME") or os.environ.get("EITE_DATA_DIR")
    if home:
        base = Path(home).expanduser()
    else:
        # Prefer ~/.tical per distillation spec; fall back is fine on Windows
        base = Path.home() / ".tical"
    path = base / "tasks" / str(task_id)
    path.mkdir(parents=True, exist_ok=True)
    return path / "hunks.db"


def _read_text_safe(path: Path) -> str:
    """Read file as UTF-8 text; return empty string if missing/unreadable."""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.debug("hunk_tracker: read failed %s: %s", path, e)
    return ""


def _git_head_content(file_path: str) -> Optional[str]:
    """Return file content at HEAD if the path is git-tracked, else None."""
    try:
        p = Path(file_path).resolve()
        # Find repo root
        proc = subprocess.run(
            ["git", "-C", str(p.parent), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return None
        root = Path(proc.stdout.strip())
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            return None
        show = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{rel}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if show.returncode != 0:
            return None
        return show.stdout
    except Exception as e:
        logger.debug("hunk_tracker: git baseline failed for %s: %s", file_path, e)
        return None


def _unified_diff(old: str, new: str, file_path: str) -> str:
    """Generate unified diff text from old -> new content."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    # Ensure trailing newline consistency for clean diffs
    if old and not old.endswith("\n"):
        old_lines = old.splitlines(keepends=True)
        if old_lines and not old_lines[-1].endswith("\n"):
            old_lines[-1] = old_lines[-1] + "\n"
    if new and not new.endswith("\n"):
        new_lines = new.splitlines(keepends=True)
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
    diff_iter = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="\n",
        n=3,
    )
    return "".join(diff_iter)


class HunkTracker:
    """Per-task hunk version tracker with SQLite persistence."""

    def __init__(self, task_id: str, db_path: Optional[str] = None):
        self.task_id = str(task_id)
        self.db_path = str(db_path) if db_path else str(_default_db_path(self.task_id))
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception as e:
                    logger.debug("hunk_tracker: close failed: %s", e)
                finally:
                    self._conn = None

    def _latest_hunk_id(self, file_path: str) -> Optional[int]:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT id FROM hunks WHERE file_path = ? ORDER BY id DESC LIMIT 1",
            (file_path,),
        ).fetchone()
        return int(row["id"]) if row else None

    def _latest_content(self, file_path: str) -> Optional[str]:
        """Best-effort prior content: last hunk content_after, else git HEAD, else disk."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT content_after FROM hunks WHERE file_path = ? "
            "AND content_after IS NOT NULL ORDER BY id DESC LIMIT 1",
            (file_path,),
        ).fetchone()
        if row and row["content_after"] is not None:
            return row["content_after"]
        git_content = _git_head_content(file_path)
        if git_content is not None:
            return git_content
        p = Path(file_path)
        if p.is_file():
            return _read_text_safe(p)
        return None

    def record(
        self,
        file_path: str,
        action: str = "write",
        old_content: Optional[str] = None,
        new_content: Optional[str] = None,
        author: str = "unknown",
        turn_id: Optional[str] = None,
        size: int = 0,
    ) -> Optional[int]:
        """Record a file write/patch as a hunk.

        Args:
            file_path: Absolute or normalized path of the file.
            action: One of read/write/patch (reads are tracked lightly).
            old_content: Content before the change (optional; auto-detected).
            new_content: Content after the change (optional; read from disk).
            author: Tool name that performed the change.
            turn_id: Logical turn identifier (defaults to timestamp string).
            size: Byte/char size for file_state compatibility.

        Returns:
            New hunk id, or None for non-mutating actions without content.
        """
        path = str(Path(file_path).expanduser())
        try:
            path = str(Path(path).resolve())
        except Exception:
            path = str(Path(file_path).expanduser())

        turn = turn_id or os.environ.get("TICAL_TURN_ID") or f"turn-{time.time():.6f}"
        action = (action or "write").lower()

        # Reads: update touched index only (no hunk)
        if action == "read":
            self._mark_touched(path, action, size)
            return None

        with self._lock:
            if self._conn is None:
                self._init_db()
            assert self._conn is not None

            if old_content is None:
                prev = self._latest_content(path)
                if prev is None:
                    # First mutation: prefer git HEAD as baseline
                    prev = _git_head_content(path)
                if prev is None:
                    prev = _read_text_safe(Path(path)) if Path(path).is_file() else ""
                # If we already have disk content equal to new and no prior hunk,
                # disk may already be written — caller should pass old_content.
                old_content = prev if prev is not None else ""

            if new_content is None:
                new_content = _read_text_safe(Path(path)) if Path(path).is_file() else ""

            if old_content == new_content and action in ("write", "patch"):
                # Still record for audit if explicit mutation API was used
                pass

            diff_text = _unified_diff(old_content, new_content, path)
            if not diff_text and old_content != new_content:
                # Binary-ish or empty-line edge case: store a marker
                diff_text = f"--- a/{path}\n+++ b/{path}\n@@ content changed (no line diff) @@\n"

            parent_id = self._latest_hunk_id(path)
            if size <= 0:
                size = len(new_content.encode("utf-8", errors="replace"))

            cur = self._conn.execute(
                "INSERT INTO hunks "
                "(file_path, turn_id, timestamp, diff, author, parent_id, "
                " content_before, content_after, action, size) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    path,
                    turn,
                    time.time(),
                    diff_text,
                    author or "unknown",
                    parent_id,
                    old_content,
                    new_content,
                    action,
                    size,
                ),
            )
            self._conn.commit()
            hunk_id = int(cur.lastrowid)
            self._mark_touched(path, action, size)
            logger.debug(
                "hunk recorded: id=%s path=%s action=%s author=%s",
                hunk_id, path, action, author,
            )
            return hunk_id

    def _mark_touched(self, path: str, action: str, size: int) -> None:
        with _touched_lock:
            if self.task_id not in _touched:
                _touched[self.task_id] = {}
            _touched[self.task_id][path] = {
                "action": action,
                "size": size,
                "timestamp": time.time(),
            }

    def get_history(self, file_path: str) -> List[Dict[str, Any]]:
        """Return hunk history for a file, oldest first."""
        path = str(Path(file_path).expanduser())
        try:
            path = str(Path(path).resolve())
        except Exception:
            pass
        with self._lock:
            if self._conn is None:
                return []
            rows = self._conn.execute(
                "SELECT id, file_path, turn_id, timestamp, diff, author, "
                "parent_id, action, size FROM hunks "
                "WHERE file_path = ? OR file_path = ? ORDER BY id ASC",
                (path, str(file_path)),
            ).fetchall()
            return [
                {
                    "hunk_id": int(r["id"]),
                    "file_path": r["file_path"],
                    "turn_id": r["turn_id"],
                    "timestamp": float(r["timestamp"]),
                    "diff_text": r["diff"],
                    "author": r["author"],
                    "parent_hunk_id": r["parent_id"],
                    "action": r["action"],
                    "size": r["size"],
                }
                for r in rows
            ]

    def get_hunk(self, hunk_id: int) -> Optional[Dict[str, Any]]:
        """Return a single hunk record including content snapshots."""
        with self._lock:
            if self._conn is None:
                return None
            r = self._conn.execute(
                "SELECT * FROM hunks WHERE id = ?", (hunk_id,)
            ).fetchone()
            if not r:
                return None
            return {
                "hunk_id": int(r["id"]),
                "file_path": r["file_path"],
                "turn_id": r["turn_id"],
                "timestamp": float(r["timestamp"]),
                "diff_text": r["diff"],
                "author": r["author"],
                "parent_hunk_id": r["parent_id"],
                "content_before": r["content_before"],
                "content_after": r["content_after"],
                "action": r["action"],
                "size": r["size"],
            }

    def rollback(self, file_path: str, hunk_id: int) -> Dict[str, Any]:
        """Revert file to the content state after the given hunk.

        Reconstruction strategy:
          1. Prefer content_after snapshot for the target hunk.
          2. Else reconstruct by walking reverse patches from latest to target.
          3. Write restored content and record a new rollback hunk.

        Args:
            file_path: File to restore.
            hunk_id: Target hunk id (content after this hunk is restored).

        Returns:
            Dict with ok/error and restored path/hunk metadata.
        """
        path = str(Path(file_path).expanduser())
        try:
            path = str(Path(path).resolve())
        except Exception:
            pass

        with self._lock:
            if self._conn is None:
                return {"error": "tracker not initialized"}

            target = self._conn.execute(
                "SELECT * FROM hunks WHERE id = ?", (hunk_id,)
            ).fetchone()
            if not target:
                return {"error": f"hunk not found: {hunk_id}"}

            target_path = target["file_path"]
            if path and Path(path).name and target_path not in (path, str(file_path)):
                # Allow matching by basename or exact path
                if Path(target_path).resolve() != Path(path).resolve() and path != target_path:
                    # Still allow if caller path resolves differently
                    if str(file_path) not in (target_path, path):
                        # Use the path stored on the hunk
                        path = target_path
            else:
                path = target_path

            content: Optional[str] = target["content_after"]
            if content is None:
                content = self._reconstruct_content(path, hunk_id)
            if content is None:
                return {"error": f"cannot reconstruct content for hunk {hunk_id}"}

            # Capture current content for the rollback hunk
            current = _read_text_safe(Path(path)) if Path(path).is_file() else ""
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text(content, encoding="utf-8")
            except Exception as e:
                return {"error": f"failed to write restored content: {e}"}

            new_id = self.record(
                file_path=path,
                action="rollback",
                old_content=current,
                new_content=content,
                author="hunk_rollback",
                turn_id=f"rollback-to-{hunk_id}",
                size=len(content.encode("utf-8", errors="replace")),
            )
            return {
                "ok": True,
                "path": path,
                "restored_hunk_id": hunk_id,
                "new_hunk_id": new_id,
                "bytes": len(content.encode("utf-8", errors="replace")),
            }

    def _reconstruct_content(self, file_path: str, target_hunk_id: int) -> Optional[str]:
        """Rebuild content after target_hunk_id using reverse walk / snapshots."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT id, content_before, content_after, parent_id FROM hunks "
            "WHERE file_path = ? ORDER BY id ASC",
            (file_path,),
        ).fetchall()
        if not rows:
            return None

        # Prefer nearest snapshot at or before target
        content: Optional[str] = None
        for r in rows:
            if int(r["id"]) > target_hunk_id:
                break
            if r["content_after"] is not None:
                content = r["content_after"]
            elif content is None and r["content_before"] is not None and int(r["id"]) == target_hunk_id:
                # Should not happen if content_after missing; fall through
                pass
        if content is not None:
            return content

        # Reverse-patch from latest content_after back to target
        latest_after = None
        for r in reversed(rows):
            if r["content_after"] is not None:
                latest_after = r["content_after"]
                break
        if latest_after is None:
            latest_after = _read_text_safe(Path(file_path))

        # Walk from newest to target+1, applying reverse (content_before of each)
        content = latest_after
        for r in reversed(rows):
            hid = int(r["id"])
            if hid <= target_hunk_id:
                break
            if r["content_before"] is not None:
                content = r["content_before"]
        # If we landed past target, use target's content_before only if rolling
        # to state before that hunk — we want after target, so:
        for r in rows:
            if int(r["id"]) == target_hunk_id and r["content_after"] is not None:
                return r["content_after"]
            if int(r["id"]) == target_hunk_id and r["content_before"] is not None:
                # Without after snapshot, reverse walk leaves us at before of next
                # which equals after of target if chain is contiguous
                return content
        return content

    def diff_since(self, file_path: str, since_turn: str) -> str:
        """Concatenate diffs for hunks after a given turn_id (exclusive).

        Args:
            file_path: File path to query.
            since_turn: Turn id; only hunks after this turn are included.
                        If the turn is not found, all hunks with timestamp
                        greater than the first matching turn string compare
                        lexicographically by insertion order after that turn.

        Returns:
            Combined unified-diff text (may be empty).
        """
        path = str(Path(file_path).expanduser())
        try:
            path = str(Path(path).resolve())
        except Exception:
            pass

        with self._lock:
            if self._conn is None:
                return ""
            # Find the max id belonging to since_turn for this file
            row = self._conn.execute(
                "SELECT MAX(id) AS mid FROM hunks WHERE file_path = ? AND turn_id = ?",
                (path, since_turn),
            ).fetchone()
            min_id = int(row["mid"]) if row and row["mid"] is not None else 0

            rows = self._conn.execute(
                "SELECT diff FROM hunks WHERE file_path = ? AND id > ? ORDER BY id ASC",
                (path, min_id),
            ).fetchall()
            parts = [r["diff"] for r in rows if r["diff"]]
            return "\n".join(parts)

    def get_touched(self) -> List[Dict[str, Any]]:
        """Return touched file entries for this task (file_state compatible)."""
        with _touched_lock:
            files = _touched.get(self.task_id, {})
            return [
                {"path": p, **info}
                for p, info in sorted(files.items(), key=lambda x: x[1]["timestamp"])
            ]


def get_tracker(task_id: Optional[str] = None) -> HunkTracker:
    """Return (and cache) the HunkTracker for a task_id."""
    tid = task_id or os.environ.get("TICAL_TASK_ID", "current")
    with _trackers_lock:
        tracker = _trackers.get(tid)
        if tracker is None:
            tracker = HunkTracker(tid)
            _trackers[tid] = tracker
        return tracker


# ---------------------------------------------------------------------------
# Legacy file_state-compatible module API
# ---------------------------------------------------------------------------

def record(
    task_id: str,
    path: str,
    action: str,
    size: int = 0,
    old_content: Optional[str] = None,
    new_content: Optional[str] = None,
    author: str = "unknown",
    turn_id: Optional[str] = None,
) -> Optional[int]:
    """Record a file operation (file_state-compatible, hunk-aware)."""
    tracker = get_tracker(task_id)
    return tracker.record(
        file_path=path,
        action=action,
        old_content=old_content,
        new_content=new_content,
        author=author,
        turn_id=turn_id,
        size=size,
    )


def get_touched(task_id: str) -> List[Dict[str, Any]]:
    """Return list of files touched by a task (file_state-compatible)."""
    with _touched_lock:
        files = _touched.get(task_id, {})
        if files:
            return [
                {"path": p, **info}
                for p, info in sorted(files.items(), key=lambda x: x[1]["timestamp"])
            ]
    # Fall back to DB-derived latest actions
    tracker = get_tracker(task_id)
    with tracker._lock:
        if tracker._conn is None:
            return []
        rows = tracker._conn.execute(
            "SELECT file_path, action, size, timestamp FROM hunks "
            "WHERE id IN (SELECT MAX(id) FROM hunks GROUP BY file_path) "
            "ORDER BY timestamp ASC"
        ).fetchall()
        return [
            {
                "path": r["file_path"],
                "action": r["action"],
                "size": r["size"] or 0,
                "timestamp": float(r["timestamp"]),
            }
            for r in rows
        ]


def clear_task(task_id: str) -> None:
    """Clear in-memory touched state for a completed task (DB retained)."""
    with _touched_lock:
        removed = _touched.pop(task_id, None)
    with _trackers_lock:
        tracker = _trackers.pop(task_id, None)
    if tracker is not None:
        tracker.close()
    if removed is not None:
        logger.debug(
            "Cleared hunk/file state for task=%s (%d files)",
            task_id, len(removed),
        )


def get_history(file_path: str, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Module-level history helper."""
    return get_tracker(task_id).get_history(file_path)


def rollback(
    file_path: str,
    hunk_id: int,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Module-level rollback helper."""
    return get_tracker(task_id).rollback(file_path, hunk_id)


def diff_since(
    file_path: str,
    since_turn: str,
    task_id: Optional[str] = None,
) -> str:
    """Module-level diff_since helper."""
    return get_tracker(task_id).diff_since(file_path, since_turn)


def register_hunk_tools(registry: Any = None) -> None:
    """Register hunk_history and hunk_rollback with a ToolRegistry if available.

    Safe no-op when registry is None or ToolDefinition is unavailable.
    """
    try:
        from tical_code.core.tool_registry import (
            ToolDefinition,
            SandboxLevel,
            get_registry,
        )
    except Exception:
        # Fallback: only ensure tool_executor dispatch is used
        logger.debug("hunk_tracker: tool_registry not available for registration")
        return

    try:
        from tical_code.core.verify import VerifyLevel  # type: ignore
        vlevel = getattr(VerifyLevel, "BASIC", 1)
    except Exception:
        vlevel = 1

    reg = registry or get_registry()

    def _history_handler(args: dict) -> dict:
        path = (args or {}).get("path") or (args or {}).get("file_path") or ""
        if not path:
            return {"error": "path is required"}
        tid = (args or {}).get("task_id") or os.environ.get("TICAL_TASK_ID", "current")
        history = get_history(path, task_id=tid)
        return {"ok": True, "path": path, "history": history, "count": len(history)}

    def _rollback_handler(args: dict) -> dict:
        path = (args or {}).get("path") or (args or {}).get("file_path") or ""
        hunk_id = (args or {}).get("hunk_id")
        if not path or hunk_id is None:
            return {"error": "path and hunk_id are required"}
        try:
            hunk_id = int(hunk_id)
        except (TypeError, ValueError):
            return {"error": "hunk_id must be an integer"}
        tid = (args or {}).get("task_id") or os.environ.get("TICAL_TASK_ID", "current")
        return rollback(path, hunk_id, task_id=tid)

    tools = [
        ToolDefinition(
            name="hunk_history",
            description=(
                "List version history (unified-diff hunks) for a file tracked "
                "during the current task. Returns hunk_id, turn_id, author, diff."
            ),
            params={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to query"},
                    "task_id": {"type": "string", "description": "Optional task id"},
                },
                "required": ["path"],
            },
            handler=_history_handler,
            verify_level=vlevel,
            sandbox_level=SandboxLevel.NONE,
        ),
        ToolDefinition(
            name="hunk_rollback",
            description=(
                "Rollback a file to the content state after a previous hunk_id. "
                "Use hunk_history first to list available versions."
            ),
            params={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to restore"},
                    "hunk_id": {"type": "integer", "description": "Target hunk id"},
                    "task_id": {"type": "string", "description": "Optional task id"},
                },
                "required": ["path", "hunk_id"],
            },
            handler=_rollback_handler,
            verify_level=vlevel,
            sandbox_level=SandboxLevel.RECOMMENDED,
        ),
    ]
    for t in tools:
        try:
            reg.register_sync(t)
        except Exception as e:
            logger.warning("Failed to register %s: %s", t.name, e)
