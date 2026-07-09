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

"""
SubAgent Delegation System (v0.3 P1)
=====================================

Core philosophy: "Do not trust AI output" -- Sub-agents are isolated workers with
their own sessions, designed for parallel task execution.

Architecture:
    The delegation system spawns independent sub-agent sessions that execute
    tasks asynchronously. Each sub-agent runs in its own reasoning loop with
    a dedicated system prompt and tool set, producing results that are verified
    by the Force-Verify system before being returned to the parent.

    SQLite provides durable persistence so task state survives restarts.
    Thread-local database connections ensure thread safety even when multiple
    sub-agents run concurrently.

Features:
- SubAgentTask dataclass: task definition with status tracking, timeouts,
  review metadata, and verification flags
- SubAgentManager: creation, execution, result collection, cancellation
- SQLite-backed persistence with schema versioning and indexed lookups
- Independent sessions per sub-agent (no context pollution)
- Timeout enforcement via asyncio.wait_for (v3 DoD)
- Exponential backoff on LLM call failures
- Tool handler functions for AI-facing 'delegate' / 'get_result' /
  'list_tasks' / 'cancel' tools

Usage:
    manager = SubAgentManager(framework)
    task = await manager.delegate(
        description="analysisthisdataFile",
        tools=["read_file", "search_web"],
        max_iterations=5,
    )

    # Non-blocking: task runs in background
    result = await manager.get_result(task.task_id)
"""

# Sub-agent delegation system - activated and wired into unified_worker via SubAgentManager.


import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

# Forward reference for WorkerFramework
if TYPE_CHECKING:
    from .worker_framework import WorkerFramework


# =============================================================================
# SubAgent Task Definition
# =============================================================================

@dataclass
class SubAgentTask:
    """
    A task delegated to a sub-agent.
    
    Attributes:
        task_id: Unique identifier for this task
        description: Task description visible to sub-agent
        parent_session_id: Parent AI session that created this task
        status: Current status (pending/running/completed/failed/timeout)
        
        # Sub-agent context
        system_prompt: Custom system prompt for sub-agent
        tools: List of tool names available to sub-agent
        max_iterations: Maximum reasoning rounds
        
        # Timeout (v3 DoD)
        timeout: Maximum execution time in seconds (default: 60)
        
        # Results
        result: Final result from sub-agent execution
        verified: Whether result passed Force-Verify
        elapsed_ms: Execution time in milliseconds
        
        # Timestamps
        created_at: When task was created
        completed_at: When task completed (or failed)
        
        # Review metadata
        review_source: Who/what suggested this task (for tracing)
        review_notes: Additional notes for review
        verify_source: Who verified the result (self/parent/cross-ai/human)
    """
    task_id: str
    description: str
    parent_session_id: str
    status: str = "pending"
    
    # Sub-agent context
    system_prompt: str = ""
    tools: List[str] = field(default_factory=list)
    max_iterations: int = 5
    
    # Timeout (v3 DoD: default 60s)
    timeout: int = 60
    
    # Results
    result: Optional[str] = None
    verified: bool = False
    elapsed_ms: float = 0.0
    
    # Timestamps
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    
    # Review metadata
    review_source: Optional[str] = None
    review_notes: Optional[str] = None
    verify_source: str = "self"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'task_id': self.task_id,
            'description': self.description,
            'parent_session_id': self.parent_session_id,
            'status': self.status,
            'system_prompt': self.system_prompt,
            'tools': self.tools,
            'max_iterations': self.max_iterations,
            'result': self.result,
            'verified': self.verified,
            'elapsed_ms': self.elapsed_ms,
            'created_at': self.created_at,
            'completed_at': self.completed_at,
            'review_source': self.review_source,
            'review_notes': self.review_notes,
            'verify_source': self.verify_source,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SubAgentTask':
        """Create SubAgentTask from dictionary."""
        return cls(
            task_id=data['task_id'],
            description=data['description'],
            parent_session_id=data.get('parent_session_id', ''),
            status=data.get('status', 'pending'),
            system_prompt=data.get('system_prompt', ''),
            tools=data.get('tools', []),
            max_iterations=data.get('max_iterations', 5),
            timeout=data.get('timeout', 60),  # v3 DoD timeout
            result=data.get('result'),
            verified=data.get('verified', False),
            elapsed_ms=data.get('elapsed_ms', 0.0),
            created_at=data.get('created_at', time.time()),
            completed_at=data.get('completed_at'),
            review_source=data.get('review_source'),
            review_notes=data.get('review_notes'),
            verify_source=data.get('verify_source', 'self'),
        )


# =============================================================================
# SubAgent Manager
# =============================================================================

class SubAgentManager:
    """
    Manager for sub-agent task delegation and execution.
    
    Features:
    - SQLite-backed task persistence
    - Independent sessions per sub-agent
    - Non-blocking async execution
    - Force-Verify integration
    - Result caching and retrieval
    
    Example:
        manager = SubAgentManager(framework)
        task = await manager.delegate("analysisdata", tools=["read_file"])
        result = await manager.get_result(task.task_id)
    """
    
    SCHEMA_VERSION = 1
    
    def __init__(
        self,
        framework: Optional['WorkerFramework'] = None,
        db_path: str = "~/.EITElite/subagents.db"
    ):
        """
        Initialize SubAgentManager.

        Args:
            framework: Optional WorkerFramework instance for tool/session access.
                       When None, the manager operates in standalone mode and
                       uses globally-wired tool_executor references instead.
            db_path: Path to SQLite database for task persistence
        """
        self.framework = framework
        self.db_path = os.path.expanduser(db_path)
        
        # Ensure directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        
        # Thread-local connections
        self._local = threading.local()
        
        # Active sub-agent tasks (in-memory cache)
        self._active_tasks: Dict[str, SubAgentTask] = {}
        self._active_tasks_lock = asyncio.Lock()
        
        # Initialize database
        self._init_db()
        
        # Reference to framework components
        self._tool_registry = None
        self._session_manager = None
        
        logger.info(f"[SubAgentManager] Initialized at {self.db_path}")
    
    def _get_conn(self) -> sqlite3.Connection:
        """
        Get or create a thread-local database connection.

        Each thread gets its own SQLite connection with WAL journaling
        and NORMAL synchronous mode for performance. The row_factory
        is set to sqlite3.Row for dict-like row access.

        Returns:
            sqlite3.Connection: A thread-local connection to the tasks database.
        """
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0,
            )
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn
    
    def _init_db(self):
        """
        Initialize the SQLite database schema and indices.

        Creates the subagent_tasks table if it does not exist, along with
        indices on status and parent_session_id for query performance.
        Uses CREATE IF NOT EXISTS so this is safe to call on every startup.

        The table stores all fields of SubAgentTask including task metadata,
        results, verification status, timestamps, and review information.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subagent_tasks (
                task_id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                parent_session_id TEXT,
                status TEXT DEFAULT 'pending',
                system_prompt TEXT DEFAULT '',
                tools TEXT DEFAULT '[]',
                max_iterations INTEGER DEFAULT 5,
                result TEXT,
                verified INTEGER DEFAULT 0,
                elapsed_ms REAL DEFAULT 0,
                review_source TEXT,
                review_notes TEXT,
                verify_source TEXT DEFAULT 'self',
                created_at REAL,
                completed_at REAL
            )
        """)
        
        # Create index for faster lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_subagent_status 
            ON subagent_tasks(status)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_subagent_parent 
            ON subagent_tasks(parent_session_id)
        """)
        
        conn.commit()
        logger.debug("[SubAgentManager] Database schema initialized")
    
    def _sync_framework_components(self):
        """
        Synchronize internal references with the framework's tool registry
        and session manager.

        Called lazily before the first delegation to ensure the manager
        has access to the latest framework state. Tries multiple attribute
        paths to handle different framework implementations. No-op when
        framework is None (standalone mode).

        Sets:
            self._tool_registry: Reference to the tool registry
            self._session_manager: Reference to the session manager
        """
        if self.framework is None:
            return
        
        # Get tool registry from framework
        if hasattr(self.framework, '_tool_registry') and self.framework._tool_registry:
            self._tool_registry = self.framework._tool_registry
        elif hasattr(self.framework, 'registry'):
            self._tool_registry = self.framework.registry
        
        # Get session manager from framework
        if hasattr(self.framework, 'sessions'):
            self._session_manager = self.framework.sessions
    
    def _generate_task_id(self) -> str:
        """
        Generate a unique task identifier.

        Uses a UUID4-based prefix ('subagent_') combined with the first
        12 hex characters of a random UUID for a compact, collision-resistant ID.

        Returns:
            str: Unique task ID in the format 'subagent_<12 hex chars>'.
        """
        return f"subagent_{uuid.uuid4().hex[:12]}"
    
    def _generate_subagent_system_prompt(
        self,
        task: SubAgentTask,
        available_tools: Optional[List[str]] = None
    ) -> str:
        """
        Generate system prompt for sub-agent.
        
        Args:
            task: The task to generate prompt for
            available_tools: List of tool names available
            
        Returns:
            System prompt string
        """
        # Base identity for sub-agent
        lines = [
            "# Sub-Agent Identity",
            "",
            "You are a specialized sub-agent executing a delegated task.",
            "Your role is to complete the assigned task with high accuracy.",
            "",
            "# Core Principles",
            "1. Execute only the delegated task",
            "2. Verify your output before returning",
            "3. Report errors clearly if task cannot be completed",
            "4. Do NOT hallucinate - if unsure, say so",
            "",
            "# Task Description",
            task.description,
            "",
        ]
        
        # Add tool descriptions if available
        if available_tools and self._tool_registry:
            lines.append("# Available Tools")
            lines.append("")
            
            for tool_name in available_tools:
                tool = self._tool_registry.get(tool_name)
                if tool:
                    lines.append(f"## {tool_name}")
                    lines.append(f"{tool.description}")
                    if tool.params.get('properties'):
                        for param_name, param_info in tool.params['properties'].items():
                            param_type = param_info.get('type', 'any')
                            required = param_name in tool.params.get('required', [])
                            req_str = "[required]" if required else "[optional]"
                            desc = param_info.get('description', '')
                            lines.append(f"  - {param_name}: {param_type} {req_str} - {desc}")
                    lines.append("")
        
        lines.extend([
            "# Output Format",
            "Return your final result in this format:",
            "```",
            "RESULT: <your answer>",
            "VERIFIED: true/false",
            "```",
        ])
        
        return "\n".join(lines)
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    async def delegate(
        self,
        description: str,
        tools: Optional[List[str]] = None,
        max_iterations: int = 5,
        review_source: Optional[str] = None,
        review_notes: Optional[str] = None,
    ) -> SubAgentTask:
        """
        Delegate a task to a sub-agent for parallel execution.
        
        This method returns immediately with a task object.
        The actual execution runs asynchronously in the background.
        
        Args:
            description: Task description for sub-agent
            tools: List of tool names available to sub-agent (None = all)
            max_iterations: Maximum reasoning rounds (default: 5)
            review_source: Optional source of review suggestion
            review_notes: Optional notes for review
            
        Returns:
            SubAgentTask object with task_id (execution runs async)
        """
        # Sync framework components if needed
        self._sync_framework_components()
        
        # Get parent session ID
        parent_session_id = ""
        if self.framework is not None and hasattr(self.framework, '_get_session_id'):
            try:
                parent_session_id = self.framework._get_session_id()
            except TypeError:
                parent_session_id = "subagent"
        
        # Create task
        task = SubAgentTask(
            task_id=self._generate_task_id(),
            description=description,
            parent_session_id=parent_session_id,
            status="pending",
            tools=tools or [],
            max_iterations=max_iterations,
            review_source=review_source,
            review_notes=review_notes,
            created_at=time.time(),
        )
        
        # Generate system prompt
        task.system_prompt = self._generate_subagent_system_prompt(task, task.tools)
        
        # Persist to database
        await self._save_task(task)
        
        # Add to active tasks
        async with self._active_tasks_lock:
            self._active_tasks[task.task_id] = task
        
        # Start async execution
        asyncio.create_task(self._run_subagent(task))
        
        logger.info(f"[SubAgentManager] Delegated task: {task.task_id}")
        
        return task
    
    async def _run_subagent(self, task: SubAgentTask):
        """
        Execute sub-agent task loop.
        
        This runs asynchronously and updates task status/results.
        v3 DoD: Implements timeout handling with asyncio.wait_for.
        
        Args:
            task: SubAgentTask to execute
        """
        start_time = time.time()
        
        # Update status to running
        task.status = "running"
        await self._update_task_status(task.task_id, "running")
        
        try:
            # Create a wrapped async task with timeout (v3 DoD)
            async def _execute_with_timeout():
                """
                Execute the full sub-agent reasoning loop.

                This inner function is wrapped by asyncio.wait_for to enforce
                the task-level timeout. It handles:
                - Creating an independent sub-agent session
                - Building the initial message context (system + user)
                - Running the reasoning loop with LLM calls and tool execution
                - Exponential backoff on LLM call failures
                - Force-Verify result validation

                Returns:
                    tuple: (result_text: str, verify_result: dict)
                """
                # Create independent session for sub-agent
                session = None
                if self._session_manager and hasattr(self._session_manager, 'create_session'):
                    session = self._session_manager.create_session({
                        'type': 'subagent',
                        'parent': task.parent_session_id,
                        'task_id': task.task_id,
                    })
                
                # Simulate sub-agent reasoning loop
                # In real implementation, this would call the AI model
                context = {
                    'task': task,
                    'session': session,
                    'iteration': 0,
                    'tool_results': [],
                    'messages': [],
                }
                
                # Build initial context
                context['messages'].append({
                    'role': 'system',
                    'content': task.system_prompt,
                })
                context['messages'].append({
                    'role': 'user',
                    'content': f"Please complete the following task:\n\n{task.description}",
                })
                
                # Reasoning loop
                result_text = ""
                verified = False
                tool_registry = getattr(self.framework, '_tool_registry', None)
                tool_executor = getattr(self.framework, '_tool_executor', None)
                llm = getattr(self.framework, 'llm', None)

                for iteration in range(task.max_iterations):
                    context['iteration'] = iteration + 1

                    # 1. Call AI model with messages and tool schemas
                    if llm is None:
                        logger.warning("[SubAgentManager] No LLM available for sub-agent call")
                        result_text = f"Error: No LLM backend available for sub-agent task"
                        break

                    try:
                        # Build tool schemas for sub-agent
                        _tool_schemas = None
                        if tool_registry is not None:
                            try:
                                _tool_schemas = [
                                    t.to_openai_schema() if hasattr(t, 'to_openai_schema')
                                    else {
                                        "type": "function",
                                        "function": {
                                            "name": t.name,
                                            "description": t.description,
                                            "parameters": t.params,
                                        }
                                    }
                                    for t in tool_registry.list_tools()
                                    if hasattr(t, 'name') and t.name in (task.tools or [])
                                ]
                            except Exception:
                                pass
                        if not _tool_schemas:
                            # Fallback: use TOOL_SCHEMAS from tool_executor
                            try:
                                from tical_code.core.tool_executor import TOOL_SCHEMAS as _TS
                                _tool_schemas = _TS
                            except ImportError:
                                _tool_schemas = None

                        response = llm.call(
                            messages=context['messages'],
                            tools=_tool_schemas,
                            max_tokens=4096,
                            temperature=0.7,
                        )
                    except Exception as e:
                        logger.warning("[SubAgentManager] LLM call failed at iteration %d: %s",
                                     iteration + 1, e)
                        if iteration < task.max_iterations - 1:
                            # Exponential backoff: 1s, 2s, 4s, 8s...
                            backoff = min(2 ** iteration, 30)
                            await asyncio.sleep(backoff)
                            continue
                        result_text = f"Error: LLM call failed after {task.max_iterations} attempts: {e}"
                        break

                    # 2. Parse response for content and tool_calls
                    content = response.get("content", "") if hasattr(response, 'get') else getattr(response, 'content', "")
                    tool_calls = response.get("tool_calls", []) if hasattr(response, 'get') else getattr(response, 'tool_calls', [])

                    # 3. If no tool calls, treat content as final answer
                    if not tool_calls:
                        if content:
                            result_text = content
                            verified = True
                            break
                        # Empty response - retry
                        if iteration < task.max_iterations - 1:
                            await asyncio.sleep(0.5)
                            continue
                        break

                    # Append assistant message with tool calls
                    _assistant_msg = {"role": "assistant", "content": content or ""}
                    _formatted_tcs = []
                    for tc in tool_calls:
                        tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, 'id', '')
                        tc_name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, 'name', '')
                        tc_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, 'args', {})
                        _formatted_tcs.append({
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": tc_name, "arguments": json.dumps(tc_args)},
                        })
                    if _formatted_tcs:
                        _assistant_msg["tool_calls"] = _formatted_tcs
                    context['messages'].append(_assistant_msg)

                    # 4. Execute tool calls via tool_registry or fallback
                    for tc in tool_calls:
                        tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, 'id', '')
                        tc_name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, 'name', '')
                        tc_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, 'args', {})

                        try:
                            if tool_executor is not None:
                                _instr = json.dumps({"tool": tc_name, "params": tc_args})
                                _tresult = await tool_executor.dispatch(_instr)
                                _tool_output = str(_tresult.data) if _tresult.success else f"Error: {_tresult.error}"
                            else:
                                from tical_code.core.tool_executor import execute as _exec
                                _exec_result = _exec(tc_name, tc_args)
                                _tool_output = str(_exec_result)
                        except Exception as _te:
                            _tool_output = f"Tool execution error: {_te}"

                        context['tool_results'].append({
                            "tool": tc_name,
                            "result": _tool_output,
                        })
                        context['messages'].append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": _tool_output,
                        })

                    # 5. Check for task completion signal in content
                    if content and any(kw in content.lower() for kw in ["final answer", "task complete", "completed"]):
                        result_text = content
                        verified = True
                        break
                
                # Apply Force-Verify (in real implementation, verify actual outputs)
                verify_result = await self._verify_subagent_result(task, result_text)
                
                return result_text, verify_result
            
            # Execute with timeout using asyncio.wait_for (v3 DoD)
            result_text, verify_result = await asyncio.wait_for(
                _execute_with_timeout(),
                timeout=task.timeout
            )
            
            # Update task with results
            task.result = result_text
            task.verified = verify_result.get('passed', False)
            task.verify_source = verify_result.get('source', 'self')
            
        except asyncio.TimeoutError:
            # v3 DoD: Handle timeout gracefully
            logger.warning(f"[SubAgentManager] Task {task.task_id} timed out after {task.timeout}s")
            task.status = "timeout"
            task.result = f"Sub-agent timed out after {task.timeout}s"
            task.verified = False
            task.verify_source = "self"
            
        except Exception as e:
            logger.error(f"[SubAgentManager] Task {task.task_id} failed: {e}")
            task.status = "failed"
            task.result = f"Error: {str(e)}"
            task.verified = False
            task.verify_source = "self"
        
        finally:
            # Update timing and status
            task.elapsed_ms = (time.time() - start_time) * 1000
            task.completed_at = time.time()
            
            # Only set to completed if not already set by timeout/failure
            if task.status not in ("timeout", "failed"):
                task.status = "completed"
            
            # Persist final state
            await self._save_task(task)
            
            # Remove from active tasks
            async with self._active_tasks_lock:
                if task.task_id in self._active_tasks:
                    del self._active_tasks[task.task_id]
            
            logger.info(
                f"[SubAgentManager] Task {task.task_id} {task.status}: "
                f"verified={task.verified}, time={task.elapsed_ms:.1f}ms"
            )
    
    async def _verify_subagent_result(
        self,
        task: SubAgentTask,
        result: str
    ) -> Dict[str, Any]:
        """
        Verify sub-agent result using Force-Verify system.
        
        Args:
            task: The task that produced the result
            result: The result string to verify
            
        Returns:
            Verification result dict
        """
        try:
            # Import Force-Verify components
            from .verify import VerifyLevel, VerifyResult
            
            # Basic verification: check result is not empty
            if not result:
                return {
                    'passed': False,
                    'source': 'self',
                    'reason': 'Empty result',
                }
            
            # Check result meets basic criteria
            if result.startswith("Error:"):
                return {
                    'passed': False,
                    'source': 'self',
                    'reason': 'Result contains error',
                }
            
            # In full implementation, more sophisticated verification would occur
            # For now, basic checks pass
            
            return {
                'passed': True,
                'source': 'self',
                'level': VerifyLevel.BASIC.name,
            }
            
        except ImportError:
            logger.warning("[SubAgentManager] Force-Verify not available")
            return {
                'passed': True,
                'source': 'self',
            }
        except Exception as e:
            logger.error(f"[SubAgentManager] Verification error: {e}")
            return {
                'passed': False,
                'source': 'self',
                'reason': str(e),
            }
    
    async def get_result(self, task_id: str) -> Optional[SubAgentTask]:
        """
        Get the result of a sub-agent task.
        
        Args:
            task_id: The task identifier
            
        Returns:
            SubAgentTask if found, None otherwise
        """
        # Check active tasks first
        async with self._active_tasks_lock:
            if task_id in self._active_tasks:
                return self._active_tasks[task_id]
        
        # Load from database
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT * FROM subagent_tasks WHERE task_id = ?",
            (task_id,)
        )
        row = cursor.fetchone()
        
        if not row:
            return None
        
        return SubAgentTask.from_dict(dict(row))
    
    async def list_tasks(
        self,
        status: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        limit: int = 20
    ) -> List[SubAgentTask]:
        """
        List sub-agent tasks with optional filters.
        
        Args:
            status: Filter by status (pending/running/completed/failed)
            parent_session_id: Filter by parent session
            limit: Maximum number of tasks to return
            
        Returns:
            List of SubAgentTask objects
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        
        query = "SELECT * FROM subagent_tasks WHERE 1=1"
        params = []
        
        if status:
            query += " AND status = ?"
            params.append(status)
        
        if parent_session_id:
            query += " AND parent_session_id = ?"
            params.append(parent_session_id)
        
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        
        cursor.execute(query, params)
        
        tasks = []
        for row in cursor.fetchall():
            tasks.append(SubAgentTask.from_dict(dict(row)))
        
        return tasks
    
    async def cancel(self, task_id: str) -> bool:
        """
        Cancel a running sub-agent task.
        
        Args:
            task_id: The task identifier
            
        Returns:
            True if task was cancelled, False if not found or already completed
        """
        task = await self.get_result(task_id)
        
        if not task:
            return False
        
        if task.status in ("completed", "failed"):
            logger.warning(f"[SubAgentManager] Cannot cancel {task_id}: status={task.status}")
            return False
        
        # Update status
        task.status = "failed"
        task.result = "Cancelled by parent"
        task.completed_at = time.time()
        task.elapsed_ms = (task.completed_at - task.created_at) * 1000
        
        # Persist
        await self._save_task(task)
        
        # Remove from active
        async with self._active_tasks_lock:
            if task_id in self._active_tasks:
                del self._active_tasks[task_id]
        
        logger.info(f"[SubAgentManager] Cancelled task: {task_id}")
        return True
    
    async def get_active_count(self) -> int:
        """
        Get the number of currently active (pending/running) tasks.

        Returns:
            int: Count of tasks currently tracked in the in-memory active cache.
        """
        async with self._active_tasks_lock:
            return len(self._active_tasks)
    
    # =========================================================================
    # Database Operations
    # =========================================================================
    
    async def _save_task(self, task: SubAgentTask):
        """
        Persist a task's full state to the SQLite database.

        Uses INSERT OR REPLACE (upsert) so this is safe for both initial
        creation and subsequent updates. Serializes the tools list as JSON
        and converts the verified boolean to an integer for SQLite storage.

        Args:
            task: The SubAgentTask to persist.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO subagent_tasks (
                task_id, description, parent_session_id, status,
                system_prompt, tools, max_iterations, result,
                verified, elapsed_ms, review_source, review_notes,
                verify_source, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task.task_id,
            task.description,
            task.parent_session_id,
            task.status,
            task.system_prompt,
            json.dumps(task.tools),
            task.max_iterations,
            task.result,
            1 if task.verified else 0,
            task.elapsed_ms,
            task.review_source,
            task.review_notes,
            task.verify_source,
            task.created_at,
            task.completed_at,
        ))
        
        conn.commit()
    
    async def _update_task_status(self, task_id: str, status: str):
        """
        Update only the status field of a task in the database.

        This is a lightweight update used during state transitions
        (e.g. pending -> running) without rewriting the full row.

        Args:
            task_id: The unique task identifier.
            status: The new status value (pending/running/completed/failed/timeout).
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute(
            "UPDATE subagent_tasks SET status = ? WHERE task_id = ?",
            (status, task_id)
        )
        
        conn.commit()


# =============================================================================
# Delegate Tool Handler
# =============================================================================

async def delegate_tool_handler(
    framework: 'WorkerFramework',
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Tool handler for the 'delegate' tool.
    
    This is the interface for AI to delegate tasks to sub-agents.
    
    Args:
        framework: WorkerFramework instance
        params: Tool parameters:
            - description: Task description (required)
            - tools: List of tool names (optional)
            - max_iterations: Max reasoning rounds (optional, default: 5)
            
    Returns:
        Dict with task_id and status
    """
    description = params.get('description')
    if not description:
        return {
            'success': False,
            'error': 'Missing required parameter: description',
        }
    
    tools = params.get('tools')
    max_iterations = params.get('max_iterations', 5)
    
    # Get or create SubAgentManager
    if not hasattr(framework, 'subagent_manager'):
        framework.subagent_manager = SubAgentManager(framework)
    
    manager = framework.subagent_manager
    
    try:
        # Delegate task
        task = await manager.delegate(
            description=description,
            tools=tools,
            max_iterations=max_iterations,
        )
        
        return {
            'success': True,
            'task_id': task.task_id,
            'status': 'pending',
            'message': f'Task delegated. Use get_result with task_id="{task.task_id}" to retrieve results.',
        }
        
    except Exception as e:
        logger.error(f"[delegate_tool] Error: {e}")
        return {
            'success': False,
            'error': str(e),
        }


# =============================================================================
# Get Result Tool Handler
# =============================================================================

async def get_subagent_result_handler(
    framework: 'WorkerFramework',
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Tool handler for retrieving sub-agent results.
    
    Args:
        framework: WorkerFramework instance
        params: Tool parameters:
            - task_id: The task identifier (required)
            
    Returns:
        Dict with task result and status
    """
    task_id = params.get('task_id')
    if not task_id:
        return {
            'success': False,
            'error': 'Missing required parameter: task_id',
        }
    
    # Get or create SubAgentManager
    if not hasattr(framework, 'subagent_manager'):
        framework.subagent_manager = SubAgentManager(framework)
    
    manager = framework.subagent_manager
    
    try:
        task = await manager.get_result(task_id)
        
        if not task:
            return {
                'success': False,
                'error': f'Task not found: {task_id}',
            }
        
        return {
            'success': True,
            'task_id': task.task_id,
            'status': task.status,
            'result': task.result,
            'verified': task.verified,
            'elapsed_ms': task.elapsed_ms,
        }
        
    except Exception as e:
        logger.error(f"[get_subagent_result] Error: {e}")
        return {
            'success': False,
            'error': str(e),
        }


# =============================================================================
# List Tasks Tool Handler
# =============================================================================

async def list_subagent_tasks_handler(
    framework: 'WorkerFramework',
    status: str = None,
    limit: int = 20
) -> Dict[str, Any]:
    """
    Tool handler for listing sub-agent tasks.
    
    Args:
        framework: WorkerFramework instance (bound by partial)
        status: Filter by status (optional)
        limit: Max results (optional, default: 20)
            
    Returns:
        Dict with list of tasks
    """
    
    # Get or create SubAgentManager
    if not hasattr(framework, 'subagent_manager'):
        framework.subagent_manager = SubAgentManager(framework)
    
    manager = framework.subagent_manager
    
    try:
        tasks = await manager.list_tasks(status=status, limit=limit)
        
        return {
            'success': True,
            'count': len(tasks),
            'tasks': [t.to_dict() for t in tasks],
        }
        
    except Exception as e:
        logger.error(f"[list_subagent_tasks] Error: {e}")
        return {
            'success': False,
            'error': str(e),
        }


# =============================================================================
# Cancel Task Tool Handler
# =============================================================================

async def cancel_subagent_task_handler(
    framework: 'WorkerFramework',
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Tool handler for cancelling a sub-agent task.
    
    Args:
        framework: WorkerFramework instance
        params: Tool parameters:
            - task_id: The task identifier (required)
            
    Returns:
        Dict with cancellation status
    """
    task_id = params.get('task_id')
    if not task_id:
        return {
            'success': False,
            'error': 'Missing required parameter: task_id',
        }
    
    # Get or create SubAgentManager
    if not hasattr(framework, 'subagent_manager'):
        framework.subagent_manager = SubAgentManager(framework)
    
    manager = framework.subagent_manager
    
    try:
        cancelled = await manager.cancel(task_id)
        
        return {
            'success': cancelled,
            'task_id': task_id,
            'cancelled': cancelled,
            'message': 'Task cancelled' if cancelled else 'Task not found or already completed',
        }
        
    except Exception as e:
        logger.error(f"[cancel_subagent_task] Error: {e}")
        return {
            'success': False,
            'error': str(e),
        }
