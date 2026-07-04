"""
Memory Store Tests - SQLite + FTS5
===================================

Tests for tical_code.core.memory_store with real SQLite + FTS5.
No mocked SQLite - all tests use real in-memory or temp databases.

Covers:
- Database initialization
- Markdown section parsing
- Index building
- FTS5 search (English)
- Index entry management (add/remove)
- Incremental sync from files
- Stats reporting
- Context manager lifecycle
- SearchResult dataclass

Minimum: 15 tests

Author: EITElite Team
Version: v0.4.2
"""

import os
import tempfile
import time

import pytest

from tical_code.core.memory_store import MemoryFTSStore, SearchResult


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def memory_dir(tmp_path):
    """Create a temp directory with sample memory files."""
    # Create directory structure
    (tmp_path / "core_settings").mkdir()

    # SOUL.md
    (tmp_path / "core_settings" / "SOUL.md").write_text(
        "# Kael\n\n"
        "# Nova personality\n\n",
        "Nova is a warm and perceptive AI assistant.\n",
        "Good at listening and empathizing.\n\n"
        "## Principles\n"
        "- Honesty first\n"
        "- Protect user privacy\n"
        "- No overstepping boundaries\n",
        encoding="utf-8",
    )

    # MEMORY.md
    (tmp_path / "MEMORY.md").write_text(
        "## User Preferences\n"
        "User prefers concise reply style.\n"
        "User primarily communicates in English.\n\n"
        "## Project Experience\n"
        "### EITElite\n"
        "EITElite is an AI Agent framework project.\n"
        "Written in Python, currently version v0.4.\n\n"
        "### IB Trading\n"
        "Interactive Brokers trading plugin.\n"
        "Uses CP Gateway interface.\n",
        encoding="utf-8",
    )

    # TOOLS.md
    (tmp_path / "core_settings" / "TOOLS.md").write_text(
        "## SSE Streaming\n"
        "Read in chunks of 4096, split by \\n\\n to parse SSE events.\n"
        "4xx no retry, 5xx exponential backoff retry.\n\n"
        "## SQLite FTS5\n"
        "Use FTS5 full-text search instead of string matching.\n"
        "content-sync mode, trigger-based sync.\n",
        encoding="utf-8",
    )

    # USER.md
    (tmp_path / "USER.md").write_text(
        "## Basic Info\n"
        "Username: Zizetu\n"
        "Preferred language: English\n",
        encoding="utf-8",
    )

    # SECRET.md - not created, test missing file handling

    return tmp_path


@pytest.fixture
def store(memory_dir):
    """Create a MemoryFTSStore instance with temp directory."""
    db_path = str(memory_dir / ".test_memory.db")
    s = MemoryFTSStore(memory_dir=str(memory_dir), db_path=db_path)
    yield s
    s.close()


# =============================================================================
# Database Initialization Tests
# =============================================================================

class TestDBInit:
    """Test SQLite database initialization."""

    def test_db_file_created(self, memory_dir):
        """Test that database file is created on init."""
        db_path = str(memory_dir / ".test_created.db")
        store = MemoryFTSStore(str(memory_dir), db_path=db_path)
        assert os.path.exists(db_path)
        store.close()

    def test_db_has_required_tables(self, store):
        """Test that all required tables exist in the database."""
        cursor = store._conn.cursor()
        tables = cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}

        assert "memory_content" in table_names
        assert "memory_meta" in table_names

    def test_db_has_fts_virtual_table(self, store):
        """Test that FTS5 virtual table exists."""
        cursor = store._conn.cursor()
        tables = cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memory_entries%'"
        ).fetchall()
        # FTS5 virtual table name may be memory_entries or memory_entries_content etc
        table_names = {t[0] for t in tables}
        assert any("memory_entries" in name for name in table_names)

    def test_db_has_triggers(self, store):
        """Test that sync triggers exist."""
        cursor = store._conn.cursor()
        triggers = cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        trigger_names = {t[0] for t in triggers}

        assert "memory_ai" in trigger_names
        assert "memory_ad" in trigger_names
        assert "memory_au" in trigger_names


# =============================================================================
# Section Parsing Tests
# =============================================================================

class TestSectionParsing:
    """Test markdown section parsing."""

    def test_parse_sections_with_headings(self, store):
        """Test parsing markdown with ## headings."""
        content = "## Title A\nContent A\n\n## Title B\nContent B"
        sections = store._parse_sections(content)

        assert len(sections) == 2
        assert sections[0][0] == "Title A"
        assert "Content A" in sections[0][1]
        assert sections[1][0] == "Title B"
        assert "Content B" in sections[1][1]

    def test_parse_sections_no_headings(self, store):
        """Test parsing markdown without headings."""
        content = "Just some content without headings"
        sections = store._parse_sections(content)

        assert len(sections) == 1
        assert sections[0][0] == "_top"
        assert "Just some content" in sections[0][1]

    def test_parse_sections_preamble(self, store):
        """Test parsing with content before first heading."""
        content = "Preamble text\n\n## First Section\nContent here"
        sections = store._parse_sections(content)

        assert len(sections) == 2
        assert sections[0][0] == "_top"
        assert "Preamble" in sections[0][1]
        assert sections[1][0] == "First Section"

    def test_parse_sections_empty_content(self, store):
        """Test parsing empty content."""
        sections = store._parse_sections("")
        assert len(sections) == 0

    def test_parse_sections_level3_headings(self, store):
        """Test that ### headings are also parsed."""
        content = "## Main\nMain content\n\n### Sub\nSub content"
        sections = store._parse_sections(content)

        assert len(sections) == 2
        assert sections[0][0] == "Main"
        assert sections[1][0] == "Sub"


# =============================================================================
# Index Building Tests
# =============================================================================

class TestBuildIndex:
    """Test index building from markdown files."""

    def test_build_index_returns_count(self, store):
        """Test that build_index returns the number of entries."""
        count = store.build_index()
        assert count > 0

    def test_build_index_creates_entries(self, store):
        """Test that build_index populates memory_content table."""
        store.build_index()
        cursor = store._conn.execute("SELECT COUNT(*) FROM memory_content")
        count = cursor.fetchone()[0]
        assert count > 0

    def test_build_index_populates_meta(self, store):
        """Test that build_index populates memory_meta table."""
        store.build_index()
        cursor = store._conn.execute("SELECT COUNT(*) FROM memory_meta")
        count = cursor.fetchone()[0]
        # SOUL, MEMORY, TOOLS, USER - 4 files exist
        assert count >= 2  # At least 2 files have content

    def test_build_index_clears_old(self, store):
        """Test that build_index clears old entries before rebuilding."""
        # Build once
        store.build_index()
        first_count = store._conn.execute("SELECT COUNT(*) FROM memory_content").fetchone()[0]

        # Build again
        store.build_index()
        second_count = store._conn.execute("SELECT COUNT(*) FROM memory_content").fetchone()[0]

        # Should be same (same files)
        assert first_count == second_count

    def test_build_index_handles_missing_files(self, store):
        """Test that build_index gracefully handles missing files."""
        # SECRET.md does not exist
        count = store.build_index()
        assert count >= 0  # Should not crash


# =============================================================================
# Search Tests
# =============================================================================

class TestSearch:
    """Test FTS5 full-text search."""

    @pytest.fixture(autouse=True)
    def setup_index(self, store):
        """Build index before each search test."""
        store.build_index()

    def test_search_keyword(self, store):
        """Test searching with keywords."""
        results = store.search("User", limit=5)
        assert len(results) > 0
        assert any("user" in r.file_key or "memory" in r.file_key for r in results)

    def test_search_english_keyword(self, store):
        """Test searching with English keywords."""
        results = store.search("Python", limit=5)
        assert len(results) > 0

    def test_search_with_file_key_filter(self, store):
        """Test searching with file_key filter."""
        results = store.search("framework", file_key="memory", limit=5)
        assert len(results) > 0
        for r in results:
            assert r.file_key == "memory"

    def test_search_file_key_filter_no_results(self, store):
        """Test file_key filter when no matches exist."""
        results = store.search("nonexistent_xyz", file_key="soul", limit=5)
        assert len(results) == 0

    def test_search_empty_query(self, store):
        """Test search with empty query returns no results."""
        results = store.search("", limit=5)
        assert len(results) == 0

    def test_search_returns_search_result_type(self, store):
        """Test that search returns SearchResult instances."""
        results = store.search("User", limit=1)
        assert len(results) > 0
        r = results[0]
        assert isinstance(r, SearchResult)
        assert hasattr(r, 'file_key')
        assert hasattr(r, 'section_title')
        assert hasattr(r, 'content')
        assert hasattr(r, 'snippet')
        assert hasattr(r, 'rank')

    def test_search_result_has_snippet(self, store):
        """Test that search results have snippet with highlights."""
        results = store.search("Personality", limit=1)
        if results:
            # snippet may have >>> <<< highlight markers
            assert isinstance(results[0].snippet, str)

    def test_search_rank_ordering(self, store):
        """Test that results are ordered by relevance (rank)."""
        results = store.search("Nova Personality", limit=10)
        if len(results) > 1:
            # rank should be ascending (smaller = more relevant, FTS5 bm25 default)
            ranks = [r.rank for r in results]
            assert ranks == sorted(ranks)

    def test_search_no_results(self, store):
        """Test search with a term that doesn't exist."""
        results = store.search("zzzzzznonexistentterm12345", limit=5)
        assert len(results) == 0


# =============================================================================
# Index Entry Management Tests
# =============================================================================

class TestEntryManagement:
    """Test index_entry and remove_entry."""

    def test_index_entry_adds_to_db(self, store):
        """Test that index_entry inserts a new entry."""
        store.index_entry("test", "Test Section", "Test content for indexing")
        cursor = store._conn.execute(
            "SELECT content FROM memory_content WHERE file_key = ? AND section_title = ?",
            ("test", "Test Section"),
        )
        row = cursor.fetchone()
        assert row is not None
        assert "Test content" in row[0]

    def test_index_entry_update_existing(self, store):
        """Test that index_entry updates an existing entry."""
        store.index_entry("test", "Section", "Original content")
        store.index_entry("test", "Section", "Updated content")

        cursor = store._conn.execute(
            "SELECT content FROM memory_content WHERE file_key = ? AND section_title = ?",
            ("test", "Section"),
        )
        row = cursor.fetchone()
        assert row[0] == "Updated content"

    def test_remove_entry_exists(self, store):
        """Test removing an existing entry."""
        store.index_entry("test", "To Remove", "Will be removed")
        removed = store.remove_entry("test", "To Remove")
        assert removed is True

    def test_remove_entry_not_exists(self, store):
        """Test removing a non-existent entry."""
        removed = store.remove_entry("nonexistent", "No Section")
        assert removed is False

    def test_index_entry_searchable(self, store):
        """Test that indexed entries are searchable via FTS5."""
        store.index_entry("test", "FTS Test", "unique_searchable_term_xyz")
        results = store.search("unique_searchable_term_xyz", limit=5)
        assert len(results) > 0
        assert results[0].section_title == "FTS Test"


# =============================================================================
# Sync from Files Tests
# =============================================================================

class TestSyncFromFiles:
    """Test incremental sync from markdown files."""

    def test_sync_first_time(self, store):
        """Test first sync (all files are new)."""
        result = store.sync_from_files()
        assert result["synced"] >= 0  # depends on file count
        assert result["errors"] == 0

    def test_sync_no_changes_skipped(self, store):
        """Test that second sync with no changes skips all files."""
        store.sync_from_files()
        result = store.sync_from_files()
        assert result["skipped"] > 0
        assert result["synced"] == 0

    def test_sync_detects_file_change(self, store, memory_dir):
        """Test that sync detects a changed file."""
        store.sync_from_files()

        # Modify MEMORY.md (wait for mtime change)
        time.sleep(0.1)
        (memory_dir / "MEMORY.md").write_text(
            "## Updated Section\nUpdated content for sync test\n",
            encoding="utf-8",
        )
        # Ensure mtime changed
        os.utime(str(memory_dir / "MEMORY.md"), None)

        result = store.sync_from_files()
        assert result["synced"] >= 1

    def test_sync_removes_deleted_file_entries(self, store, memory_dir):
        """Test that sync removes entries for deleted files."""
        store.build_index()

        # Delete USER.md
        os.remove(str(memory_dir / "USER.md"))

        result = store.sync_from_files()
        # Deleted file entries should be removed
        cursor = store._conn.execute(
            "SELECT COUNT(*) FROM memory_content WHERE file_key = 'user'"
        )
        count = cursor.fetchone()[0]
        assert count == 0


# =============================================================================
# Stats Tests
# =============================================================================

class TestStats:
    """Test get_stats."""

    def test_stats_after_build(self, store):
        """Test stats after building index."""
        store.build_index()
        stats = store.get_stats()

        assert "total_entries" in stats
        assert stats["total_entries"] > 0
        assert "entries_per_file" in stats
        assert "db_size_bytes" in stats
        assert stats["db_size_bytes"] > 0

    def test_stats_empty_index(self, store):
        """Test stats with empty index."""
        stats = store.get_stats()
        assert stats["total_entries"] == 0


# =============================================================================
# Lifecycle Tests
# =============================================================================

class TestLifecycle:
    """Test MemoryFTSStore lifecycle."""

    def test_close_and_reuse(self, memory_dir):
        """Test closing and that connection is cleaned up."""
        db_path = str(memory_dir / ".lifecycle_test.db")
        store = MemoryFTSStore(str(memory_dir), db_path=db_path)
        store.build_index()
        store.close()

        assert store._conn is None

    def test_context_manager(self, memory_dir):
        """Test using MemoryFTSStore as context manager."""
        db_path = str(memory_dir / ".ctx_test.db")
        with MemoryFTSStore(str(memory_dir), db_path=db_path) as store:
            count = store.build_index()
            assert count > 0

        # After context exit, connection should be closed
        assert store._conn is None

    def test_custom_db_path(self, memory_dir):
        """Test using a custom database path."""
        custom_path = str(memory_dir / "custom" / "custom.db")
        store = MemoryFTSStore(str(memory_dir), db_path=custom_path)
        store.build_index()
        assert os.path.exists(custom_path)
        store.close()


# =============================================================================
# FTS5 Query Sanitization Tests
# =============================================================================

class TestQuerySanitization:
    """Test FTS5 query sanitization."""

    def test_simple_query(self, store):
        """Test sanitizing a simple query."""
        result = store._sanitize_fts_query("hello")
        assert result == '"hello"'

    def test_multi_word_query(self, store):
        """Test sanitizing a multi-word query (OR joined)."""
        result = store._sanitize_fts_query("hello world")
        assert '"hello"' in result
        assert '"world"' in result
        assert "OR" in result

    def test_empty_query(self, store):
        """Test sanitizing an empty query."""
        result = store._sanitize_fts_query("")
        assert result == ""

    def test_special_chars_removed(self, store):
        """Test that FTS5 special characters are removed."""
        result = store._sanitize_fts_query('test"AND*OR')
        # Should not contain raw special characters
        assert '"' not in result or result.startswith('"')
        # AND/OR should be removed (are FTS5 keywords)
        assert '"AND"' not in result

    def test_multi_word_query_sanitized(self, store):
        """Test sanitizing a multi-word query - words are OR-joined for FTS5."""
        result = store._sanitize_fts_query("Personality Description")
        # Multi-word query is OR-joined
        assert '"Personality"' in result
        assert '"Description"' in result
        assert "OR" in result
