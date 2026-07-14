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

"""Module 1: Session Persistence - SQLite-backed conversation history."""

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("EITElite.session_manager")

class SessionManager:
    """Thread-safe SQLite session store with archival support."""

    MAX_TOOL_CONTENT = 32768

    def __init__(self, db_path: str, max_active: int = 100):
        self.db_path = Path(db_path)
        self.max_active = max_active
        self.lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_sid ON messages(session_id, id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS archived_sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at REAL,
                    updated_at REAL,
                    metadata TEXT,
                    archived_at REAL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS archived_messages (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS session_summaries (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            self.conn.commit()

    def get_session_id(self, channel: str, chat_id: str) -> str:
        raw = f"{channel}:{chat_id}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def save_messages(self, session_id: str, messages: list[dict]) -> bool:
        if not messages:
            return True
        try:
            now = time.time()
            with self.lock:
                cur = self.conn.cursor()
                cur.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,))
                if cur.fetchone():
                    cur.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id))
                else:
                    cur.execute(
                        "INSERT INTO sessions (session_id, created_at, updated_at, metadata) VALUES (?, ?, ?, ?)",
                        (session_id, now, now, "{}"),
                    )
                for msg in messages:
                    role = msg.get("role", "unknown")
                    # Strip reasoning_content - it causes 400 errors on model fallback
                    # and should never be persisted to DB
                    msg.pop("reasoning_content", None)
                    content = msg.get("content")
                    if isinstance(content, list):
                        parts = []
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") == "text":
                                    parts.append(part.get("text", ""))
                                else:
                                    parts.append("[media]")
                            else:
                                parts.append(str(part))
                        content = "\n".join(parts)
                    elif content is None:
                        content = ""
                    elif not isinstance(content, str):
                        content = str(content)
                    meta: dict[str, Any] = {}
                    if role == "assistant" and msg.get("tool_calls"):
                        meta["tool_calls"] = msg["tool_calls"]
                    if role == "tool":
                        meta["tool_call_id"] = msg.get("tool_call_id")
                    if role == "tool" and len(content) > self.MAX_TOOL_CONTENT:
                        content = content[:self.MAX_TOOL_CONTENT] + "\n[truncated]"
                    cur.execute(
                        "INSERT INTO messages (session_id, role, content, timestamp, metadata) VALUES (?, ?, ?, ?, ?)",
                        (session_id, role, content, now, json.dumps(meta, ensure_ascii=False)),
                    )
                self.conn.commit()
                self._enforce_active_limit()
            return True
        except Exception:
            logger.exception("save_messages failed")
            return False

    def load_session(self, session_id: str, max_messages: int = 30) -> list[dict]:
        try:
            with self.lock:
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, max_messages),
                )
                rows = list(reversed(cur.fetchall()))
            # Track which tool_call_ids have matching tool results in loaded messages
            loaded_tool_ids: set[str] = set()
            for row in rows:
                if row["role"] == "tool":
                    try:
                        meta = json.loads(row["metadata"] or "{}")
                    except Exception:
                        meta = {}
                    tc_id = meta.get("tool_call_id", "")
                    if tc_id:
                        loaded_tool_ids.add(tc_id)

            out: list[dict] = []
            for row in rows:
                if row["role"] == "tool":
                    content = row["content"]
                    if len(content) > 2000:
                        content = content[:2000] + "\n[truncated]"
                    tool_msg = {"role": "tool", "content": content}
                    # Restore tool_call_id from metadata - required by API
                    try:
                        meta = json.loads(row["metadata"] or "{}")
                    except Exception:
                        meta = {}
                    tc_id = meta.get("tool_call_id", "")
                    if tc_id:
                        tool_msg["tool_call_id"] = tc_id
                    out.append(tool_msg)
                    continue
                if row["role"] not in ("user", "assistant"):
                    continue
                msg: dict[str, Any] = {"role": row["role"], "content": row["content"]}
                if row["role"] == "assistant":
                    try:
                        meta = json.loads(row["metadata"] or "{}")
                    except Exception:
                        meta = {}
                    tool_calls = meta.get("tool_calls", [])
                    if tool_calls:
                        # Include ALL tool_calls with placeholder results for orphans
                        fixed_calls = []
                        for tc in tool_calls:
                            tc_id = tc.get("id", "")
                            if tc_id in loaded_tool_ids:
                                fixed_calls.append(tc)
                            else:
                                # Orphan tool_call: inject placeholder result to avoid API 400
                                fixed_calls.append(tc)
                                out.append({
                                    "role": "tool",
                                    "content": "[result from prior turn - not in loaded window]",
                                    "tool_call_id": tc_id,
                                })
                        if fixed_calls:
                            msg["tool_calls"] = fixed_calls
                out.append(msg)
            return out
        except Exception:
            logger.exception("load_session failed")
            return []

    def save_summary(self, session_id: str, summary: str) -> bool:
        """Save or update a session summary for context persistence on restart."""
        try:
            now = time.time()
            with self.lock:
                cur = self.conn.cursor()
                # Get current turn count from messages table
                cur.execute("SELECT COUNT(*) as cnt FROM messages WHERE session_id = ?", (session_id,))
                turn_count = cur.fetchone()["cnt"]
                cur.execute(
                    "INSERT INTO session_summaries (session_id, summary, updated_at, turn_count) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "summary=excluded.summary, updated_at=excluded.updated_at, turn_count=excluded.turn_count",
                    (session_id, summary, now, turn_count),
                )
                self.conn.commit()
            return True
        except Exception:
            logger.exception("save_summary failed")
            return False

    def get_summary(self, session_id: str) -> str | None:
        """Retrieve session summary for context injection on restart."""
        try:
            with self.lock:
                cur = self.conn.cursor()
                cur.execute("SELECT summary FROM session_summaries WHERE session_id = ?", (session_id,))
                row = cur.fetchone()
                if row:
                    return row["summary"]
            return None
        except Exception:
            logger.exception("get_summary failed")
            return None

    def archive_old(self, days: int = 7) -> int:
        try:
            cutoff = time.time() - days * 86400
            with self.lock:
                cur = self.conn.cursor()
                cur.execute("SELECT session_id FROM sessions WHERE updated_at < ?", (cutoff,))
                expired = [r["session_id"] for r in cur.fetchall()]
                moved = 0
                for sid in expired:
                    self._archive_one(cur, sid)
                    moved += 1
                self.conn.commit()
            return moved
        except Exception:
            logger.exception("archive_old failed")
            return 0

    def _archive_one(self, cur, session_id: str) -> None:
        row = cur.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return
        now = time.time()
        cur.execute(
            "INSERT OR REPLACE INTO archived_sessions (session_id, created_at, updated_at, metadata, archived_at) VALUES (?, ?, ?, ?, ?)",
            (row["session_id"], row["created_at"], row["updated_at"], row["metadata"], now),
        )
        cur.execute(
            "INSERT OR REPLACE INTO archived_messages SELECT * FROM messages WHERE session_id = ?",
            (session_id,),
        )
        cur.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cur.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def _enforce_active_limit(self) -> None:
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT session_id FROM sessions ORDER BY updated_at DESC")
            rows = cur.fetchall()
            if len(rows) <= self.max_active:
                return
            for row in rows[self.max_active:]:
                self._archive_one(cur, row["session_id"])
            self.conn.commit()
        except Exception:
            logger.exception("_enforce_active_limit failed")

    def cleanup(self, max_age_days: int = 3, max_db_size_mb: int = 50) -> dict:
        """Auto-cleanup: archive old sessions and trim oversized DBs.

        Called from worker loop periodically (e.g. every 100 messages).
        Returns stats dict.
        """
        stats = {"archived": 0, "db_size_mb": 0, "trimmed": False}
        try:
            # Archive sessions older than max_age_days
            stats["archived"] = self.archive_old(days=max_age_days)

            # Check DB size
            db_size = self.db_path.stat().st_size / (1024 * 1024)
            stats["db_size_mb"] = round(db_size, 1)

            if db_size > max_db_size_mb:
                # Aggressive: delete archived tables and VACUUM
                with self.lock:
                    cur = self.conn.cursor()
                    cur.execute("DELETE FROM archived_messages")
                    cur.execute("DELETE FROM archived_sessions")
                    cur.execute("VACUUM")
                    self.conn.commit()
                stats["trimmed"] = True
                logger.info(f"[session] DB trimmed: {db_size:.1f}MB -> {self.db_path.stat().st_size / (1024*1024):.1f}MB")
        except Exception:
            logger.exception("[session] cleanup failed")
        return stats
