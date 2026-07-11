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
Lightweight delegate_task implementation for eite-agent (1C1G profile).

Uses subprocess to execute tasks independently without the full
SubAgentManager infrastructure from EITElite. Suitable for Oracle/Test
lightweight VPS nodes.

Provides:
  - delegate_task_handler(args)        → spawn background task, return task_id
  - get_subagent_result_handler(args)  → poll for task result
"""

import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("EITElite.delegate-light")

_tasks: Dict[str, Dict] = {}
_RESULT_DIR = Path(os.environ.get("TICAL_DELEGATE_DIR", "/tmp/tical-delegates"))


def delegate_task_handler(args: dict) -> dict:
    """Spawn a background task via subprocess, return a task_id for polling.

    Args:
        args: Dict with 'goal' (str, required), 'context' (str, optional),
              'timeout' (int, optional, default=120),
              'max_iterations' (int, optional, default=10).

    Returns:
        Dict with task_id, status.
    """
    goal = args.get("goal", "").strip()
    context = args.get("context", "")
    timeout = int(args.get("timeout", 120))
    max_iterations = int(args.get("max_iterations", 10))

    if not goal:
        return {"error": "delegate_task requires 'goal' parameter"}

    # Ensure result dir exists
    _RESULT_DIR.mkdir(parents=True, exist_ok=True)

    task_id = uuid.uuid4().hex
    task_file = _RESULT_DIR / f"{task_id}.json"

    # Write task params as JSON (safe from injection)
    task_params = json.dumps({
        "task_file": str(task_file),
        "goal": goal,
        "context": context,
        "max_iterations": max_iterations,
        "timeout": timeout,
        "worker_endpoint": os.environ.get("TICAL_CHAT_URL", "http://localhost:8080"),
    })

    # AG-H3: whitelist env vars instead of blacklist (prevent credential leak)
    _SAFE_ENV_KEYS = {"PATH", "HOME", "LANG", "VIRTUAL_ENV", "NODE_PATH", "TICAL_CHAT_URL"}
    safe_env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    safe_env["DELEGATE_PARAMS"] = task_params

    payload = {
        "task_id": task_id,
        "status": "pending",
        "created_at": time.time(),
    }
    task_file.write_text(json.dumps(payload))

    # SECURITY: pass data via env var (json.dumps), NOT string interpolation into -c
    script = r"""import json, os, urllib.request, time
params = json.loads(os.environ.get('DELEGATE_PARAMS', '{}'))
task_file = params['task_file']
goal = params['goal']
context = params.get('context', '')
max_iterations = params.get('max_iterations', 10)
timeout = params.get('timeout', 120)
worker_endpoint = params.get('worker_endpoint', 'http://localhost:8080')
try:
    with open(task_file) as f:
        data = json.load(f)
    data['status'] = 'running'
    data['started_at'] = time.time()
    with open(task_file, 'w') as f:
        json.dump(data, f)
    body = json.dumps({
        'goal': goal,
        'context': context,
        'max_iterations': max_iterations,
    }).encode()
    req = urllib.request.Request(
        worker_endpoint + '/v1/delegate',
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
        data['status'] = 'completed'
        data['result'] = result
        data['completed_at'] = time.time()
    except Exception as e:
        data['status'] = 'failed'
        data['error'] = str(e)[:500]
    with open(task_file, 'w') as f:
        json.dump(data, f)
except Exception as e:
    with open(task_file, 'w') as f:
        json.dump({'task_id': task_id, 'status': 'failed', 'error': str(e)[:500]}, f)
"""

    # AG-H3: Use pre-built safe_env from earlier (whitelist mode)
    safe_env["DELEGATE_PARAMS"] = task_params
    try:
        subprocess.Popen(
            ["python3", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=safe_env,
        )
    except Exception as e:
        return {"error": f"Failed to spawn delegate: {e}"}

    _tasks[task_id] = {"status": "pending", "created_at": time.time()}
    logger.info("Delegate task %s spawned: %s", task_id, goal[:80])
    return {"task_id": task_id, "status": "pending"}


def get_subagent_result_handler(args: dict) -> dict:
    """Poll for the result of a previously spawned delegate task.

    Args:
        args: Dict with 'task_id' (str, required).

    Returns:
        Dict with task_id, status, result (if completed), or error.
    """
    task_id = args.get("task_id", "")
    if not task_id:
        return {"error": "get_subagent_result requires 'task_id' parameter"}

    if task_id in _tasks:
        return _tasks[task_id]

    task_file = _RESULT_DIR / f"{task_id}.json"
    if task_file.exists():
        try:
            data = json.loads(task_file.read_text())
            _tasks[task_id] = data
            return data
        except (json.JSONDecodeError, OSError) as e:
            return {"task_id": task_id, "status": "error", "error": str(e)}

    return {"task_id": task_id, "status": "unknown", "error": "Task ID not found"}
