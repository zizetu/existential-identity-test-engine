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
# Original repository: https://github.com/zizetu/existential-identity-test-engine
#

"""
Session Snapshot & Death Log - EITE Evaluation Worker Lifecycle
================================================================

P1: Session Persist - recover evaluation worker context from snapshot
P2: Death Record - record each exit's signal, cause, status

Design principles:
- Atomic write: write to .tmp first then os.rename, avoid half-written files
- Pure stdlib: json/os/time/signal, no external dependencies
- Backward compatibility: snapshot recover failure is non-blocking for boot
- Auto-cleanup: retain recent 5 snapshots, prevent disk bloat

EITE Version: 1.0.0
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Default paths
# =============================================================================

def _default_snapshot_dir() -> str:
    """Get the default snapshot directory path."""
    return os.path.expanduser("~/.eite/snapshots")


def _default_death_log_dir() -> str:
    """Get the default death log directory path."""
    return os.path.expanduser("~/.eite/death-log")


def _ensure_dir(path: str) -> bool:
    """Ensure directory exists. Returns whether successful."""
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError as e:
        logger.error("Failed to create directory %s: %s", path, e)
        return False


# =============================================================================
# P1: Session Snapshot - save and recover worker context
# =============================================================================

def save_snapshot(
    worker_name: str,
    data: Dict[str, Any],
    snapshot_dir: Optional[str] = None,
) -> bool:
    """
    Save evaluation worker session snapshot (atomic write).

    Write process:
    1. Serialize data to JSON
    2. Write temporary file {worker_name}-{timestamp}.json.tmp
    3. os.rename as formal file (atomic operation)

    Args:
        worker_name: Worker name
        data: Snapshot data to save (loop_count, uptime, last_error, status etc.)
        snapshot_dir: Snapshot directory path (default ~/.eite/snapshots)

    Returns:
        Whether save was successful
    """
    snapshot_dir = snapshot_dir or _default_snapshot_dir()

    if not _ensure_dir(snapshot_dir):
        return False

    timestamp = int(time.time())
    # Avoid same-second override: if file already exists, add 1ms precision suffix
    filename = f"{worker_name}-{timestamp}.json"
    filepath = os.path.join(snapshot_dir, filename)
    # If same-name file already exists, add milliseconds suffix
    if os.path.exists(filepath):
        ms = int(time.time() * 1000) % 1000
        filename = f"{worker_name}-{timestamp}-{ms}.json"
        filepath = os.path.join(snapshot_dir, filename)
    tmp_path = filepath + ".tmp"

    try:
        # Add metadata
        data['_meta'] = {
            'worker_name': worker_name,
            'saved_at': timestamp,
            'saved_at_iso': time.strftime(
                '%Y-%m-%dT%H:%M:%S', time.localtime(timestamp)
            ),
            'version': '1.0',
        }

        # Atomic write: write to tmp first, then rename
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # os.rename is an atomic operation on the same filesystem
        os.rename(tmp_path, filepath)

        logger.info("Snapshot saved: %s", filepath)
        return True

    except Exception as e:
        logger.error("Snapshot save failed: %s", e)
        # Clean up temporary file
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        return False


def load_latest_snapshot(
    worker_name: str,
    snapshot_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Load the latest snapshot for a worker.

    Finds {worker_name}-*.json files, sorted by timestamp descending,
    returns the latest one. If load fails returns None (no exception raised).

    Args:
        worker_name: Worker name
        snapshot_dir: Snapshot directory path

    Returns:
        Latest snapshot data, or None
    """
    snapshot_dir = snapshot_dir or _default_snapshot_dir()

    if not os.path.isdir(snapshot_dir):
        logger.debug("Snapshot directory does not exist: %s", snapshot_dir)
        return None

    # Find all matching snapshot files
    snapshots = list_snapshots(worker_name, snapshot_dir)

    if not snapshots:
        logger.debug("No snapshot found for worker=%s", worker_name)
        return None

    # Get the latest one
    latest_path = snapshots[0]  # Already sorted by time descending

    try:
        with open(latest_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        logger.info("Loaded snapshot: %s", os.path.basename(latest_path))
        return data

    except json.JSONDecodeError as e:
        logger.warning("Snapshot JSON parse failed %s: %s", latest_path, e)
        return None
    except Exception as e:
        logger.warning("Snapshot load failed %s: %s", latest_path, e)
        return None


def list_snapshots(
    worker_name: str,
    snapshot_dir: Optional[str] = None,
) -> List[str]:
    """
    List all snapshot files for a worker (by timestamp descending).

    Args:
        worker_name: Worker name
        snapshot_dir: Snapshot directory path

    Returns:
        File path list, latest first
    """
    snapshot_dir = snapshot_dir or _default_snapshot_dir()

    if not os.path.isdir(snapshot_dir):
        return []

    prefix = f"{worker_name}-"
    result = []

    for fname in os.listdir(snapshot_dir):
        # Match {worker_name}-{timestamp}.json (exclude .tmp)
        if fname.startswith(prefix) and fname.endswith('.json') and not fname.endswith('.tmp'):
            filepath = os.path.join(snapshot_dir, fname)
            if os.path.isfile(filepath):
                result.append(filepath)

    # Sort by modification time descending (latest first)
    result.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    return result


def cleanup_old_snapshots(
    worker_name: str,
    snapshot_dir: Optional[str] = None,
    keep: int = 5,
) -> int:
    """
    Clean up old snapshots, retain only the recent N.

    Args:
        worker_name: Worker name
        snapshot_dir: Snapshot directory path
        keep: Number of snapshots to retain (default 5)

    Returns:
        Number of deleted snapshots
    """
    snapshot_dir = snapshot_dir or _default_snapshot_dir()

    snapshots = list_snapshots(worker_name, snapshot_dir)

    if len(snapshots) <= keep:
        return 0

    # Retain the first 'keep' entries (latest), delete rest
    to_delete = snapshots[keep:]
    deleted = 0

    for filepath in to_delete:
        try:
            os.unlink(filepath)
            logger.debug("Deleted old snapshot: %s", os.path.basename(filepath))
            deleted += 1
        except OSError as e:
            logger.warning("Failed to delete snapshot %s: %s", filepath, e)

    if deleted > 0:
        logger.info(
            "Cleaned up %d old snapshots (retaining recent %d)", deleted, keep,
        )

    return deleted


def mark_snapshot_recovered(
    worker_name: str,
    snapshot_dir: Optional[str] = None,
) -> bool:
    """
    Mark the latest snapshot as already recovered (append .recovered suffix).

    This prevents repeated recovery of the same snapshot on subsequent starts.

    Args:
        worker_name: Worker name
        snapshot_dir: Snapshot directory path

    Returns:
        Whether mark was successful
    """
    snapshot_dir = snapshot_dir or _default_snapshot_dir()

    snapshots = list_snapshots(worker_name, snapshot_dir)
    if not snapshots:
        return False

    latest_path = snapshots[0]

    # Check whether already marked
    if latest_path.endswith('.recovered'):
        return True

    # Rename to .recovered
    recovered_path = latest_path + ".recovered"
    try:
        os.rename(latest_path, recovered_path)
        logger.info("Snapshot marked as recovered: %s", os.path.basename(recovered_path))
        return True
    except OSError as e:
        logger.warning("Failed to mark snapshot as recovered: %s", e)
        return False


# =============================================================================
# P2: Death Log - record worker exit cause
# =============================================================================

def record_death(
    worker_name: str,
    signal_type: Optional[int] = None,
    uptime: float = 0.0,
    loop_count: int = 0,
    last_error: Optional[str] = None,
    session_status: Optional[str] = None,
    traceback_str: Optional[str] = None,
    death_log_dir: Optional[str] = None,
) -> bool:
    """
    Record an evaluation worker death event.

    Signal type notes:
    - 15 (SIGTERM): Graceful termination
    - 2 (SIGINT): User Ctrl+C
    - 11 (SIGSEGV): Segmentation fault
    - 6 (SIGABRT): Abort signal
    - 0: Non-signal exit (Exception/Error)

    Args:
        worker_name: Worker name
        signal_type: Signal number (None indicates unknown)
        uptime: Run duration (seconds)
        loop_count: Main loop count
        last_error: Last error info
        session_status: Session status at exit
        traceback_str: Exception traceback (if any)
        death_log_dir: Death log directory path

    Returns:
        Whether record was successful
    """
    death_log_dir = death_log_dir or _default_death_log_dir()

    if not _ensure_dir(death_log_dir):
        return False

    # Signal name map
    signal_names = {
        2: 'SIGINT',
        6: 'SIGABRT',
        11: 'SIGSEGV',
        15: 'SIGTERM',
        9: 'SIGKILL',
    }
    import signal as _signal
    try:
        sig_name = _signal.Signals(signal_type).name if signal_type else 'unknown'
    except (ValueError, AttributeError):
        sig_name = signal_names.get(signal_type, f'SIGNAL_{signal_type}')

    timestamp = int(time.time())

    death_record = {
        'worker_name': worker_name,
        'timestamp': timestamp,
        'timestamp_iso': time.strftime(
            '%Y-%m-%dT%H:%M:%S', time.localtime(timestamp)
        ),
        'signal': signal_type,
        'signal_name': sig_name,
        'uptime_seconds': round(uptime, 2),
        'loop_count': loop_count,
        'last_error': last_error,
        'session_status': session_status,
        'version': '1.0',
    }

    # Optional field
    if traceback_str:
        death_record['traceback'] = traceback_str[:2000]  # Truncate to prevent bloat

    # Write file (append mode: one file per death, named by timestamp)
    filename = f"{worker_name}-death.json"
    filepath = os.path.join(death_log_dir, filename)
    tmp_path = filepath + ".tmp"

    try:
        # Read existing records (append mode)
        existing_records = []
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    existing_records = json.load(f)
                if not isinstance(existing_records, list):
                    existing_records = [existing_records]
            except (json.JSONDecodeError, OSError):
                existing_records = []

        # Append new record
        existing_records.append(death_record)

        # Only retain recent 50 records (prevent file bloat)
        if len(existing_records) > 50:
            existing_records = existing_records[-50:]

        # Atomic write
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(existing_records, f, ensure_ascii=False, indent=2)

        os.rename(tmp_path, filepath)

        logger.info(
            "Death record written: %s(%s), uptime=%.0fs, loops=%d",
            sig_name, signal_type, uptime, loop_count,
        )
        return True

    except Exception as e:
        logger.error("Death record write failed: %s", e)
        # Clean up temporary file
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        return False


def load_death_log(
    worker_name: str,
    death_log_dir: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Load worker death records.

    Args:
        worker_name: Worker name
        death_log_dir: Death log directory path
        limit: Maximum return entry count

    Returns:
        Death record list (latest first)
    """
    death_log_dir = death_log_dir or _default_death_log_dir()

    filename = f"{worker_name}-death.json"
    filepath = os.path.join(death_log_dir, filename)

    if not os.path.exists(filepath):
        return []

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            records = json.load(f)

        if not isinstance(records, list):
            records = [records]

        # Latest first
        records.reverse()
        return records[:limit]

    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Death log load failed: %s", e)
        return []


def get_death_summary(
    worker_name: str,
    death_log_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get worker death statistics summary.

    Returns:
        Summary containing total_deaths, last_death, signal_counts
    """
    records = load_death_log(worker_name, death_log_dir, limit=100)

    if not records:
        return {
            'worker_name': worker_name,
            'total_deaths': 0,
            'last_death': None,
            'signal_counts': {},
        }

    signal_counts = {}
    for r in records:
        sig = r.get('signal_name', 'unknown')
        signal_counts[sig] = signal_counts.get(sig, 0) + 1

    return {
        'worker_name': worker_name,
        'total_deaths': len(records),
        'last_death': records[0] if records else None,
        'signal_counts': signal_counts,
    }


# =============================================================================
# Convenience function: for Worker framework integration
# =============================================================================

def try_restore_snapshot(
    worker_name: str,
    snapshot_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Attempt to recover the latest snapshot for evaluation worker bootstrap.

    Design principle: Recovery failure is non-blocking for start,
    only records a warning.

    Args:
        worker_name: Worker name
        snapshot_dir: Snapshot directory path

    Returns:
        Recovered snapshot data, or None
    """
    try:
        data = load_latest_snapshot(worker_name, snapshot_dir)
        if data:
            # Check whether already recovered
            meta = data.get('_meta', {})
            saved_at = meta.get('saved_at', 0)

            # Mark as recovered
            mark_snapshot_recovered(worker_name, snapshot_dir)

            # Clean up old snapshots
            cleanup_old_snapshots(worker_name, snapshot_dir, keep=5)

            logger.info(
                "Snapshot recover success: worker=%s, saved_at=%s, loop_count=%s",
                worker_name,
                meta.get('saved_at_iso', '?'),
                data.get('loop_count', '?'),
            )
            return data
    except Exception as e:
        logger.warning(
            "Snapshot recover failed (does not affect startup): %s", e,
        )

    return None
