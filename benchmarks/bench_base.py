"""Base benchmark adapter - common interface for all leaderboard adapters"""

import json
import os
import time
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("EITElite.benchmark")


@dataclass
class BenchResult:
    """Result of a single test case"""
    task_id: str
    passed: bool
    score: float = 0.0          # 0.0-1.0
    latency_ms: float = 0.0
    error: Optional[str] = None
    detail: Optional[Dict] = None
    retry_stats: Optional[Dict] = None
    error_breakdown: Optional[Dict] = None


@dataclass
class BenchReport:
    """Report for the entire benchmark run"""
    benchmark_name: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    accuracy: float = 0.0
    avg_latency_ms: float = 0.0
    results: List[BenchResult] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Aggregated retry metrics
    error_breakdown: Dict[str, int] = field(default_factory=lambda: {
        "wrong_func_name": 0, "wrong_param_type": 0, "missing_param": 0,
        "wrong_enum_value": 0, "extra_call": 0, "wrong_format": 0, "suspicious_semantics": 0,
    })
    retry_stats: Dict[str, Any] = field(default_factory=lambda: {
        "total_retries": 0,
        "retry_success_count": 0,
        "retry_still_failed_count": 0,
        "total_repair_attempts": 0,
        "repair_efficiency": 0.0,
    })

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)


class BenchAdapter(ABC):
    """Benchmark adapter base class

    Subclasses must implement:
    - load_tasks(): Load test data
    - run_single(): Execute a single test case
    - evaluate(): Validate results
    """

    def __init__(self, data_dir: str = "", output_dir: str = ""):
        self.data_dir = data_dir
        self.output_dir = output_dir or os.path.join(data_dir, "results")
        os.makedirs(self.output_dir, exist_ok=True)
        self._tasks: List[Dict] = []

    @property
    @abstractmethod
    def name(self) -> str:
        """Benchmark name"""
        ...

    @abstractmethod
    def load_tasks(self, split: str = "test") -> List[Dict]:
        """Load test data, return standardized task list"""
        ...

    @abstractmethod
    def run_single(self, task: Dict, agent_fn) -> BenchResult:
        """Execute a single test case

        Args:
            task: Standardized test case
            agent_fn: callable, receives (messages, tools) returns agent response
        """
        ...

    def evaluate(self, task: Dict, response: Any) -> Tuple[bool, float]:
        """Validate if agent response is correct, return (passed, score)"""
        return False, 0.0

    def run(self, agent_fn, split: str = "test",
            max_tasks: int = 0) -> BenchReport:
        """Run the entire benchmark

        Args:
            agent_fn: callable, receives (messages, tools) returns agent response
            split: Dataset split
            max_tasks: Limit number of tasks to run, 0=all
        """
        self._tasks = self.load_tasks(split)
        tasks = self._tasks[:max_tasks] if max_tasks > 0 else self._tasks

        report = BenchReport(benchmark_name=self.name, total=len(tasks))
        latencies = []

        for i, task in enumerate(tasks):
            task_id = task.get("id", str(i))
            try:
                t0 = time.time()
                result = self.run_single(task, agent_fn)
                result.latency_ms = (time.time() - t0) * 1000
            except Exception as e:
                result = BenchResult(
                    task_id=task_id, passed=False, score=0.0,
                    error=str(e)
                )

            report.results.append(result)
            latencies.append(result.latency_ms)

            if result.error and "skip" in result.error.lower():
                report.skipped += 1
            elif result.passed:
                report.passed += 1
            else:
                report.failed += 1

            # Aggregate retry metrics
            if result.retry_stats:
                stats = result.retry_stats
                report.retry_stats["total_retries"] += stats.get("total_retries", 0)
                if stats.get("retry_triggered"):
                    report.retry_stats["total_repair_attempts"] += 1
                    if stats.get("repair_success_count", 0) > 0:
                        report.retry_stats["retry_success_count"] += 1
                    else:
                        report.retry_stats["retry_still_failed_count"] += 1

            if result.error_breakdown:
                for k, v in result.error_breakdown.items():
                    if k in report.error_breakdown:
                        report.error_breakdown[k] += v

            if (i + 1) % 10 == 0:
                logger.info(
                    f"[{self.name}] {i+1}/{len(tasks)} "
                    f"passed={report.passed} failed={report.failed}"
                )

        if report.total > report.skipped:
            report.accuracy = report.passed / (report.total - report.skipped)
        if latencies:
            report.avg_latency_ms = sum(latencies) / len(latencies)

        # Calculate repair_efficiency
        total_attempted = report.retry_stats.get("total_repair_attempts", 0)
        if total_attempted > 0:
            success = report.retry_stats.get("retry_success_count", 0)
            report.retry_stats["repair_efficiency"] = round(success / total_attempted, 4)

        # Save report
        report_path = os.path.join(
            self.output_dir, f"{self.name}_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"[{self.name}] Report saved to {report_path}")
        logger.info(
            f"[{self.name}] accuracy={report.accuracy:.4f} "
            f"passed={report.passed}/{report.total - report.skipped}"
        )

        return report
