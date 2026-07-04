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

"""Task Supervisor — thread-based background task lifecycle manager.

Runs long-running tasks in daemon threads, monitors heartbeat to detect
stuck tasks, reports progress via file IPC, and recovers zombie tasks
after worker restarts. Designed to be checked every poll cycle by the
main worker loop (Worker / AsyncWorker).
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger('EITElite.task_supervisor')

HEARTBEAT_MAX_IDLE = 120  # seconds without heartbeat = assumed stuck


class TaskSupervisor:
    """Manages background task threads. Main thread calls check_tasks() every cycle."""

    def __init__(self, workspace: str):
        self.workspace = workspace
        self._tasks: dict[str, threading.Thread] = {}
        self._abort_flags: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    # ── State file paths ──────────────────────────────────────────
    def _task_dir(self, task_id: str) -> Path:
        d = Path(self.workspace) / 'tasks' / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _heartbeat_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / 'heartbeat'

    def _progress_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / 'progress'

    def _state_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / 'state.json'

    # ── Heartbeat ─────────────────────────────────────────────────
    def touch_heartbeat(self, task_id: str):
        """Called by task thread to update heartbeat."""
        try:
            self._heartbeat_path(task_id).write_text(str(time.time()))
        except Exception as e:
            logger.warning('heartbeat write failed for %s: %s', task_id, e)

    def heartbeat_age(self, task_id: str) -> Optional[float]:
        """Seconds since last heartbeat. None if missing."""
        try:
            ts = float(self._heartbeat_path(task_id).read_text().strip())
            return time.time() - ts
        except (FileNotFoundError, ValueError):
            return None

    def heartbeat_ok(self, task_id: str, max_idle: int = HEARTBEAT_MAX_IDLE) -> bool:
        age = self.heartbeat_age(task_id)
        return age is not None and age < max_idle

    # ── Progress ──────────────────────────────────────────────────
    def set_progress(self, task_id: str, text: str):
        """Called by task thread to report progress text."""
        try:
            self._progress_path(task_id).write_text(text)
        except Exception as e:
            logger.warning('progress write failed for %s: %s', task_id, e)

    def get_progress(self, task_id: str) -> str:
        """Read current progress text."""
        try:
            return self._progress_path(task_id).read_text().strip()
        except FileNotFoundError:
            return ''

    def get_all_progress(self) -> list[dict]:
        """Return progress for all running tasks. Used by main thread."""
        results = []
        with self._lock:
            for task_id in list(self._tasks.keys()):
                prog = self.get_progress(task_id)
                age = self.heartbeat_age(task_id)
                results.append({
                    'task_id': task_id,
                    'progress': prog,
                    'heartbeat_age': age,
                    'running': self.is_running(task_id),
                })
        return results

    # ── Thread lifecycle ──────────────────────────────────────────
    def start_task(self, task_id: str, target: Callable, args: tuple = ()):
        """Launch task in a new daemon thread."""
        with self._lock:
            if task_id in self._tasks and self._tasks[task_id].is_alive():
                logger.warning('task %s already running, ignoring', task_id)
                return

            abort_flag = threading.Event()
            self._abort_flags[task_id] = abort_flag

            # Write initial state
            state_path = self._state_path(task_id)
            try:
                with open(state_path, 'w') as f:
                    json.dump({'status': 'running', 'task_id': task_id}, f)
            except Exception as e:
                logger.warning('state write failed for %s: %s', task_id, e)

            self.touch_heartbeat(task_id)

            def _wrapper(*wrapper_args):
                try:
                    target(*wrapper_args, supervisor=self, abort_flag=abort_flag)
                except Exception as e:
                    logger.error('task %s crashed: %s', task_id, e)
                    try:
                        with open(self._state_path(task_id), 'w') as f:
                            json.dump({'status': 'crashed', 'error': str(e), 'task_id': task_id}, f)
                    except Exception:
                        pass
                finally:
                    with self._lock:
                        self._tasks.pop(task_id, None)
                        self._abort_flags.pop(task_id, None)
                    try:
                        self._heartbeat_path(task_id).write_text('0')
                    except Exception:
                        pass

            t = threading.Thread(target=_wrapper, args=args, daemon=True, name=f'task-{task_id}')
            t.start()
            self._tasks[task_id] = t
            logger.info('started task %s (alive=%s)', task_id, t.is_alive())

    def abort_task(self, task_id: str) -> bool:
        """Signal task to abort. Returns True if flag was set."""
        with self._lock:
            flag = self._abort_flags.get(task_id)
            if flag is None:
                return False
            flag.set()
        return True

    def is_running(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            return t is not None and t.is_alive()

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.is_alive())

    def list_active(self) -> list[str]:
        with self._lock:
            return [tid for tid, t in self._tasks.items() if t.is_alive()]

    # ── Main thread check ─────────────────────────────────────────
    def check_tasks(self) -> list[dict]:
        """Called by main thread each poll cycle.

        Returns list of events: completed, crashed, timed out.
        """
        events = []
        with self._lock:
            for task_id in list(self._tasks.keys()):
                t = self._tasks[task_id]
                if not t.is_alive():
                    try:
                        with open(self._state_path(task_id)) as f:
                            state = json.load(f)
                    except (FileNotFoundError, json.JSONDecodeError):
                        state = {'status': 'unknown'}
                    events.append({
                        'task_id': task_id,
                        'event': state.get('status', 'unknown'),
                        'state': state,
                    })
                    self._tasks.pop(task_id, None)
                    self._abort_flags.pop(task_id, None)
                    continue

                age = self.heartbeat_age(task_id)
                if age is not None and age > HEARTBEAT_MAX_IDLE:
                    logger.warning('task %s heartbeat stale (%.0fs), killing', task_id, age)
                    self.abort_task(task_id)
                    events.append({
                        'task_id': task_id,
                        'event': 'timeout',
                        'heartbeat_age': age,
                    })

        return events

    # ── Recovery after restart ────────────────────────────────────
    def find_zombie_tasks(self) -> list[dict]:
        """On worker startup, find tasks/ dir entries with pending/running state."""
        tasks_dir = Path(self.workspace) / 'tasks'
        if not tasks_dir.exists():
            return []
        zombies = []
        for d in tasks_dir.iterdir():
            if not d.is_dir():
                continue
            state_file = d / 'state.json'
            if not state_file.exists():
                continue
            try:
                with open(state_file) as f:
                    state = json.load(f)
                if state.get('status') in ('running', 'pending'):
                    zombies.append(state)
            except (json.JSONDecodeError, Exception):
                pass
        return zombies
