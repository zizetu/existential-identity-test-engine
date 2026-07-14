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

#!/usr/bin/env python3
"""
memory_sense.py - EITE Evaluation Context Awareness
=====================================================

Evaluation context awareness: semantic search of evaluation session files.
This provides an agent-level recall primitive for evaluation history.

Implementation: sqlite3 FTS5 full-text index + CJK unigram tokenizer
"""

import json
import os
import re
import sqlite3
import threading
import time
from typing import Dict, List, Optional

# ============ Config ============

MEMORY_DB = os.environ.get(
    "EITE_MEMORY_DB",
    os.path.expanduser("~/.eite/memory.db")
)
MEMORY_SUPPORTED_EXT = (".md", ".txt", ".json")

# ============ Chinese tokenize ============

def _chinese_unigram(text: str) -> str:
    """CJK unigram tokenizer: insert spaces between CJK characters.

    FTS5's unicode61 tokenizer uses space-delimited tokenization.
    This function separates CJK characters so they become individual tokens.

    Args:
        text: original text

    Returns:
        space-separated tokenization result
    """
    spaced = re.sub(r'([\u4e00-\u9fff])', r' \1 ', text)
    spaced = re.sub(r'\s+', ' ', spaced).strip()
    return spaced

# ============ Database management ============

_module_conn: Optional[sqlite3.Connection] = None
_module_lock = threading.Lock()
_thread_local = threading.local()

def _get_conn() -> sqlite3.Connection:
    """Get thread-local database connection.

    Each thread gets its own connection. WAL mode allows concurrent
    reads from different connections. Writes are serialized via lock.
    """
    conn = getattr(_thread_local, '_mem_conn', None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            conn = None

    os.makedirs(os.path.dirname(MEMORY_DB) or ".", exist_ok=True)
    try:
        conn = sqlite3.connect(MEMORY_DB, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _init_tables(conn)
    except sqlite3.DatabaseError:
        if os.path.exists(MEMORY_DB):
            os.remove(MEMORY_DB)
        conn = sqlite3.connect(MEMORY_DB, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _init_tables(conn)

    _thread_local._mem_conn = conn
    return conn

def _init_tables(conn: sqlite3.Connection):
    """Initialize FTS5 tables and metadata table."""
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory
        USING fts5(path, content, content_indexed, lineno UNINDEXED,
                   tokenize='unicode61')
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_meta (
            path TEXT PRIMARY KEY,
            mtime REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL,
            metadata TEXT DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_conv_session
        ON conversation(session_id, timestamp)
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts
        USING fts5(session_id, role, content, content_indexed,
                   tokenize='unicode61')
    """)
    conn.commit()

# ============ Core primitives ============

def memory_index(directory: str) -> int:
    """Index all evaluation memory files in directory. Incremental update only.

    Args:
        directory: evaluation memory file directory

    Returns:
        number of files indexed
    """
    conn = _get_conn()
    indexed = 0

    existing_paths = set()
    file_update_ops = []
    for root, _, files in os.walk(directory):
        for fname in files:
            if not fname.endswith(MEMORY_SUPPORTED_EXT):
                continue
            fpath = os.path.join(root, fname)
            existing_paths.add(fpath)

            mtime = os.path.getmtime(fpath)
            row = conn.execute(
                "SELECT mtime FROM memory_meta WHERE path = ?", (fpath,)
            ).fetchone()

            if row and row[0] == mtime:
                continue

            lines_text = []
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        line_stripped = line.strip()
                        if line_stripped:
                            lines_text.append((i, line_stripped))
            except OSError:
                continue

            file_update_ops.append((fpath, mtime, lines_text))

    with _module_lock:
        for fpath, mtime, lines_text in file_update_ops:
            conn.execute("DELETE FROM memory WHERE path = ?", (fpath,))
            for lineno, line_stripped in lines_text:
                indexed_text = _chinese_unigram(line_stripped)
                conn.execute(
                    "INSERT INTO memory(path, content, content_indexed, lineno) "
                    "VALUES (?, ?, ?, ?)",
                    (fpath, line_stripped, indexed_text, lineno)
                )
            conn.execute(
                "INSERT OR REPLACE INTO memory_meta(path, mtime) VALUES (?, ?)",
                (fpath, mtime)
            )
            indexed += 1

        all_indexed = conn.execute("SELECT path FROM memory_meta").fetchall()
        for (path,) in all_indexed:
            if path not in existing_paths:
                conn.execute("DELETE FROM memory WHERE path = ?", (path,))
                conn.execute("DELETE FROM memory_meta WHERE path = ?", (path,))

        conn.commit()
    return indexed

def memory_search(query: str, top_k: int = 5, directory: str = None) -> List[Dict]:
    """Semantic search of evaluation memory files.

    Args:
        query: search keyword
        top_k: return at most N results
        directory: optional, limit to search directory

    Returns:
        [{path, lineno, snippet, score}, ...]
    """
    conn = _get_conn()
    spaced_query = _chinese_unigram(query)

    try:
        if directory:
            sql = """
                SELECT path, lineno, highlight(memory, 1, '>>>', '<<<') as snippet,
                       bm25(memory) as score
                FROM memory
                WHERE memory MATCH ? AND path LIKE ?
                ORDER BY score
                LIMIT ?
            """
            cur = conn.execute(sql, (spaced_query, f"{directory}%", top_k))
        else:
            sql = """
                SELECT path, lineno, highlight(memory, 1, '>>>', '<<<') as snippet,
                       bm25(memory) as score
                FROM memory
                WHERE memory MATCH ?
                ORDER BY score
                LIMIT ?
            """
            cur = conn.execute(sql, (spaced_query, top_k))
    except sqlite3.OperationalError:
        return []

    results = []
    for path, lineno, snippet, score in cur.fetchall():
        results.append({
            "path": path,
            "lineno": lineno,
            "snippet": snippet,
            "score": round(score, 4),
        })

    return results

def memory_reindex(force: bool = False) -> int:
    """Full rebuild of evaluation memory index.

    Args:
        force: force rebuild (delete old index)

    Returns:
        number of files rebuilt
    """
    conn = _get_conn()
    if force:
        with _module_lock:
            conn.execute("DELETE FROM memory")
            conn.execute("DELETE FROM memory_meta")
            conn.commit()

    memory_dir = os.environ.get(
        "EITE_MEMORY_DIR",
        os.path.expanduser("~/.eite/memory")
    )
    if os.path.isdir(memory_dir):
        return memory_index(memory_dir)
    return 0

def memory_remove(file_path: str) -> bool:
    """Delete a specific file's index."""
    conn = _get_conn()
    with _module_lock:
        conn.execute("DELETE FROM memory WHERE path = ?", (file_path,))
        conn.execute("DELETE FROM memory_meta WHERE path = ?", (file_path,))
        conn.commit()
    return True


# ============ Evaluation conversation storage ============

def conversation_save(session_id: str, role: str, content: str, metadata: dict = None) -> int:
    """Save one evaluation conversation to long-term storage.

    Args:
        session_id: session ID (e.g., eval session name)
        role: "user" / "assistant" / "system" / "tool"
        content: message content
        metadata: optional metadata (e.g., tool_name, model, etc.)

    Returns:
        inserted row ID
    """
    if not content:
        return 0
    conn = _get_conn()
    ts = time.time()
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    cursor = conn.execute(
        "INSERT INTO conversation(session_id, role, content, timestamp, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, ts, meta_json)
    )
    row_id = cursor.lastrowid
    indexed = _chinese_unigram(content)
    conn.execute(
        "INSERT INTO conversation_fts(rowid, session_id, role, content, content_indexed) "
        "VALUES (?, ?, ?, ?, ?)",
        (row_id, session_id, role, content[:2000], indexed[:2000])
    )
    conn.commit()
    return row_id


def conversation_search(query: str, session_id: str = None, top_k: int = 10,
                        role: str = None) -> List[Dict]:
    """Search evaluation conversation history."""
    conn = _get_conn()
    has_chinese = bool(re.search(r'[\u4e00-\u9fff]', query))
    if not has_chinese:
        words = re.findall(r'[a-zA-Z0-9]+', query)
        spaced = ' OR '.join(words) if words else _chinese_unigram(query)
    else:
        spaced = _chinese_unigram(query)
    try:
        sql = """
            SELECT c.id, c.session_id, c.role, c.content, c.timestamp
            FROM conversation_fts
            JOIN conversation c ON c.id = conversation_fts.rowid
            WHERE conversation_fts MATCH ?
        """
        params = [spaced]
        if session_id:
            sql += " AND c.session_id = ?"
            params.append(session_id)
        if role:
            sql += " AND c.role = ?"
            params.append(role)
        sql += " LIMIT ?"
        params.append(top_k)
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError:
        return []

    return [
        {"id": r[0], "session_id": r[1], "role": r[2], "content": r[3][:500],
         "timestamp": r[4], "score": 0.0}
        for r in cur.fetchall()
    ]


def conversation_recent(session_id: str = None, limit: int = 30) -> List[Dict]:
    """Get recent N evaluation conversations.

    Args:
        session_id: optional, limit to session
        limit: return entry count

    Returns:
        [{id, session_id, role, content, timestamp}, ...] in chronological order
    """
    conn = _get_conn()
    if session_id:
        cur = conn.execute(
            "SELECT id, session_id, role, content, timestamp "
            "FROM conversation WHERE session_id = ? "
            "ORDER BY timestamp DESC LIMIT ?", (session_id, limit)
        )
    else:
        cur = conn.execute(
            "SELECT id, session_id, role, content, timestamp "
            "FROM conversation ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
    rows = cur.fetchall()
    rows.reverse()
    return [
        {"id": r[0], "session_id": r[1], "role": r[2], "content": r[3][:500], "timestamp": r[4]}
        for r in rows
    ]
