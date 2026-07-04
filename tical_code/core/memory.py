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
# Original repository: https://github.com/zizetu/tical-agent
#

"""Memory - persistent key-value storage."""

# DESIGNED-NOT-DEAD: Persistent key-value memory. Superseded by memory_store.py (FTS5) for search, but still valid for simple KV use. DO NOT DELETE without checking memory_store coverage.


import json
import re
import time
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, TypeVar
from enum import Enum
import logging

logger = logging.getLogger(__name__)

# =============================================================================
# Memory Types
# =============================================================================

class MemoryType(Enum):
    """Types of memory entries."""
    EPISODIC = "episodic"      # Specific events/experiences
    SEMANTIC = "semantic"      # General knowledge/facts
    PROCEDURAL = "procedural"   # How to do things
    WORKING = "working"        # Short-term, temporary
    VERIFICATION = "verification"  # Verification results

# =============================================================================
# Memory Entry
# =============================================================================

@dataclass
class MemoryEntry:
    """A single memory entry."""
    memory_type: MemoryType
    key: str
    value: Any
    
    # Metadata
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    access_count: int = 0
    
    # Skeletonization
    summary: Optional[str] = None
    original_size: Optional[int] = None
    compressed_size: Optional[int] = None
    
    # Trust & Verification
    verified: bool = False
    verification_method: Optional[str] = None
    confidence: float = 0.5
    
    # Expiration
    ttl: Optional[int] = None  # seconds, None = never
    
    def is_expired(self) -> bool:
        """Check if memory has expired."""
        if self.ttl is None:
            return False
        return time.time() > (self.created_at + self.ttl)
    
    def touch(self):
        """Update access time and count."""
        self.accessed_at = time.time()
        self.access_count += 1
    
    def get_age(self) -> float:
        """Get age in seconds."""
        return time.time() - self.created_at
    
    def get_fingerprint(self) -> str:
        """Get a unique fingerprint."""
        content = f"{self.memory_type.value}:{self.key}:{self.created_at}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'memory_type': self.memory_type.value,
            'key': self.key,
            'value': self.value,
            'created_at': self.created_at,
            'accessed_at': self.accessed_at,
            'access_count': self.access_count,
            'summary': self.summary,
            'original_size': self.original_size,
            'compressed_size': self.compressed_size,
            'verified': self.verified,
            'verification_method': self.verification_method,
            'confidence': self.confidence,
            'ttl': self.ttl,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'MemoryEntry':
        """Create from dictionary."""
        return cls(
            memory_type=MemoryType(data['memory_type']),
            key=data['key'],
            value=data['value'],
            created_at=data.get('created_at', time.time()),
            accessed_at=data.get('accessed_at', time.time()),
            access_count=data.get('access_count', 0),
            summary=data.get('summary'),
            original_size=data.get('original_size'),
            compressed_size=data.get('compressed_size'),
            verified=data.get('verified', False),
            verification_method=data.get('verification_method'),
            confidence=data.get('confidence', 0.5),
            ttl=data.get('ttl'),
        )

# =============================================================================
# Skeletonization Strategies
# =============================================================================

class SkeletonStrategy(Enum):
    """How to skeletonize memory."""
    NONE = "none"              # Keep full content
    SUMMARY = "summary"        # Keep summary only
    REFERENCE = "reference"    # Keep reference/pointer only
    COMPRESS = "compress"      # Compress content
    PRUNE = "prune"            # Delete non-essential

class Skeletonizer:
    """
    Skeletonization logic for compressing memories.
    
    Implements "forget most, keep structure" philosophy.
    """
    
    # Default summarizer - can be conn = sqlite3.connect(self.db_path, timeout=30.0)d with LLM-based
    _summarizer: Optional[Callable[[str, int], str]] = None
    
    @classmethod
    def set_summarizer(cls, func: Callable[[str, int], str]):
        """Set a custom summarizer function."""
        cls._summarizer = func
    
    @classmethod
    def summarize(cls, content: Any, max_length: int = 200) -> str:
        """
        Summarize content to max_length characters.
        
        Args:
            content: Content to summarize
            max_length: Maximum summary length
            
        Returns:
            Summary string
        """
        # Convert to string
        if isinstance(content, dict):
            text = json.dumps(content, sort_keys=True)
        elif isinstance(content, list):
            text = json.dumps(content, sort_keys=True)
        else:
            text = str(content)
        
        # Use custom summarizer if available
        if cls._summarizer:
            return cls._summarizer(text, max_length)
        
        # Simple truncation with ellipsis
        if len(text) <= max_length:
            return text
        
        # Extract key parts
        if len(text) > max_length * 3:
            # Very long content - aggressive skeletonization
            return text[:max_length - 3] + "..."
        
        return text
    
    @classmethod
    def skeletonize_entry(cls, entry: MemoryEntry, strategy: SkeletonStrategy) -> MemoryEntry:
        """
        Apply skeletonization strategy to a memory entry.
        
        Args:
            entry: Memory entry to skeletonize
            strategy: Strategy to apply
            
        Returns:
            Modified memory entry (may be same object or new)
        """
        # Calculate sizes
        content = json.dumps(entry.value, sort_keys=True, default=str)
        entry.original_size = len(content)
        
        if strategy == SkeletonStrategy.NONE:
            entry.compressed_size = entry.original_size
            return entry
        
        if strategy == SkeletonStrategy.SUMMARY:
            entry.summary = cls.summarize(content, max_length=200)
            entry.value = {"_skeletonized": True, "summary": entry.summary}
            entry.compressed_size = len(entry.summary)
        
        elif strategy == SkeletonStrategy.REFERENCE:
            # Keep only reference info
            entry.value = {
                "_skeletonized": True,
                "_type": "reference",
                "key": entry.key,
                "fingerprint": entry.get_fingerprint(),
            }
            entry.compressed_size = len(json.dumps(entry.value))
        
        elif strategy == SkeletonStrategy.COMPRESS:
            # TODO: Implement actual compression
            entry.summary = cls.summarize(content, max_length=100)
            entry.compressed_size = len(entry.summary)
        
        elif strategy == SkeletonStrategy.PRUNE:
            # Keep only essential metadata
            entry.value = {
                "_skeletonized": True,
                "_type": "pruned",
                "key": entry.key,
            }
            entry.compressed_size = len(json.dumps(entry.value))
        
        return entry
    
    @classmethod
    def calculate_compression_ratio(cls, entry: MemoryEntry) -> float:
        """Calculate compression ratio (0.0 to 1.0, lower = more compressed)."""
        if not entry.original_size or not entry.compressed_size:
            return 1.0
        if entry.original_size == 0:
            return 1.0
        return entry.compressed_size / entry.original_size

# =============================================================================
# Memory Store
# =============================================================================

T = TypeVar('T')

class MemoryStore:
    """
    Persistent memory storage with skeletonization.
    
    All plugin state MUST use this or extend it.
    """
    
    def __init__(
        self,
        store_file: Optional[str] = None,
        max_entries: int = 1000,
        default_ttl: Optional[int] = None,
        skeleton_strategy: SkeletonStrategy = SkeletonStrategy.SUMMARY,
    ):
        """
        Initialize Memory Store.
        
        Args:
            store_file: Path to persistence file
            max_entries: Maximum entries before pruning
            default_ttl: Default TTL for entries (seconds)
            skeleton_strategy: Default skeletonization strategy
        """
        self.store_file = store_file
        self.max_entries = max_entries
        self.default_ttl = default_ttl
        self.skeleton_strategy = skeleton_strategy
        
        self.entries: Dict[str, MemoryEntry] = {}
        self._access_order: List[str] = []  # LRU tracking
        
        self._load()
    
    def _load(self):
        """Load entries from file."""
        if not self.store_file or not os.path.exists(self.store_file):
            return
        
        try:
            with open(self.store_file, 'r') as f:
                data = json.load(f)
            
            for entry_data in data.get('entries', []):
                entry = MemoryEntry.from_dict(entry_data)
                # Skip expired entries
                if not entry.is_expired():
                    self.entries[entry.key] = entry
                    self._access_order.append(entry.key)
            
            logger.info(f"Loaded {len(self.entries)} memory entries")
        except Exception as e:
            logger.error(f"Failed to load memory: {e}")
    
    def _save(self):
        """Save entries to file."""
        if not self.store_file:
            return
        
        try:
            os.makedirs(os.path.dirname(self.store_file), exist_ok=True)
            with open(self.store_file, 'w') as f:
                json.dump({
                    'version': '0.3.0',
                    'saved_at': time.time(),
                    'entries': [e.to_dict() for e in self.entries.values()],
                }, f, indent=2)
            logger.debug(f"Saved {len(self.entries)} memory entries")
        except Exception as e:
            logger.error(f"Failed to save memory: {e}")
    
    def _update_access_order(self, key: str):
        """Update LRU access order."""
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)
    
    def set(
        self,
        key: str,
        value: Any,
        memory_type: MemoryType = MemoryType.EPISODIC,
        ttl: Optional[int] = None,
        summary: Optional[str] = None,
        verified: bool = False,
    ) -> MemoryEntry:
        """
        Store a memory entry.
        
        Args:
            key: Unique key for this memory
            value: Value to store
            memory_type: Type of memory
            ttl: Time to live (seconds), uses default if None
            summary: Pre-computed summary
            verified: Whether value has been verified
            
        Returns:
            Created memory entry
        """
        entry = MemoryEntry(
            memory_type=memory_type,
            key=key,
            value=value,
            ttl=ttl or self.default_ttl,
            summary=summary,
            verified=verified,
        )
        
        # Calculate sizes
        content = json.dumps(value, sort_keys=True, default=str)
        entry.original_size = len(content)
        entry.compressed_size = entry.original_size
        
        # Apply skeletonization if needed
        if len(self.entries) >= self.max_entries:
            entry = Skeletonizer.skeletonize_entry(entry, self.skeleton_strategy)
        
        self.entries[key] = entry
        self._update_access_order(key)
        self._save()
        
        return entry
    
    def get(self, key: str, touch: bool = True) -> Optional[Any]:
        """
        Retrieve a memory entry.
        
        Args:
            key: Key to retrieve
            touch: Update access time (default True)
            
        Returns:
            Stored value or None if not found/expired
        """
        entry = self.entries.get(key)
        
        if not entry:
            return None
        
        if entry.is_expired():
            self.delete(key)
            return None
        
        if touch:
            entry.touch()
            self._update_access_order(key)
        
        return entry.value
    
    def get_entry(self, key: str) -> Optional[MemoryEntry]:
        """Get the full memory entry (including metadata)."""
        entry = self.entries.get(key)
        if entry and not entry.is_expired():
            return entry
        return None
    
    def delete(self, key: str) -> bool:
        """Delete a memory entry."""
        if key in self.entries:
            del self.entries[key]
            if key in self._access_order:
                self._access_order.remove(key)
            self._save()
            return True
        return False
    
    def get_by_type(self, memory_type: MemoryType) -> List[MemoryEntry]:
        """Get all entries of a specific type."""
        return [
            e for e in self.entries.values()
            if e.memory_type == memory_type and not e.is_expired()
        ]
    
    def search(self, predicate: Callable[[MemoryEntry], bool]) -> List[MemoryEntry]:
        """Search entries using a predicate function."""
        return [e for e in self.entries.values() if predicate(e) and not e.is_expired()]
    
    def cleanup_expired(self):
        """Remove all expired entries."""
        expired_keys = [k for k, e in self.entries.items() if e.is_expired()]
        for key in expired_keys:
            del self.entries[key]
            if key in self._access_order:
                self._access_order.remove(key)
        
        if expired_keys:
            self._save()
            logger.info(f"Cleaned up {len(expired_keys)} expired memories")
        
        return len(expired_keys)
    
    def prune_lru(self, keep_count: int = 100):
        """
        Prune least recently used entries.
        
        Args:
            keep_count: Number of entries to keep
        """
        if len(self.entries) <= keep_count:
            return
        
        to_remove = self._access_order[:-keep_count]
        for key in to_remove:
            if key in self.entries:
                del self.entries[key]
        
        self._access_order = self._access_order[-keep_count:]
        self._save()
        logger.info(f"Pruned {len(to_remove)} LRU entries")
    
    def skeletonize_all(self, strategy: SkeletonStrategy = SkeletonStrategy.SUMMARY):
        """
        Apply skeletonization to all entries.
        
        Args:
            strategy: Strategy to apply
        """
        for entry in self.entries.values():
            if not entry.summary:  # Only skeletonize if not already done
                Skeletonizer.skeletonize_entry(entry, strategy)
        
        self._save()
        logger.info(f"Applied {strategy.value} skeletonization to all entries")
    
    def get_stats(self) -> Dict:
        """Get memory store statistics."""
        entries = list(self.entries.values())
        total_original = sum(e.original_size or 0 for e in entries)
        total_compressed = sum(e.compressed_size or 0 for e in entries)
        
        return {
            'total_entries': len(entries),
            'by_type': {
                mt.value: len([e for e in entries if e.memory_type == mt])
                for mt in MemoryType
            },
            'verified_count': sum(1 for e in entries if e.verified),
            'total_original_size': total_original,
            'total_compressed_size': total_compressed,
            'compression_ratio': total_compressed / total_original if total_original > 0 else 1.0,
            'avg_access_count': sum(e.access_count for e in entries) / len(entries) if entries else 0,
        }
    
    def export(self) -> Dict:
        """Export all memories as dictionary."""
        return {
            'version': '0.3.0',
            'exported_at': time.time(),
            'stats': self.get_stats(),
            'entries': [e.to_dict() for e in self.entries.values()],
        }

# =============================================================================
# Global Memory Store
# =============================================================================

_global_store: Optional[MemoryStore] = None

def get_memory_store(
    store_file: Optional[str] = None,
    **kwargs
) -> MemoryStore:
    """Get or create the global memory store."""
    global _global_store
    if _global_store is None:
        _global_store = MemoryStore(store_file, **kwargs)
    return _global_store

def reset_memory_store():
    """Reset the global memory store (for testing)."""
    global _global_store
    _global_store = None

# =============================================================================
# Persistent Memory (v3 DoD - Long-term Memory)
# =============================================================================

class PersistentMemory:
    """ """
    
    SCHEMA_VERSION = 1
    
    def __init__(self, db_path: str = "~/.EITElite/memory.db"):
        """
        Initialize persistent memory.
        
        Args:
            db_path: Path to SQLite database
        """
        import sqlite3
        self.db_path = os.path.expanduser(db_path)
        
        # Ensure directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        
        # Initialize database
        self._init_db()
        
        logger.info(f"[PersistentMemory] Initialized at {self.db_path}")
    
    def _get_conn(self):
        """Get database connection."""
        import sqlite3
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    
    def _init_db(self):
        """Initialize database schema."""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                category TEXT DEFAULT 'fact',
                priority INTEGER DEFAULT 5,
                access_count INTEGER DEFAULT 0,
                created_at REAL,
                updated_at REAL,
                last_accessed_at REAL
            )
        """)
        
        # Create indexes
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_category 
            ON memories(category)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_access_count 
            ON memories(access_count DESC)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_priority 
            ON memories(priority DESC)
        """)
        
        conn.commit()
        conn.close()
        
        logger.debug("[PersistentMemory] Database schema initialized")
    
    def store(
        self,
        key: str,
        value: str,
        category: str = "fact",
        priority: int = 5
    ) -> bool:
        """ """
        import sqlite3
        now = time.time()
        
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            # Check if key exists for preserving access_count
            cursor.execute("SELECT access_count, created_at FROM memories WHERE key = ?", (key,))
            row = cursor.fetchone()
            
            if row:
                # Update existing
                access_count = row[0]
                created_at = row[1]
                cursor.execute("""
                    UPDATE memories 
                    SET value = ?, category = ?, priority = ?, 
                        updated_at = ?, last_accessed_at = ?
                    WHERE key = ?
                """, (value, category, priority, now, now, key))
            else:
                # Insert new
                cursor.execute("""
                    INSERT INTO memories 
                    (key, value, category, priority, access_count, created_at, updated_at, last_accessed_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """, (key, value, category, priority, now, now, now))
            
            conn.commit()
            conn.close()

            # Update semantic index
            self._on_store(key, value)

            logger.debug(f"[PersistentMemory] Stored: {key} ({category})")
            return True
            
        except Exception as e:
            logger.error(f"[PersistentMemory] Failed to store {key}: {e}")
            return False
    
    def recall(
        self,
        key: str = None,
        category: str = None,
        query: str = None,
        limit: int = 10
    ) -> List[Dict]:
        """ """
        import sqlite3
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if key:
                # Exact key match
                cursor.execute("""
                    SELECT * FROM memories WHERE key = ?
                """, (key,))
            elif category:
                # Category filter
                cursor.execute("""
                    SELECT * FROM memories 
                    WHERE category = ?
                    ORDER BY priority DESC, last_accessed_at DESC
                    LIMIT ?
                """, (category, limit))
            elif query:
                # Text search in value
                cursor.execute("""
                    SELECT * FROM memories 
                    WHERE value LIKE ?
                    ORDER BY priority DESC, access_count DESC
                    LIMIT ?
                """, (f'%{query}%', limit))
            else:
                # Return most recent/important
                cursor.execute("""
                    SELECT * FROM memories 
                    ORDER BY priority DESC, last_accessed_at DESC
                    LIMIT ?
                """, (limit,))
            
            rows = cursor.fetchall()
            conn.close()
            
            # Update access counts for non-key queries
            if not key:
                self._update_access_counts([dict(row)['key'] for row in rows])
            
            return [dict(row) for row in rows]
            
        except Exception as e:
            logger.error(f"[PersistentMemory] Failed to recall: {e}")
            return []
    
    def _update_access_counts(self, keys: List[str]):
        """Update access counts for recalled items."""
        if not keys:
            return
        
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            now = time.time()
            
            placeholders = ','.join('?' * len(keys))
            cursor.execute(f"""
                UPDATE memories 
                SET access_count = access_count + 1,
                    last_accessed_at = ?
                WHERE key IN ({placeholders})
            """, [now] + keys)
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[PersistentMemory] Failed to update access: {e}")
    
    def forget(self, key: str) -> bool:
        """ """
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            cursor.execute("SELECT key FROM memories WHERE key = ?", (key,))
            if not cursor.fetchone():
                conn.close()
                return False
            
            cursor.execute("DELETE FROM memories WHERE key = ?", (key,))
            conn.commit()
            conn.close()

            # Update semantic index
            self._on_forget(key)

            logger.debug(f"[PersistentMemory] Forgot: {key}")
            return True
            
        except Exception as e:
            logger.error(f"[PersistentMemory] Failed to forget {key}: {e}")
            return False
    
    def get_context_for_session(self, max_items: int = 20) -> str:
        """ """
        import sqlite3
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT * FROM memories 
                WHERE priority >= 5
                ORDER BY priority DESC, access_count DESC
                LIMIT ?
            """, (max_items,))
            
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                return ""
            
            lines = ["# Long-term Memory Context", ""]
            
            # Group by category
            by_category: Dict[str, List[Dict]] = {}
            for row in rows:
                mem = dict(row)
                cat = mem.get('category', 'fact')
                if cat not in by_category:
                    by_category[cat] = []
                by_category[cat].append(mem)
            
            for cat, mems in by_category.items():
                lines.append(f"## {cat.title()}")
                for mem in mems:
                    lines.append(f"- {mem['key']}: {mem['value']}")
                lines.append("")
            
            return "\n".join(lines).strip()
            
        except Exception as e:
            logger.error(f"[PersistentMemory] Failed to get context: {e}")
            return ""
    
    def get_stats(self) -> Dict:
        """Get memory statistics."""
        import sqlite3
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) as total FROM memories")
            total = cursor.fetchone()[0]  # Use index instead of key
            
            cursor.execute("SELECT category, COUNT(*) as count FROM memories GROUP BY category")
            by_category = {row[0]: row[1] for row in cursor.fetchall()}  # Use index
            
            cursor.execute("SELECT AVG(priority) as avg_priority FROM memories")
            avg_priority = cursor.fetchone()[0] or 0
            
            cursor.execute("SELECT SUM(access_count) as total_access FROM memories")
            total_access = cursor.fetchone()[0] or 0
            
            conn.close()
            
            return {
                'total_memories': total,
                'by_category': by_category,
                'avg_priority': round(avg_priority, 2),
                'total_access_count': total_access,
            }
            
        except Exception as e:
            logger.error(f"[PersistentMemory] Failed to get stats: {e}")
            return {}
    
    def clear_all(self) -> bool:
        """Clear all memories (use with caution)."""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM memories")
            conn.commit()
            conn.close()
            
            logger.warning("[PersistentMemory] Cleared all memories")
            return True
            
        except Exception as e:
            logger.error(f"[PersistentMemory] Failed to clear: {e}")
            return False
    
    def store_memory_file(self, category: str, content: str, source_file: str) -> bool:
        """ """
        import sqlite3
        now = time.time()
        key = f"memory_file:{category}"

        try:
            conn = self._get_conn()
            cursor = conn.cursor()

            # Checkis
            cursor.execute("SELECT key FROM memories WHERE key = ?", (key,))
            if cursor.fetchone():
                # Update
                cursor.execute("""
                    UPDATE memories 
                    SET value = ?, category = ?, priority = ?,
                        updated_at = ?, last_accessed_at = ?
                    WHERE key = ?
                """, (content, f"memory_file:{category}", 8, now, now, key))
            else:
                # 
                cursor.execute("""
                    INSERT INTO memories 
                    (key, value, category, priority, access_count, created_at, updated_at, last_accessed_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """, (key, content, f"memory_file:{category}", 8, now, now, now))

            conn.commit()
            conn.close()

            logger.debug(f"[PersistentMemory] Stored memory file: {category} from {source_file}")
            return True

        except Exception as e:
            logger.error(f"[PersistentMemory] Failed to store memory file {category}: {e}")
            return False

    def search_by_keywords(self, query: str, top_k: int = 5) -> List[Dict]:
        """ """
        import sqlite3

        if not query:
            return []

        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # (/,)
            keywords = re.findall(r'[\w\u4e00-\u9fff]+', query.lower())
            keywords = [k for k in keywords if len(k) > 1]

            if not keywords:
                return []

            #  LIKE  -  keyword  value or key
            conditions = []
            params = []
            for kw in keywords:
                conditions.append("(value LIKE ? OR key LIKE ?)")
                params.extend([f'%{kw}%', f'%{kw}%'])

            where_clause = " OR ".join(conditions)

            cursor.execute(f"""
                SELECT * FROM memories 
                WHERE {where_clause}
                ORDER BY priority DESC, access_count DESC
                LIMIT ?
            """, params + [top_k * 2])  # 

            rows = cursor.fetchall()
            conn.close()

            # Compute
            results = []
            for row in rows:
                mem = dict(row)
                value_lower = (mem.get('value', '') or '').lower()
                key_lower = (mem.get('key', '') or '').lower()

                # Compute
                match_count = 0
                for kw in keywords:
                    if kw in value_lower or kw in key_lower:
                        match_count += 1

                relevance = match_count / len(keywords) if keywords else 0
                mem['relevance'] = round(relevance, 4)
                results.append(mem)

            # 
            results.sort(key=lambda x: x.get('relevance', 0), reverse=True)

            return results[:top_k]

        except Exception as e:
            logger.error(f"[PersistentMemory] Keyword search failed: {e}")
            return []

    def semantic_search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
    ) -> List[Dict]:
        """
        Semantic search: find memories by meaning, not just keywords.
        
        Uses sentence-transformers + FAISS for embedding-based retrieval.
        Falls back to keyword search if embeddings are unavailable.
        
        Args:
            query: Natural language query (Chinese or English)
            top_k: Max results to return
            min_score: Minimum cosine similarity (0.0-1.0)
            
        Returns:
            List of memory dicts with 'semantic_score' field, sorted by score desc.
        """
        try:
            from .semantic_search import get_semantic_index
        except ImportError:
            logger.warning("[PersistentMemory] semantic_search module not available")
            return self.search_by_keywords(query, top_k)
        
        index = get_semantic_index(self.db_path)
        if index is None:
            return self.search_by_keywords(query, top_k)
        
        # Ensure index is built
        index._ensure_loaded()
        if index._model is None:
            logger.info("[SemanticSearch] Model unavailable, falling back to keywords")
            return self.search_by_keywords(query, top_k)
        
        # Rebuild if index is empty but DB has entries
        if len(index._keys) == 0:
            import sqlite3 as _sqlite3
            try:
                conn = _sqlite3.connect(self.db_path, check_same_thread=False)
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM memories")
                count = cursor.fetchone()[0]
                conn.close()
                if count > 0:
                    logger.info(f"[SemanticSearch] Empty index, rebuilding from {count} entries...")
                    index.rebuild_from_db(self.db_path)
            except Exception as e:
                logger.error(f"[SemanticSearch] Rebuild check failed: {e}")
        
        # Run semantic search
        hits = index.search(query, top_k=top_k, min_score=min_score)
        if not hits:
            # No semantic matches - fall back to keywords
            return self.search_by_keywords(query, top_k)
        
        # Fetch full memory entries from DB
        import sqlite3 as _sqlite3
        results = []
        try:
            conn = _sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = _sqlite3.Row
            cursor = conn.cursor()
            
            for key, score in hits:
                cursor.execute("SELECT * FROM memories WHERE key = ?", (key,))
                row = cursor.fetchone()
                if row:
                    mem = dict(row)
                    mem['semantic_score'] = round(score, 4)
                    results.append(mem)
                    self._update_access_counts([key])
            
            conn.close()
        except Exception as e:
            logger.error(f"[SemanticSearch] DB fetch failed: {e}")
        
        return results
    
    def reindex_semantic(self) -> int:
        """
        Rebuild the semantic embedding index from all memories.
        
        Returns:
            Number of entries indexed.
        """
        try:
            from .semantic_search import get_semantic_index
            index = get_semantic_index(self.db_path)
            if index is None:
                return 0
            index.rebuild_from_db(self.db_path)
            return len(index._keys)
        except ImportError:
            logger.warning("[PersistentMemory] semantic_search module not available")
            return 0
    
    def _on_store(self, key: str, value: str):
        """Update semantic index after storing a memory (called by store())."""
        try:
            from .semantic_search import get_semantic_index
            index = get_semantic_index(self.db_path)
            if index and index._model is not None:
                text = f"{key}: {value}"
                index.upsert(key, text)
        except (ImportError, Exception):
            pass  # Silent - semantic search is optional
    
    def _on_forget(self, key: str):
        """Update semantic index after forgetting a memory."""
        try:
            from .semantic_search import get_semantic_index
            index = get_semantic_index(self.db_path)
            if index:
                index.remove(key)
        except (ImportError, Exception):
            pass
    
# =============================================================================
# Global Persistent Memory Instance
# =============================================================================

_global_persistent_memory: Optional['PersistentMemory'] = None

def get_persistent_memory(db_path: str = "~/.EITElite/memory.db") -> 'PersistentMemory':
    """Get or create the global persistent memory instance."""
    global _global_persistent_memory
    if _global_persistent_memory is None:
        _global_persistent_memory = PersistentMemory(db_path)
    return _global_persistent_memory

def reset_persistent_memory():
    """Reset the global persistent memory (for testing)."""
    global _global_persistent_memory
    _global_persistent_memory = None
