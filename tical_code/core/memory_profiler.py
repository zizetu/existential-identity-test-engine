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

Memory Profiler - EITE Evaluation Resource Tracking
=====================================================

Tracks memory and resource usage during evaluation sessions. Continuously
samples Python memory allocations across evaluation steps and identifies
objects that grow without bound, enabling resource leak detection in
long-running evaluation sessions.

Usage:
    profiler = MemoryProfiler(eval_session_name="eval_v1", sample_interval_steps=25)
    profiler.start()
    # ... run evaluation steps ...
    profiler.sample(step=47)
    report = profiler.compare(step_1_snapshot, step_N_snapshot)
    profiler.log_report(report)

Author: Tical (Zize Tu)
"""

import gc
import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SNAPSHOT_DIR = "~/.EITElite/memory-snapshots"


# ---------------------------------------------------------------------------
# Memory Profiler
# ---------------------------------------------------------------------------

class MemoryProfiler:
    """Tracemalloc-based memory profiler for EITE evaluation resource tracking.

    Enabled only when MEMORY_PROFILE=1 env var is set.
    Production evaluation runs skip profiling overhead entirely.
    """

    def __init__(
        self,
        worker_name: str = "unknown",
        sample_interval_steps: int = 25,
        snapshot_dir: str = "",
        top_n: int = 20,
    ):
        self.worker_name = worker_name
        self.sample_interval = sample_interval_steps
        self.top_n = top_n
        self.enabled = os.environ.get("MEMORY_PROFILE", "") == "1"
        self._snapshot_dir = Path(
            os.path.expanduser(snapshot_dir or DEFAULT_SNAPSHOT_DIR)
        )

        self._tracemalloc = None
        self._snapshots: List[Dict] = []
        self._start_rss = 0
        self._started = False

        if self.enabled:
            try:
                import tracemalloc
                self._tracemalloc = tracemalloc
            except ImportError:
                logger.warning("[EITE memprof] tracemalloc not available - profiling disabled")
                self.enabled = False

    def start(self) -> None:
        """Start memory profiling. No-op if disabled."""
        if not self.enabled or not self._tracemalloc:
            return

        self._tracemalloc.start(25)
        self._start_rss = self._get_rss_mb()
        self._started = True
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[EITE memprof] Memory profiling started (RSS baseline: %.0f MB)", self._start_rss)

    def sample(self, step: int) -> Optional[Dict]:
        """Take a memory snapshot at the given evaluation step.

        Returns snapshot dict or None if profiling disabled.
        """
        if not self.enabled or not self._tracemalloc:
            return None

        snapshot = self._tracemalloc.take_snapshot()
        top_stats = snapshot.statistics("lineno")

        rss = self._get_rss_mb()
        record = {
            "step": step,
            "timestamp": time.time(),
            "rss_mb": rss,
            "rss_delta_mb": rss - self._start_rss,
            "top_allocations": [
                {
                    "file": str(stat.traceback),
                    "size_mb": round(stat.size / (1024 * 1024), 3),
                    "count": stat.count,
                }
                for stat in top_stats[:self.top_n]
            ],
            "gc_stats": self._get_gc_stats(),
        }

        self._snapshots.append(record)
        self._save_snapshot(record)

        if len(self._snapshots) >= 2:
            prev = self._snapshots[-2]
            rss_growth = rss - prev["rss_mb"]
            if rss_growth > 10:
                logger.warning(
                    "[EITE memprof] Step %d: RSS %.0f MB (+%.0f MB since step %d) - "
                    "top allocations: %s",
                    step, rss, rss_growth, prev["step"],
                    [a["file"].split("/")[-1] for a in top_stats[:5]]
                )

        return record

    def compare(
        self,
        step_a: int,
        step_b: int,
    ) -> Dict:
        """Compare two snapshots and identify growing allocations.

        Returns a report with objects that grew the most.
        """
        if not self.enabled or not self._tracemalloc:
            return {"error": "profiling disabled"}

        snap_a = None
        snap_b = None
        for s in self._snapshots:
            if s["step"] == step_a:
                snap_a = s
            if s["step"] == step_b:
                snap_b = s

        if not snap_a or not snap_b:
            return {"error": f"snapshots not found: a={step_a} b={step_b}"}

        rss_growth = snap_b["rss_mb"] - snap_a["rss_mb"]

        a_files = {a["file"].split("/")[-1] for a in snap_a["top_allocations"]}
        b_files = {a["file"].split("/")[-1] for a in snap_b["top_allocations"]}
        new_allocs = b_files - a_files

        return {
            "step_a": step_a,
            "step_b": step_b,
            "rss_a": snap_a["rss_mb"],
            "rss_b": snap_b["rss_mb"],
            "rss_growth_mb": rss_growth,
            "steps_elapsed": step_b - step_a,
            "rss_per_step_mb": round(rss_growth / max(1, step_b - step_a), 3),
            "new_allocation_sources": list(new_allocs),
            "suspect_leak": rss_growth / max(1, step_b - step_a) > 1.0,
        }

    def finalize(self) -> Dict:
        """Generate final memory report for the evaluation session."""
        if not self.enabled or not self._tracemalloc:
            return {"enabled": False}

        report = {
            "worker_name": self.worker_name,
            "start_rss_mb": self._start_rss,
            "end_rss_mb": self._get_rss_mb(),
            "samples_taken": len(self._snapshots),
            "total_rss_growth_mb": self._get_rss_mb() - self._start_rss,
        }

        if len(self._snapshots) >= 2:
            first = self._snapshots[0]
            last = self._snapshots[-1]
            report["rss_per_step_mb"] = round(
                (last["rss_mb"] - first["rss_mb"])
                / max(1, last["step"] - first["step"]),
                3
            )

        report_path = self._snapshot_dir / f"{self.worker_name}-final-report.json"
        tmp_path = self._snapshot_dir / f"{self.worker_name}-final-report.json.tmp"
        try:
            tmp_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
            os.rename(str(tmp_path), str(report_path))
        except OSError:
            pass

        logger.info("[EITE memprof] Final report: RSS %.0f->%.0f MB, %.3f MB/step",
                     report["start_rss_mb"], report["end_rss_mb"],
                     report.get("rss_per_step_mb", 0))

        return report

    def _save_snapshot(self, record: Dict) -> None:
        """Persist snapshot to disk for later analysis."""
        step = record["step"]
        path = self._snapshot_dir / f"{self.worker_name}-step-{step:04d}.json"
        tmp = self._snapshot_dir / f"{self.worker_name}-step-{step:04d}.json.tmp"
        try:
            tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False))
            os.rename(str(tmp), str(path))
        except OSError as e:
            logger.debug("[EITE memprof] Failed to save snapshot: %s", e)

    @staticmethod
    def _get_rss_mb() -> float:
        """Get current process RSS in MB."""
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024
        except Exception:
            pass
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
            return 0.0

    @staticmethod
    def _get_gc_stats() -> Dict:
        """Get garbage collector statistics."""
        counts = gc.get_count()
        return {
            "gen0": counts[0],
            "gen1": counts[1],
            "gen2": counts[2],
            "thresholds": list(gc.get_threshold()),
        }


# ---------------------------------------------------------------------------
# Resource safety utilities (always active, not just profiling)
# ---------------------------------------------------------------------------

def force_gc_collect(aggressive: bool = False) -> Dict[str, int]:
    """Force garbage collection and return stats.

    Args:
        aggressive: If True, also run gc.collect(2) for full generational sweep.

    Returns:
        Dict with collected counts.
    """
    before = gc.get_count()
    if aggressive:
        gc.collect(2)
    else:
        gc.collect()
    after = gc.get_count()
    return {
        "gen0_before": before[0],
        "gen1_before": before[1],
        "gen2_before": before[2],
        "gen0_after": after[0],
        "gen1_after": after[1],
        "gen2_after": after[2],
    }


def cleanup_large_objects(*objs) -> int:
    """Delete large objects and force GC. Returns bytes freed (estimated).

    Usage:
        cleanup_large_objects(conv, old_results, temp_data)
    """
    total_size = 0
    for obj in objs:
        try:
            total_size += obj.__sizeof__()
        except Exception:
            pass
        del obj
    force_gc_collect(aggressive=True)
    return total_size
