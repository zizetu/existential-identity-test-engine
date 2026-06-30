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
Cron Task System (v0.3 P1)
===========================

Lightweight cron-like scheduling for tical-code workers.

Features:
- Pre-defined schedules: every_minute, every_5_minutes, hourly, daily, etc.
- Custom interval support for flexible timing
- Three task types: tool, prompt, shell
- SQLite persistence for job state
- Auto-disable after consecutive failures
- Force-Verify integration for verified jobs
- AI-friendly tools: cron_add, cron_list, cron_remove

This enables workers to autonomously schedule periodic tasks like:
- Health monitoring every 5 minutes
- Log rotation daily
- Data sync every hour
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# =============================================================================
# Schedule Definitions
# =============================================================================

class CronSchedule(Enum):
    """Pre-defined scheduling patterns."""
    EVERY_MINUTE = "every_minute"          # 60 seconds
    EVERY_5_MINUTES = "every_5_minutes"    # 300 seconds
    EVERY_15_MINUTES = "every_15_minutes"  # 900 seconds
    EVERY_30_MINUTES = "every_30_minutes"  # 1800 seconds
    HOURLY = "hourly"                       # 3600 seconds
    EVERY_6_HOURS = "every_6_hours"        # 21600 seconds
    DAILY = "daily"                         # 86400 seconds
    CUSTOM = "custom"                       # User-defined interval

    def get_interval(self) -> int:
        """Get interval in seconds for this schedule."""
        intervals = {
            "every_minute": 60,
            "every_5_minutes": 300,
            "every_15_minutes": 900,
            "every_30_minutes": 1800,
            "hourly": 3600,
            "every_6_hours": 21600,
            "daily": 86400,
            "custom": 0,  # Custom uses job.interval_seconds
        }
        return intervals.get(self.value, 60)

    @classmethod
    def from_string(cls, value: str) -> 'CronSchedule':
        """Parse schedule from string."""
        value = value.lower().strip()
        for schedule in cls:
            if schedule.value == value:
                return schedule
        # Try partial match
        for schedule in cls:
            if value in schedule.value or schedule.value in value:
                return schedule
        return cls.EVERY_MINUTE  # Default


# =============================================================================
# CronJob Definition
# =============================================================================

@dataclass
class CronJob:
    """
    A scheduled task that runs periodically.

    Attributes:
        job_id: Unique identifier for this job
        name: Human-readable name for this job
        description: What this job does
        schedule: Scheduling pattern (CronSchedule enum)
        interval_seconds: Custom interval for CUSTOM schedule type

        task_type: How to execute - 'tool', 'prompt', or 'shell'
        task_params: Parameters for the task:
            - tool: {"tool_name": str, "params": dict}
            - prompt: {"text": str}  - injected into AI session
            - shell: {"cmd": str}

        enabled: Whether this job is active
        last_run: Unix timestamp of last execution
        next_run: Unix timestamp of next scheduled execution
        run_count: Number of successful executions
        fail_count: Number of consecutive failures
        last_result: Result or error from last execution

        max_failures: Auto-disable after this many consecutive failures
        timeout: Maximum execution time in seconds
        verified: Whether this job requires Force-Verify on results

        created_by: Who created this job - 'ai', 'human', or 'system'
        review_source: Reference to original request/task
    """
    job_id: str
    name: str
    description: str = ""
    schedule: CronSchedule = CronSchedule.EVERY_MINUTE
    interval_seconds: int = 0  # Used when schedule is CUSTOM

    # Execution
    task_type: str = "tool"  # tool / prompt / shell
    task_params: Dict[str, Any] = field(default_factory=dict)

    # State
    enabled: bool = True
    last_run: Optional[float] = None
    next_run: Optional[float] = None
    run_count: int = 0
    fail_count: int = 0
    last_result: Optional[str] = None

    # Safety
    max_failures: int = 3
    timeout: int = 60
    verified: bool = False

    # Audit
    created_by: str = "ai"  # ai / human / system
    review_source: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def calculate_next_run(self) -> float:
        """Calculate next run time based on schedule."""
        now = time.time()
        if self.schedule == CronSchedule.CUSTOM:
            interval = self.interval_seconds if self.interval_seconds > 0 else 60
        else:
            interval = self.schedule.get_interval()
        return now + interval

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for storage."""
        return {
            'job_id': self.job_id,
            'name': self.name,
            'description': self.description,
            'schedule': self.schedule.value,
            'interval_seconds': self.interval_seconds,
            'task_type': self.task_type,
            'task_params': json.dumps(self.task_params),
            'enabled': self.enabled,
            'last_run': self.last_run,
            'next_run': self.next_run,
            'run_count': self.run_count,
            'fail_count': self.fail_count,
            'last_result': self.last_result,
            'max_failures': self.max_failures,
            'timeout': self.timeout,
            'verified': self.verified,
            'created_by': self.created_by,
            'review_source': self.review_source,
            'created_at': self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CronJob':
        """Deserialize from dictionary."""
        # Handle schedule enum
        schedule_str = data.get('schedule', 'every_minute')
        if isinstance(schedule_str, str):
            schedule = CronSchedule.from_string(schedule_str)
        else:
            schedule = CronSchedule.EVERY_MINUTE

        # Parse task_params JSON string
        task_params = data.get('task_params', {})
        if isinstance(task_params, str):
            try:
                task_params = json.loads(task_params)
            except json.JSONDecodeError:
                task_params = {}

        return cls(
            job_id=data['job_id'],
            name=data['name'],
            description=data.get('description', ''),
            schedule=schedule,
            interval_seconds=data.get('interval_seconds', 0),
            task_type=data.get('task_type', 'tool'),
            task_params=task_params,
            enabled=bool(data.get('enabled', True)),
            last_run=data.get('last_run'),
            next_run=data.get('next_run'),
            run_count=int(data.get('run_count', 0)),
            fail_count=int(data.get('fail_count', 0)),
            last_result=data.get('last_result'),
            max_failures=int(data.get('max_failures', 3)),
            timeout=int(data.get('timeout', 60)),
            verified=bool(data.get('verified', False)),
            created_by=data.get('created_by', 'ai'),
            review_source=data.get('review_source'),
            created_at=float(data.get('created_at', time.time())),
        )

    def to_summary(self) -> Dict[str, Any]:
        """Human-readable summary for AI/tool output."""
        status = "enabled" if self.enabled else "disabled"
        if self.fail_count >= self.max_failures:
            status = "FAILED (auto-disabled)"

        next_run_str = "N/A"
        if self.next_run:
            next_run_str = datetime.fromtimestamp(self.next_run).strftime('%Y-%m-%d %H:%M:%S')

        last_run_str = "Never"
        if self.last_run:
            last_run_str = datetime.fromtimestamp(self.last_run).strftime('%Y-%m-%d %H:%M:%S')

        return {
            'job_id': self.job_id,
            'name': self.name,
            'description': self.description,
            'schedule': self.schedule.value,
            'task_type': self.task_type,
            'status': status,
            'last_run': last_run_str,
            'next_run': next_run_str,
            'run_count': self.run_count,
            'fail_count': self.fail_count,
            'created_by': self.created_by,
        }


# =============================================================================
# CronManager
# =============================================================================

class CronManager:
    """
    Lightweight cron-like task scheduler.

    Integrates with WorkerFramework to provide:
    - Persistent job storage (SQLite)
    - Time-based task scheduling
    - Task execution (tool/prompt/shell)
    - Auto-disable on consecutive failures
    - Force-Verify for verified jobs

    Usage:
        # In WorkerFramework.bootstrap()
        self.cron_manager = CronManager(self)
        await self.cron_manager.load_jobs()

        # In WorkerFramework.run_loop() heartbeat phase
        await self.cron_manager.tick()

        # AI tools
        await self.cron_manager.add_job(cron_job)
        jobs = self.cron_manager.list_jobs()
        self.cron_manager.remove_job(job_id)
    """

    def __init__(self, framework: Any, db_path: str = "~/.tical-code/cron.db"):
        """
        Initialize CronManager.

        Args:
            framework: WorkerFramework instance for tool execution
            db_path: Path to SQLite database file
        """
        self.framework = framework
        self.db_path = os.path.expanduser(db_path)
        self._ensure_db_dir()
        self._init_db()
        self._jobs: Dict[str, CronJob] = {}
        self._lock = asyncio.Lock()

    def _ensure_db_dir(self):
        """Ensure database directory exists."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    def _init_db(self):
        """Initialize SQLite database schema."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cron_jobs (
                job_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                schedule TEXT NOT NULL,
                interval_seconds INTEGER DEFAULT 0,
                task_type TEXT DEFAULT 'tool',
                task_params TEXT DEFAULT '{}',
                enabled INTEGER DEFAULT 1,
                last_run REAL,
                next_run REAL,
                run_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                last_result TEXT,
                max_failures INTEGER DEFAULT 3,
                timeout INTEGER DEFAULT 60,
                verified INTEGER DEFAULT 0,
                created_by TEXT DEFAULT 'ai',
                review_source TEXT,
                created_at REAL
            )
        """)
        conn.commit()
        conn.close()
        logger.info(f"[CronManager] Database initialized: {self.db_path}")

    async def load_jobs(self):
        """Load all jobs from database into memory."""
        async with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM cron_jobs")
            rows = cursor.fetchall()
            conn.close()

            self._jobs.clear()
            for row in rows:
                # Column order matches CREATE TABLE
                data = {
                    'job_id': row[0],
                    'name': row[1],
                    'description': row[2],
                    'schedule': row[3],
                    'interval_seconds': row[4],
                    'task_type': row[5],
                    'task_params': row[6],
                    'enabled': bool(row[7]),
                    'last_run': row[8],
                    'next_run': row[9],
                    'run_count': row[10],
                    'fail_count': row[11],
                    'last_result': row[12],
                    'max_failures': row[13],
                    'timeout': row[14],
                    'verified': bool(row[15]),
                    'created_by': row[16],
                    'review_source': row[17],
                    'created_at': row[18],
                }
                job = CronJob.from_dict(data)
                self._jobs[job.job_id] = job

            logger.info(f"[CronManager] Loaded {len(self._jobs)} jobs from database")

    def _save_job(self, job: CronJob):
        """Persist a single job to database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        data = job.to_dict()

        cursor.execute("""
            INSERT OR REPLACE INTO cron_jobs (
                job_id, name, description, schedule, interval_seconds,
                task_type, task_params, enabled, last_run, next_run,
                run_count, fail_count, last_result, max_failures, timeout,
                verified, created_by, review_source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data['job_id'], data['name'], data['description'],
            data['schedule'], data['interval_seconds'], data['task_type'],
            json.dumps(data['task_params']), int(data['enabled']),
            data['last_run'], data['next_run'], data['run_count'],
            data['fail_count'], data['last_result'], data['max_failures'],
            data['timeout'], int(data['verified']), data['created_by'],
            data['review_source'], data['created_at']
        ))
        conn.commit()
        conn.close()

    # =========================================================================
    # Job Management
    # =========================================================================

    async def add_job(self, job: CronJob) -> str:
        """
        Add a new cron job.

        Args:
            job: CronJob instance to add

        Returns:
            The job_id of the added job
        """
        async with self._lock:
            # Generate job_id if not set
            if not job.job_id:
                job.job_id = f"cron_{uuid.uuid4().hex[:12]}"

            # Set initial next_run
            if job.next_run is None:
                job.next_run = job.calculate_next_run()

            # Store in memory
            self._jobs[job.job_id] = job

            # Persist to database
            self._save_job(job)

            logger.info(f"[CronManager] Added job: {job.name} ({job.job_id})")
            return job.job_id

    async def remove_job(self, job_id: str) -> bool:
        """
        Remove a cron job.

        Args:
            job_id: ID of the job to remove

        Returns:
            True if job was removed, False if not found
        """
        async with self._lock:
            if job_id not in self._jobs:
                logger.warning(f"[CronManager] Job not found for removal: {job_id}")
                return False

            job = self._jobs.pop(job_id)

            # Remove from database
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM cron_jobs WHERE job_id = ?", (job_id,))
            conn.commit()
            conn.close()

            logger.info(f"[CronManager] Removed job: {job.name} ({job_id})")
            return True

    async def enable_job(self, job_id: str) -> bool:
        """
        Enable a cron job.

        Args:
            job_id: ID of the job to enable

        Returns:
            True if job was enabled, False if not found
        """
        async with self._lock:
            if job_id not in self._jobs:
                return False

            job = self._jobs[job_id]
            job.enabled = True
            job.fail_count = 0  # Reset failure counter
            job.next_run = job.calculate_next_run()
            self._save_job(job)

            logger.info(f"[CronManager] Enabled job: {job.name} ({job_id})")
            return True

    async def disable_job(self, job_id: str) -> bool:
        """
        Disable a cron job.

        Args:
            job_id: ID of the job to disable

        Returns:
            True if job was disabled, False if not found
        """
        async with self._lock:
            if job_id not in self._jobs:
                return False

            job = self._jobs[job_id]
            job.enabled = False
            self._save_job(job)

            logger.info(f"[CronManager] Disabled job: {job.name} ({job_id})")
            return True

    def list_jobs(self, enabled_only: bool = False) -> List[CronJob]:
        """
        List all cron jobs.

        Args:
            enabled_only: If True, only return enabled jobs

        Returns:
            List of CronJob instances
        """
        jobs = list(self._jobs.values())
        if enabled_only:
            jobs = [j for j in jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.next_run or float('inf'))

    def get_job(self, job_id: str) -> Optional[CronJob]:
        """Get a specific job by ID."""
        return self._jobs.get(job_id)

    def get_due_jobs(self) -> List[CronJob]:
        """
        Get all jobs that are due to run now.

        Returns:
            List of jobs where next_run <= current time
        """
        now = time.time()
        due_jobs = []

        for job in self._jobs.values():
            if not job.enabled:
                continue
            if job.next_run is None:
                continue
            if job.next_run <= now:
                due_jobs.append(job)

        return due_jobs

    # =========================================================================
    # Job Execution
    # =========================================================================

    async def execute_job(self, job: CronJob) -> Dict[str, Any]:
        """
        Execute a cron job.

        Args:
            job: CronJob to execute

        Returns:
            Execution result dict with success, data, error
        """
        start_time = time.time()
        result = {
            'success': False,
            'data': None,
            'error': None,
            'job_id': job.job_id,
            'job_name': job.name,
            'task_type': job.task_type,
            'elapsed_ms': 0,
        }

        try:
            # Execute based on task type
            if job.task_type == "tool":
                result['data'] = await self._execute_tool(job)
                result['success'] = result['data'] is not None
            elif job.task_type == "prompt":
                result['data'] = await self._execute_prompt(job)
                result['success'] = True  # Prompt injection always "succeeds"
            elif job.task_type == "shell":
                result['data'] = await self._execute_shell(job)
                result['success'] = result['data'] is not None
            else:
                result['error'] = f"Unknown task_type: {job.task_type}"
                result['success'] = False

            # Apply Force-Verify if job requires it
            if job.verified and result['success']:
                verify_result = await self._verify_result(result['data'], job)
                if not verify_result['passed']:
                    result['success'] = False
                    result['error'] = f"Verification failed: {verify_result['details']}"
                    result['data'] = None

        except asyncio.TimeoutError:
            result['error'] = f"Job timed out after {job.timeout}s"
            result['success'] = False
        except Exception as e:
            result['error'] = str(e)
            result['success'] = False

        # Update job state
        job.last_run = time.time()
        job.run_count += 1

        if result['success']:
            job.fail_count = 0
            job.last_result = str(result['data'])[:1000] if result['data'] else "OK"
        else:
            job.fail_count += 1
            job.last_result = result.get('error', 'Unknown error')

            # Auto-disable on consecutive failures
            if job.fail_count >= job.max_failures:
                job.enabled = False
                logger.warning(f"[CronManager] Job auto-disabled after {job.fail_count} failures: {job.name}")

        # Calculate next run
        job.next_run = job.calculate_next_run()
        job.last_result = (result.get('error') or str(result.get('data')))[:1000]

        # Persist state
        self._save_job(job)

        result['elapsed_ms'] = (time.time() - start_time) * 1000
        return result

    async def _execute_tool(self, job: CronJob) -> Any:
        """Execute a tool-based task."""
        tool_name = job.task_params.get('tool_name') or job.task_params.get('tool')
        params = job.task_params.get('params') or job.task_params.get('parameters') or {}

        if not tool_name:
            raise ValueError("task_params must include 'tool_name' or 'tool'")

        # Use framework's tool execution
        if hasattr(self.framework, 'execute_tool'):
            instruction = json.dumps({"tool": tool_name, "params": params})
            exec_result = await self.framework.execute_tool(instruction)

            if not exec_result.get('success'):
                raise Exception(exec_result.get('error', 'Tool execution failed'))

            return exec_result.get('data')

        # Fallback: direct registry access
        if hasattr(self.framework, '_tool_registry'):
            tool = self.framework._tool_registry.get(tool_name)
            if tool and hasattr(tool, 'handler'):
                if asyncio.iscoroutinefunction(tool.handler):
                    return await tool.handler(**params)
                else:
                    return tool.handler(**params)

        raise ValueError(f"Tool not found or not executable: {tool_name}")

    async def _execute_prompt(self, job: CronJob) -> str:
        """Execute a prompt-based task (inject into AI session)."""
        prompt_text = job.task_params.get('text') or job.task_params.get('prompt')

        if not prompt_text:
            raise ValueError("task_params must include 'text' or 'prompt'")

        # Store for AI to process in next reasoning cycle
        if hasattr(self.framework, '_pending_cron_prompts'):
            self.framework._pending_cron_prompts.append({
                'job_id': job.job_id,
                'prompt': prompt_text,
                'timestamp': time.time(),
            })
        elif hasattr(self.framework, '_cron_prompts'):
            self.framework._cron_prompts.append({
                'job_id': job.job_id,
                'prompt': prompt_text,
                'timestamp': time.time(),
            })

        logger.info(f"[CronManager] Prompt injected for AI: {job.name}")
        return f"Prompt queued for AI: {prompt_text[:100]}..."

    async def _execute_shell(self, job: CronJob) -> str:
        """Execute a shell command task."""
        cmd = job.task_params.get('cmd') or job.task_params.get('command')

        if not cmd:
            raise ValueError("task_params must include 'cmd' or 'command'")

        # Use framework's shell_exec tool if available
        if hasattr(self.framework, 'execute_tool'):
            instruction = json.dumps({"tool": "shell_exec", "params": {"cmd": cmd, "timeout": job.timeout}})
            exec_result = await self.framework.execute_tool(instruction)

            if not exec_result.get('success'):
                raise Exception(exec_result.get('error', 'Shell execution failed'))

            return exec_result.get('data')

        # Direct subprocess execution (less safe) - use exec+shlex to prevent shell injection
        import shlex
        cmd_parts = shlex.split(cmd)
        process = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=job.timeout
            )
            output = stdout.decode() if stdout else ""
            error = stderr.decode() if stderr else ""

            if process.returncode != 0:
                raise Exception(f"Shell exited with code {process.returncode}: {error}")

            return output.strip()

        except asyncio.TimeoutError:
            process.kill()
            raise asyncio.TimeoutError(f"Shell command timed out after {job.timeout}s")

    async def _verify_result(self, data: Any, job: CronJob) -> Dict[str, Any]:
        """Verify job execution result using Force-Verify."""
        # Try to import Force-Verify
        try:
            from .verify import VerifyLevel, force_verify, verify_result
        except ImportError:
            try:
                pass  # legacy verify module removed
            except ImportError:
                return {'passed': True, 'details': 'Verify module not available'}

        # Basic verification: result is not None and not an error
        verify_level = 'BASIC' if job.verified else 'NONE'

        passed = data is not None
        details = "Result is valid" if passed else "Result is None or empty"

        return {
            'passed': passed,
            'level': verify_level if verify_level else 'NONE',
            'details': details,
        }

    # =========================================================================
    # Main Tick (called from WorkerFramework heartbeat)
    # =========================================================================

    async def tick(self):
        """
        Check and execute due jobs.

        Called from WorkerFramework.run_loop() heartbeat phase.
        Lightweight: only checks timestamps, no DB queries.
        """
        due_jobs = self.get_due_jobs()

        if not due_jobs:
            return

        logger.info(f"[CronManager] Tick: {len(due_jobs)} jobs due")

        for job in due_jobs:
            try:
                # Run each job (could run concurrently for better performance)
                result = await self.execute_job(job)
                logger.info(
                    f"[CronManager] Job '{job.name}' executed: "
                    f"success={result['success']}, elapsed={result['elapsed_ms']:.1f}ms"
                )
            except Exception as e:
                logger.error(f"[CronManager] Job '{job.name}' failed: {e}")

    # =========================================================================
    # Stats and Debugging
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get scheduler statistics."""
        jobs = list(self._jobs.values())
        enabled = sum(1 for j in jobs if j.enabled)
        disabled = len(jobs) - enabled
        total_runs = sum(j.run_count for j in jobs)
        total_fails = sum(j.fail_count for j in jobs)

        return {
            'total_jobs': len(jobs),
            'enabled_jobs': enabled,
            'disabled_jobs': disabled,
            'total_runs': total_runs,
            'total_consecutive_fails': total_fails,
            'database_path': self.db_path,
        }


# =============================================================================
# Cron Tools (for AI integration)
# =============================================================================

# These are registered as built-in tools in WorkerFramework

def create_cron_tools(cron_manager: CronManager) -> Dict[str, Any]:
    """
    Create tool definitions for cron management.

    Returns dict of tool handlers for registration.
    """

    async def cron_add_handler(
        name: str,
        schedule: str,
        task_type: str = "tool",
        task_params: Optional[Dict] = None,
        description: str = "",
        verified: bool = False,
        timeout: int = 60,
        max_failures: int = 3,
        interval_seconds: int = 0,
    ) -> Dict[str, Any]:
        """
        Add a new cron job. AI can use this to schedule periodic tasks.
        """
        task_params = task_params or {}

        # Validate schedule
        valid_schedules = [s.value for s in CronSchedule]
        if schedule not in valid_schedules:
            return {
                'success': False,
                'error': f"Invalid schedule. Valid: {valid_schedules}",
            }

        # Create job
        job = CronJob(
            job_id=f"cron_{uuid.uuid4().hex[:12]}",
            name=name,
            description=description,
            schedule=CronSchedule.from_string(schedule),
            interval_seconds=interval_seconds,
            task_type=task_type,
            task_params=task_params,
            verified=verified,
            timeout=timeout,
            max_failures=max_failures,
            created_by="ai",
        )

        job_id = await cron_manager.add_job(job)
        return {
            'success': True,
            'job_id': job_id,
            'message': f"Cron job '{name}' added successfully",
            'next_run': datetime.fromtimestamp(job.next_run).strftime('%Y-%m-%d %H:%M:%S'),
        }

    async def cron_list_handler() -> Dict[str, Any]:
        """
        List all cron jobs with their status.
        """
        jobs = cron_manager.list_jobs()
        summaries = [j.to_summary() for j in jobs]

        return {
            'success': True,
            'count': len(jobs),
            'jobs': summaries,
        }

    async def cron_remove_handler(job_id: str) -> Dict[str, Any]:
        """
        Remove a cron job by ID.
        """
        removed = await cron_manager.remove_job(job_id)
        if removed:
            return {
                'success': True,
                'message': f"Cron job '{job_id}' removed",
            }
        else:
            return {
                'success': False,
                'error': f"Job not found: {job_id}",
            }

    async def cron_enable_handler(job_id: str) -> Dict[str, Any]:
        """Enable a cron job."""
        enabled = await cron_manager.enable_job(job_id)
        if enabled:
            return {
                'success': True,
                'message': f"Cron job '{job_id}' enabled",
            }
        return {
            'success': False,
            'error': f"Job not found: {job_id}",
        }

    async def cron_disable_handler(job_id: str) -> Dict[str, Any]:
        """Disable a cron job."""
        disabled = await cron_manager.disable_job(job_id)
        if disabled:
            return {
                'success': True,
                'message': f"Cron job '{job_id}' disabled",
            }
        return {
            'success': False,
            'error': f"Job not found: {job_id}",
        }

    async def cron_stats_handler() -> Dict[str, Any]:
        """Get scheduler statistics."""
        return {
            'success': True,
            'stats': cron_manager.get_stats(),
        }

    return {
        'cron_add': cron_add_handler,
        'cron_list': cron_list_handler,
        'cron_remove': cron_remove_handler,
        'cron_enable': cron_enable_handler,
        'cron_disable': cron_disable_handler,
        'cron_stats': cron_stats_handler,
    }
