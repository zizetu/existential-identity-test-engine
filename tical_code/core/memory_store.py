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

"""
Memory Store - EITE Evaluation Data Storage
=============================================

Provides a SQLite-backed full-text search index layer over evaluation
session data files (SOUL.md, MEMORY.md, TOOLS.md, USER.md, SECRET.md).

Design Principles:
- SQLite is the INDEX layer; markdown files remain the SOURCE OF TRUTH
- On startup, rebuild FTS5 index from markdown files (incremental via mtime)
- Write-through: index_entry() and remove_entry() update SQLite only
- sync_from_files() detects file changes and rebuilds stale entries
- Zero new external dependencies (SQLite + FTS5 are stdlib)

Author: Tical (Zize Tu)
Version: see tical_code.__version__
"""

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Memory file map (keep consistent with memory_boot.py)
MEMORY_FILE_MAP = {
    "soul": "Base config/SOUL.md",
    "user": "USER.md",
    "memory": "MEMORY.md",
    "secret": "SECRET.md",
    "tools": "Base config/TOOLS.md",
    "email_rules": "Base config/EMAIL_RULES.md",
}

# Markdown section title regex (## Title format)
_SECTION_RE = re.compile(r'^(#{1,3})\s+(.+)$', re.MULTILINE)

# CJK character preprocessing: insert spaces before indexing
_CJK_RE = re.compile(r'([\u4e00-\u9fff])')


def _preprocess_cjk(text: str) -> str:
    """Preprocess text for FTS5 indexing.

    FTS5's unicode61 tokenizer skips CJK characters (Unicode 'Lo' category).
    This function inserts spaces between CJK characters so that each character
    becomes an individual token that unicode61 can recognize.

    Args:
        text: Original text

    Returns:
        Text with CJK characters spaced for FTS5 tokenization
    """
    return _CJK_RE.sub(r'\1 ', text)

# FTS5 create-table SQL
_CREATE_CONTENT_TABLE = """
CREATE TABLE IF NOT EXISTS memory_content(
    rowid INTEGER PRIMARY KEY,
    file_key TEXT NOT NULL,
    section_title TEXT NOT NULL,
    raw_section_title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    raw_content TEXT NOT NULL DEFAULT ''
);
"""

_CREATE_FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries USING fts5(
    file_key,
    section_title,
    content,
    content='memory_content',
    content_rowid='rowid',
    tokenize='unicode61'
);
"""

_CREATE_INSERT_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_content BEGIN
    INSERT INTO memory_entries(rowid, file_key, section_title, content)
    VALUES (new.rowid, new.file_key, new.section_title, new.content);
END;
"""

_CREATE_DELETE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_content BEGIN
    INSERT INTO memory_entries(memory_entries, rowid, file_key, section_title, content)
    VALUES ('delete', old.rowid, old.file_key, old.section_title, old.content);
END;
"""

_CREATE_UPDATE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_content BEGIN
    INSERT INTO memory_entries(memory_entries, rowid, file_key, section_title, content)
    VALUES ('delete', old.rowid, old.file_key, old.section_title, old.content);
    INSERT INTO memory_entries(rowid, file_key, section_title, content)
    VALUES (new.rowid, new.file_key, new.section_title, new.content);
END;
"""

_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS memory_meta(
    file_key TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    entry_count INTEGER NOT NULL DEFAULT 0
);
"""

_SEARCH_SQL = """
SELECT
    mc.file_key,
    mc.raw_section_title,
    mc.raw_content,
    snippet(memory_entries, 2, '>>>', '<<<', '...', 20) as snippet,
    rank
FROM memory_entries
JOIN memory_content mc ON memory_entries.rowid = mc.rowid
WHERE memory_entries MATCH ?
ORDER BY rank
LIMIT ?;
"""

_SEARCH_SQL_WITH_FILE_KEY = """
SELECT
    mc.file_key,
    mc.raw_section_title,
    mc.raw_content,
    snippet(memory_entries, 2, '>>>', '<<<', '...', 20) as snippet,
    rank
FROM memory_entries
JOIN memory_content mc ON memory_entries.rowid = mc.rowid
WHERE memory_entries MATCH ? AND mc.file_key = ?
ORDER BY rank
LIMIT ?;
"""


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class SearchResult:
    """Search result from FTS5 full-text search."""
    file_key: str           # Source memory file
    section_title: str      # Section title
    content: str            # Matched content fragment
    snippet: str            # FTS5 highlight summary
    rank: float             # Relevance score


# =============================================================================
# MemoryFTSStore
# =============================================================================

class MemoryFTSStore:
    """SQLite+FTS5 memory storage engine for EITE evaluation data.

    Design Principles:
    - SQLite is the index layer; markdown files are the source of truth
    - On startup, rebuild FTS5 index from markdown files
    - Write-through: updating index updates SQLite only
    - sync_from_files() detects file changes and rebuilds stale entries
    - Zero new external dependencies (SQLite + FTS5 are stdlib)

    Usage:
        store = MemoryFTSStore(memory_dir="/path/to/memory")
        count = store.build_index()
        results = store.search("evaluation criteria", limit=5)
        store.close()
    """

    def __init__(self, memory_dir: str, db_path: Optional[str] = None):
        """Initialize MemoryFTSStore.

        Args:
            memory_dir: Memory file directory
            db_path: SQLite database path, defaults to memory_dir/.memory.db
        """
        self.memory_dir = os.path.expanduser(memory_dir)
        self.db_path = db_path or os.path.join(self.memory_dir, ".memory.db")

        os.makedirs(self.memory_dir, exist_ok=True)

        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database with FTS5 tables and triggers."""
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        cursor = self._conn.cursor()
        cursor.execute(_CREATE_CONTENT_TABLE)
        cursor.execute(_CREATE_FTS_TABLE)
        cursor.execute(_CREATE_INSERT_TRIGGER)
        cursor.execute(_CREATE_DELETE_TRIGGER)
        cursor.execute(_CREATE_UPDATE_TRIGGER)
        cursor.execute(_CREATE_META_TABLE)
        self._conn.commit()

        logger.debug(f"[EITE MemoryFTSStore] Database initialized: {self.db_path}")

    # =========================================================================
    # Index Building
    # =========================================================================

    def build_index(self) -> int:
        """Build FTS5 index from evaluation markdown files.

        Parses each markdown file into sections (by ## headings) and indexes
        each section as a separate entry. Clears existing index first.

        Returns:
            Number of entries indexed
        """
        total_entries = 0

        self._conn.execute("DELETE FROM memory_content")
        self._conn.execute("DELETE FROM memory_meta")
        self._conn.commit()

        for file_key, rel_path in MEMORY_FILE_MAP.items():
            file_path = os.path.join(self.memory_dir, rel_path)
            if not os.path.exists(file_path):
                logger.debug(f"[EITE MemoryFTSStore] Skip nonexistent file: {rel_path}")
                continue

            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                sections = self._parse_sections(content)
                entry_count = 0

                for section_title, section_content in sections:
                    if section_content.strip():
                        self.index_entry(file_key, section_title, section_content)
                        entry_count += 1

                stat = os.stat(file_path)
                self._conn.execute(
                    "INSERT OR REPLACE INTO memory_meta (file_key, mtime, size, entry_count) VALUES (?, ?, ?, ?)",
                    (file_key, stat.st_mtime, stat.st_size, entry_count),
                )

                total_entries += entry_count
                logger.info(
                    f"[EITE MemoryFTSStore] Indexed {file_key}: {entry_count} entries, "
                    f"mtime={stat.st_mtime:.1f}"
                )

            except Exception as e:
                logger.error(f"[EITE MemoryFTSStore] Index failed for {file_key}: {e}")

        self._conn.commit()
        logger.info(f"[EITE MemoryFTSStore] Index build complete: {total_entries} entries")
        return total_entries

    def _parse_sections(self, content: str) -> List[tuple]:
        """Parse markdown content into (title, content) sections.

        Split by ## headings. Content before the first heading goes into the
        "_top" section. Supports level-1, level-2, and level-3 headings.

        Args:
            content: Markdown text content

        Returns:
            List of (section_title, section_content) tuples
        """
        sections = []
        matches = list(_SECTION_RE.finditer(content))

        if not matches:
            if content.strip():
                sections.append(("_top", content.strip()))
            return sections

        first_match = matches[0]
        if first_match.start() > 0:
            preamble = content[:first_match.start()].strip()
            if preamble:
                sections.append(("_top", preamble))

        for i, match in enumerate(matches):
            title = match.group(2).strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            section_content = content[start:end].strip()
            sections.append((title, section_content))

        return sections

    # =========================================================================
    # Index Entry Management
    # =========================================================================

    def index_entry(self, file_key: str, section_title: str, content: str) -> None:
        """Index an evaluation memory entry.

        Args:
            file_key: Memory file identifier
            section_title: Section title
            content: Section content
        """
        processed_content = _preprocess_cjk(content)
        processed_title = _preprocess_cjk(section_title)

        existing = self._conn.execute(
            "SELECT rowid FROM memory_content WHERE file_key = ? AND raw_section_title = ?",
            (file_key, section_title),
        ).fetchone()

        if existing:
            self._conn.execute(
                "UPDATE memory_content SET section_title = ?, content = ?, raw_content = ? WHERE rowid = ?",
                (processed_title, processed_content, content, existing[0]),
            )
        else:
            self._conn.execute(
                "INSERT INTO memory_content (file_key, section_title, raw_section_title, content, raw_content) VALUES (?, ?, ?, ?, ?)",
                (file_key, processed_title, section_title, processed_content, content),
            )

        self._conn.commit()

    def remove_entry(self, file_key: str, section_title: str) -> bool:
        """Remove an evaluation memory entry from the index.

        Args:
            file_key: Memory file identifier
            section_title: Section title (original value)

        Returns:
            True if entry was removed, False if not found
        """
        cursor = self._conn.execute(
            "DELETE FROM memory_content WHERE file_key = ? AND raw_section_title = ?",
            (file_key, section_title),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # =========================================================================
    # Search
    # =========================================================================

    def search(
        self,
        query: str,
        limit: int = 10,
        file_key: Optional[str] = None,
    ) -> List[SearchResult]:
        """FTS5 full-text search over evaluation data.

        Args:
            query: Search query (FTS5 query syntax)
            limit: Maximum number of results
            file_key: Limit search to a specific file

        Returns:
            SearchResult list sorted by relevance
        """
        fts_query = self._sanitize_fts_query(query)
        if not fts_query:
            return []

        try:
            if file_key:
                rows = self._conn.execute(
                    _SEARCH_SQL_WITH_FILE_KEY,
                    (fts_query, file_key, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    _SEARCH_SQL,
                    (fts_query, limit),
                ).fetchall()

            results = []
            for row in rows:
                results.append(SearchResult(
                    file_key=row[0],
                    section_title=row[1],
                    content=row[2],
                    snippet=row[3] or "",
                    rank=row[4],
                ))

            return results

        except sqlite3.OperationalError as e:
            logger.warning(f"[EITE MemoryFTSStore] Search query failed: {e}")
            return []

    def _sanitize_fts_query(self, query: str) -> str:
        """Sanitize user query for FTS5.

        Strips FTS5 special operators, wraps each token in quotes,
        and joins with OR. CJK characters are preprocessed consistently
        with indexing.

        Args:
            query: Original user query

        Returns:
            Sanitized FTS5 query string
        """
        if not query or not query.strip():
            return ""

        cleaned = re.sub(r'[\"\'*()\^:{}]', '', query)

        tokens = re.split(r'[\s,;,；,]+', cleaned)
        tokens = [t for t in tokens if t]

        if not tokens:
            return ""

        fts_keywords = {'AND', 'OR', 'NOT', 'NEAR'}
        tokens = [t for t in tokens if t.upper() not in fts_keywords]

        if not tokens:
            return ""

        processed_tokens = []
        for t in tokens:
            processed = _preprocess_cjk(t).strip()
            if processed:
                processed_tokens.append(f'"{processed}"')

        if not processed_tokens:
            return ""

        return " OR ".join(processed_tokens)

    # =========================================================================
    # Sync & Stats
    # =========================================================================

    def sync_from_files(self) -> Dict[str, int]:
        """Sync from markdown files to SQLite (incremental update).

        Only re-indexes files whose mtime or size has changed since last sync.

        Returns:
            {"synced": int, "skipped": int, "errors": int}
        """
        synced = 0
        skipped = 0
        errors = 0

        for file_key, rel_path in MEMORY_FILE_MAP.items():
            file_path = os.path.join(self.memory_dir, rel_path)

            if not os.path.exists(file_path):
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM memory_content WHERE file_key = ?",
                    (file_key,),
                ).fetchone()[0]
                if count > 0:
                    self._conn.execute(
                        "DELETE FROM memory_content WHERE file_key = ?", (file_key,)
                    )
                    self._conn.execute(
                        "DELETE FROM memory_meta WHERE file_key = ?", (file_key,)
                    )
                    self._conn.commit()
                    logger.info(f"[EITE MemoryFTSStore] Removed index entries for deleted file: {file_key}")
                continue

            try:
                stat = os.stat(file_path)

                meta = self._conn.execute(
                    "SELECT mtime, size, entry_count FROM memory_meta WHERE file_key = ?",
                    (file_key,),
                ).fetchone()

                if meta and meta[0] == stat.st_mtime and meta[1] == stat.st_size:
                    skipped += 1
                    continue

                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                self._conn.execute(
                    "DELETE FROM memory_content WHERE file_key = ?", (file_key,)
                )

                sections = self._parse_sections(content)
                entry_count = 0
                for section_title, section_content in sections:
                    if section_content.strip():
                        self.index_entry(file_key, section_title, section_content)
                        entry_count += 1

                self._conn.execute(
                    "INSERT OR REPLACE INTO memory_meta (file_key, mtime, size, entry_count) VALUES (?, ?, ?, ?)",
                    (file_key, stat.st_mtime, stat.st_size, entry_count),
                )
                self._conn.commit()

                synced += 1
                logger.info(f"[EITE MemoryFTSStore] Synced {file_key}: {entry_count} entries")

            except Exception as e:
                errors += 1
                logger.error(f"[EITE MemoryFTSStore] Sync failed for {file_key}: {e}")

        result = {"synced": synced, "skipped": skipped, "errors": errors}
        logger.info(f"[EITE MemoryFTSStore] Sync complete: {result}")
        return result

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics.

        Returns:
            Dict with total entries, entries per file_key, DB size, etc.
        """
        total = self._conn.execute("SELECT COUNT(*) FROM memory_content").fetchone()[0]

        per_file = {}
        rows = self._conn.execute(
            "SELECT file_key, COUNT(*) FROM memory_content GROUP BY file_key"
        ).fetchall()
        for file_key, count in rows:
            per_file[file_key] = count

        meta_rows = self._conn.execute("SELECT * FROM memory_meta").fetchall()
        meta_info = {}
        for row in meta_rows:
            meta_info[row[0]] = {
                "mtime": row[1],
                "size": row[2],
                "entry_count": row[3],
            }

        db_size = 0
        if os.path.exists(self.db_path):
            db_size = os.path.getsize(self.db_path)

        return {
            "total_entries": total,
            "entries_per_file": per_file,
            "file_meta": meta_info,
            "db_size_bytes": db_size,
            "db_path": self.db_path,
        }

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            try:
                self._conn.close()
                logger.debug("[EITE MemoryFTSStore] Database connection closed")
            except Exception as e:
                logger.warning(f"[EITE MemoryFTSStore] Failed to close database: {e}")
            finally:
                self._conn = None

    def __del__(self):
        """Cleanup on garbage collection."""
        self.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False
