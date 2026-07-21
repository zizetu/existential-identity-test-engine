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

"""Delivery Ledger — persistent write-ahead ledger for outbound message delivery.

The ledger records every final response created by the model before it's
sent to the transport channel.  If the process crashes between "response
generated" and "response delivered", the ledger survives and the next
gateway/worker instance redelivers the pending obligation.

Lifecycle:
  1. create_obligation(chat_id, content, message_ref) — write-ahead record
  2. mark_delivered(obligation_id) — set state to 'delivered'
  3. mark_failed(obligation_id, error) — set state to 'failed'
  4. redeliver_pending() — called at startup, re-sends all pending/failed

Guarantees:
  - Write-ahead: obligation is persisted BEFORE send() is called
  - At-most-once: duplicates are prevented via (chat_id, content_hash) pair
  - Stuck-message drain: obligations older than MAX_AGE are abandoned
  - Bail-out after MAX_RETRIES: prevents infinite re-delivery loops
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tical-code.delivery_ledger")

MAX_RETRIES = 3
MAX_AGE = 86400        # 24 hours — abandon older obligations
_LOCK = threading.Lock()
_DB_PATH: Optional[Path] = None


def _get_db_path() -> Path:
    """Return the path to delivery_ledger.db under TICAL_HOME."""
    global _DB_PATH
    if _DB_PATH is not None:
        return _DB_PATH

    home = os.environ.get(
        "TICAL_HOME",
        os.environ.get("EITE_DATA_DIR", str(Path.home() / ".tical")),
    )
    path = Path(home) / "delivery_ledger.db"
    _DB_PATH = path
    return path


def _init_table():
    """Create the delivery_obligations table if it doesn't exist."""
    db = _get_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_obligations (
                    obligation_id   TEXT PRIMARY KEY,
                    chat_id         TEXT NOT NULL,
                    content         TEXT NOT NULL,
                    message_ref     TEXT NOT NULL DEFAULT '',
                    state           TEXT NOT NULL DEFAULT 'pending',
                    attempts        INTEGER NOT NULL DEFAULT 0,
                    last_error      TEXT DEFAULT NULL,
                    owner_pid       INTEGER DEFAULT NULL,
                    owner_started   REAL DEFAULT NULL,
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def _obligation_id(chat_id: str, content: str, message_ref: str = "") -> str:
    """Deterministic ID from (chat_id, message_ref, content) for dedup."""
    raw = f"{chat_id}|{message_ref}|{content}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def create_obligation(
    chat_id: str, content: str, message_ref: str = ""
) -> str:
    """Write-ahead: record a delivery obligation BEFORE sending.

    Returns:
        The obligation_id (deterministic, safe for dedup).
    """
    _init_table()
    oid = _obligation_id(chat_id, content, message_ref)
    now = time.time()
    pid = os.getpid()

    with _LOCK:
        conn = sqlite3.connect(str(_get_db_path()))
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO delivery_obligations
                    (obligation_id, chat_id, content, message_ref,
                     state, attempts, owner_pid, owner_started,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """,
                (oid, chat_id, content, message_ref, pid, now, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    logger.debug("[ledger] created obligation %s (chat=%s)", oid[:12], chat_id)
    return oid


def mark_delivered(obligation_id: str) -> bool:
    """Mark obligation as delivered.

    Returns True if the obligation existed and was updated.
    """
    if not obligation_id:
        return False
    now = time.time()
    with _LOCK:
        conn = sqlite3.connect(str(_get_db_path()))
        try:
            cur = conn.execute(
                """
                UPDATE delivery_obligations
                SET state='delivered', updated_at=?, attempts=attempts+1
                WHERE obligation_id=? AND state != 'abandoned'
                """,
                (now, obligation_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def mark_failed(obligation_id: str, error: str = "") -> bool:
    """Mark obligation as failed after an unsuccessful send attempt.

    If attempts exceed MAX_RETRIES, marks as 'abandoned'.
    Returns True if the obligation existed and was updated.
    """
    if not obligation_id:
        return False
    now = time.time()
    with _LOCK:
        conn = sqlite3.connect(str(_get_db_path()))
        try:
            # Increment attempts and determine new state
            cur = conn.execute(
                """
                UPDATE delivery_obligations
                SET state=CASE
                        WHEN attempts >= ? THEN 'abandoned'
                        ELSE 'failed'
                    END,
                    attempts=attempts+1,
                    last_error=?,
                    updated_at=?
                WHERE obligation_id=? AND state NOT IN ('delivered', 'abandoned')
                """,
                (MAX_RETRIES, error[:200], now, obligation_id),
            )
            conn.commit()
            if cur.rowcount > 0:
                logger.warning(
                    "[ledger] obligation %s failed: %s", obligation_id[:12], error[:60]
                )
            return cur.rowcount > 0
        finally:
            conn.close()


def load_pending() -> list:
    """Return all pending/failed obligations not yet abandoned or delivered.

    Returns:
        List of dicts: {obligation_id, chat_id, content, message_ref, attempts}
    """
    _init_table()
    cutoff = time.time() - MAX_AGE
    with _LOCK:
        conn = sqlite3.connect(str(_get_db_path()))
        try:
            cur = conn.execute(
                """
                SELECT obligation_id, chat_id, content, message_ref, attempts
                FROM delivery_obligations
                WHERE state IN ('pending', 'failed')
                  AND attempts < ?
                  AND created_at > ?
                ORDER BY created_at ASC
                """,
                (MAX_RETRIES, cutoff),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

    return [
        {
            "obligation_id": r[0],
            "chat_id": r[1],
            "content": r[2],
            "message_ref": r[3],
            "attempts": r[4],
        }
        for r in rows
    ]


def abandon_expired() -> int:
    """Mark obligations older than MAX_AGE as abandoned.

    Returns:
        Number of obligations abandoned.
    """
    _init_table()
    cutoff = time.time() - MAX_AGE
    with _LOCK:
        conn = sqlite3.connect(str(_get_db_path()))
        try:
            cur = conn.execute(
                """
                UPDATE delivery_obligations
                SET state='abandoned', updated_at=?
                WHERE state IN ('pending', 'failed') AND created_at < ?
                """,
                (time.time(), cutoff),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def get_obligation(obligation_id: str) -> Optional[dict]:
    """Return a single delivery obligation by ID.

    Args:
        obligation_id: The SHA-256 hash obligation identifier.

    Returns:
        A dict with all columns, or None if not found.
    """
    if not obligation_id:
        return None
    _init_table()
    with _LOCK:
        conn = sqlite3.connect(str(_get_db_path()))
        try:
            cur = conn.execute(
                """
                SELECT obligation_id, chat_id, content, message_ref,
                       state, attempts, last_error, owner_pid,
                       owner_started, created_at, updated_at
                FROM delivery_obligations
                WHERE obligation_id = ?
                """,
                (obligation_id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()

    if not row:
        return None
    return {
        "obligation_id": row[0],
        "chat_id": row[1],
        "content": row[2],
        "message_ref": row[3],
        "state": row[4],
        "attempts": row[5],
        "last_error": row[6],
        "owner_pid": row[7],
        "owner_started": row[8],
        "created_at": row[9],
        "updated_at": row[10],
    }


def ledger_health() -> dict:
    """Return health check info for the delivery ledger.

    Verifies the database is reachable, the table exists, and returns
    aggregate counts plus the path to the database file.

    Returns:
        A dict with keys: ok (bool), db_path, size_bytes, and
        summary stats (same as stats()).
    """
    _init_table()
    db_path = _get_db_path()
    info = {"db_path": str(db_path), "ok": True, "error": ""}
    try:
        info["size_bytes"] = db_path.stat().st_size
    except OSError:
        info["size_bytes"] = 0

    try:
        with _LOCK:
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.execute("PRAGMA integrity_check")
                integrity = cur.fetchone()[0]
                if integrity != "ok":
                    info["ok"] = False
                    info["error"] = f"integrity_check: {integrity}"
            finally:
                conn.close()

        if info["ok"]:
            s = stats()
            info.update(s)
    except Exception as e:
        info["ok"] = False
        info["error"] = str(e)

    return info


def vacuum() -> int:
    """Remove delivered obligations older than MAX_AGE.

    Returns:
        Number of rows deleted.
    """
    _init_table()
    cutoff = time.time() - MAX_AGE
    with _LOCK:
        conn = sqlite3.connect(str(_get_db_path()))
        try:
            cur = conn.execute(
                "DELETE FROM delivery_obligations WHERE state='delivered' AND updated_at < ?",
                (cutoff,),
            )
            conn.commit()
            conn.execute("VACUUM")
            return cur.rowcount
        finally:
            conn.close()


def stats() -> dict:
    """Return summary statistics for the delivery ledger.

    Returns:
        Dict with keys: total, pending, failed, delivered, abandoned.
    """
    _init_table()
    with _LOCK:
        conn = sqlite3.connect(str(_get_db_path()))
        try:
            cur = conn.execute(
                """
                SELECT state, COUNT(*) FROM delivery_obligations GROUP BY state
                """
            )
            counts = dict(cur.fetchall())
        finally:
            conn.close()

    return {
        "total": sum(counts.values()),
        "pending": counts.get("pending", 0),
        "failed": counts.get("failed", 0),
        "delivered": counts.get("delivered", 0),
        "abandoned": counts.get("abandoned", 0),
    }
