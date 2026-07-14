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

"""BenchListener - poll bench server for remote commands and execute them.

Command types:
  - "repair": run a named repair routine
  - "selfcheck": trigger a full self-check
  - "exec": run an arbitrary shell command (white-listed)
  - "config": update a config value

All exec commands pass through a white-list for safety.
"""

import json
import logging
import os
import shlex
import subprocess
import time
from typing import Any, Dict, Optional

from .reporter import BenchReporter

logger = logging.getLogger("eite.bench.listener")

# Only these commands are allowed when bench sends "exec"
_EXEC_WHITELIST = {
    "git pull": "git -C {repo} pull",
    "restart worker": "systemctl restart unified-worker-{name}",
    "check disk": "df -h /",
    "check memory": "free -m",
    "uptime": "uptime",
    "ping": "echo pong",
}


class BenchListener:
    """Poll bench for commands and execute them safely."""

    def __init__(self, worker_name: str = "unknown", repo_path: str = ""):
        self.worker_name = worker_name
        self.repo_path = repo_path or os.getcwd()
        self._reporter = BenchReporter(worker_name)

    def poll_and_execute(self) -> Optional[Dict[str, Any]]:
        """Poll bench for a command, execute it if found, return result."""
        cmd = BenchReporter.poll_commands()
        if not cmd:
            return None

        cmd_type = cmd.get("type", "")
        cmd_args = cmd.get("args", {})

        if cmd_type == "repair":
            return self._handle_repair(cmd_args)
        elif cmd_type == "selfcheck":
            return self._handle_selfcheck()
        elif cmd_type == "exec":
            return self._handle_exec(cmd_args)
        elif cmd_type == "config":
            return self._handle_config(cmd_args)
        else:
            logger.warning("[bench] unknown command type: %s", cmd_type)
            return {"status": "error", "detail": f"unknown command type: {cmd_type}"}

    def _handle_repair(self, args: Dict) -> Dict:
        """Run a named repair routine."""
        name = args.get("name", "")
        logger.info("[bench] executing repair: %s", name)
        # Placeholder - actual repair routines are registered via self_repair module
        return {"status": "pending", "repair": name, "detail": "routed to self_repair"}

    def _handle_selfcheck(self) -> Dict:
        """Collect health and push self-check result."""
        from .health_collector import HealthCollector
        collector = HealthCollector(self.worker_name)
        health = collector.collect()
        result = self._reporter.send_selfcheck(health)
        return {"status": "ok" if result else "error", "detail": health}

    def _handle_exec(self, args: Dict) -> Dict:
        """Execute a white-listed command."""
        raw = args.get("command", "").strip().lower()
        if raw not in _EXEC_WHITELIST:
            logger.warning("[bench] denied exec command: %s", raw)
            return {"status": "denied", "detail": f"command not in whitelist: {raw}"}

        template = _EXEC_WHITELIST[raw]
        filled = template.format(repo=self.repo_path, name=self.worker_name)
        try:
            result = subprocess.run(
                shlex.split(filled),
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {
                "status": "ok" if result.returncode == 0 else "error",
                "stdout": result.stdout[-1000:],
                "stderr": result.stderr[-500:],
                "returncode": result.returncode,
            }
        except (subprocess.TimeoutExpired, OSError) as e:
            return {"status": "error", "detail": str(e)}

    def _handle_config(self, args: Dict) -> Dict:
        """Apply a config update."""
        key = args.get("key", "")
        value = args.get("value", "")
        logger.info("[bench] config update: %s = %s", key, value)
        # Placeholder - actual config persistence to be wired in
        return {"status": "pending", "detail": f"config {key} queued for update"}
