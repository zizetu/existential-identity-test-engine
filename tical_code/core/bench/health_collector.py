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

"""HealthCollector - collect system and module health metrics for bench upload.

Reads /proc for CPU/memory/disk stats (no psutil dependency).
Collects module health state from EITE's module system.
Returns a flat dict ready for JSON serialisation.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("eite.bench.health")


def _read_proc(path: str) -> Optional[str]:
    try:
        return Path(path).read_text().strip()
    except (OSError, PermissionError):
        return None


def _cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        try:
            return os.cpu_count() or 1
        except Exception:
            return 1


def _memory_mb() -> Dict[str, float]:
    """Read total/available/used memory from /proc/meminfo (MB)."""
    mem = _read_proc("/proc/meminfo")
    if not mem:
        return {"total_mb": 0, "available_mb": 0, "used_mb": 0}
    total = 0
    avail = 0
    for line in mem.splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1]) / 1024
        elif line.startswith("MemAvailable:"):
            avail = int(line.split()[1]) / 1024
    return {"total_mb": round(total, 1), "available_mb": round(avail, 1), "used_mb": round(total - avail, 1)}


def _disk_gb(path: str = "/") -> Dict[str, float]:
    """Read disk usage from statvfs."""
    try:
        s = os.statvfs(path)
        total = s.f_frsize * s.f_blocks / (1024 ** 3)
        free = s.f_frsize * s.f_bfree / (1024 ** 3)
        return {"total_gb": round(total, 1), "free_gb": round(free, 1), "used_gb": round(total - free, 1)}
    except Exception:
        return {"total_gb": 0, "free_gb": 0, "used_gb": 0}


def _load_avg() -> Dict[str, float]:
    """Read 1/5/15 min load averages from /proc/loadavg."""
    raw = _read_proc("/proc/loadavg")
    if not raw:
        return {"1min": 0, "5min": 0, "15min": 0}
    parts = raw.split()
    return {"1min": float(parts[0]), "5min": float(parts[1]), "15min": float(parts[2])}


def _uptime() -> float:
    raw = _read_proc("/uptime")
    if raw:
        try:
            return float(raw.split()[0])
        except (IndexError, ValueError):
            pass
    return 0.0


class HealthCollector:
    """Collect system and EITE module health metrics."""

    def __init__(self, worker_name: str = "unknown"):
        self.worker_name = worker_name
        self._started = time.time()

    def collect(self, module_health: Optional[Dict] = None) -> Dict:
        """Return a snapshot of current health state.

        Args:
            module_health: Optional dict of {module_name: {"state": str, …}}
                           from self_repair / module_registry.
        """
        mem = _memory_mb()
        disk = _disk_gb()
        load = _load_avg()
        cpu = _cpu_count()

        payload = {
            "worker": self.worker_name,
            "timestamp": time.time(),
            "uptime_seconds": round(time.time() - self._started, 1),
            "system": {
                "cpu_count": cpu,
                "load": load,
                "memory": mem,
                "disk": disk,
                "hostname": os.uname().nodename,
            },
        }

        if module_health:
            payload["modules"] = module_health

        return payload
