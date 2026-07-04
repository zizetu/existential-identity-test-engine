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

"""BenchReporter - heartbeat upload and self-check result push to bench server.

Connects to the bench URL (configurable via BENCH_URL env var) for:
- Heartbeat upload (system resources, module health, provider status)
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Dict, Optional

logger = logging.getLogger("eite.bench.reporter")

_BASE_URL = os.environ.get("BENCH_URL", "http://localhost:9877")
_API_KEY = os.environ.get("BENCH_API_KEY", "")
_HEARTBEAT_PATH = "/api/v1/heartbeat"
_SELFCHECK_PATH = "/api/v1/self-check"
_COMMANDS_PATH = "/api/v1/commands"

_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0


def _post(path: str, payload: Dict) -> Optional[Dict]:
    """POST JSON payload to bench endpoint with retry."""
    if not _API_KEY:
        logger.warning("BENCH_API_KEY not set - bench reporting disabled")
        return None

    url = _BASE_URL.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_API_KEY}",
        },
        method="POST",
    )

    last_err = None
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode()
                if resp.status == 200:
                    return json.loads(body) if body else {}
                logger.warning("[bench] %s returned %d: %s", path, resp.status, body[:200])
                return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < _MAX_RETRIES - 1:
                sleep = _BACKOFF_BASE * (2 ** attempt)
                logger.debug("[bench] retry %d/%d after %.1fs: %s", attempt + 1, _MAX_RETRIES, sleep, e)
                time.sleep(sleep)

    logger.warning("[bench] %s failed after %d retries: %s", path, _MAX_RETRIES, last_err)
    return None


class BenchReporter:
    """Upload health data and self-check results to bench.ticalasi.com."""

    def __init__(self, worker_name: str = "unknown"):
        self.worker_name = worker_name

    def send_heartbeat(self, health_payload: Dict) -> Optional[Dict]:
        """Upload a heartbeat snapshot. Called periodically (e.g. every 60s)."""
        health_payload["worker"] = self.worker_name
        return _post(_HEARTBEAT_PATH, health_payload)

    def send_selfcheck(self, check_results: Dict) -> Optional[Dict]:
        """Push the result of a full self-check (module tests, provider checks)."""
        payload = {
            "worker": self.worker_name,
            "timestamp": time.time(),
            "results": check_results,
        }
        return _post(_SELFCHECK_PATH, payload)

    @staticmethod
    def poll_commands() -> Optional[Dict]:
        """Fetch pending commands from bench (e.g. repair, config update)."""
        result = _post(_COMMANDS_PATH, {})
        if result and isinstance(result, dict):
            return result.get("command")
        return None
