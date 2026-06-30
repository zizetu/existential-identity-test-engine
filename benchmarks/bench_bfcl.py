"""BFCL v3 Adapter - Berkeley Function-Calling Leaderboard

Tests tool-calling accuracy:
- Single-tool: only 1 tool available
- Multi-tool: 5 tools available
- Multi-tool+: 20+ tools available

Data format: BFCL v3 JSON (function definitions + query + expected call)
GitHub: https://github.com/ShishirPatil/gorilla

Adaptation strategy:
- BFCL function definitions → directly feed into tical-code's tool-calling pipeline
- Validation: function name + parameter names+values exact match
"""

import json
import os
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from benchmarks.bench_base import BenchAdapter, BenchResult

logger = logging.getLogger("tical-code.benchmark.bfcl")


@dataclass
class BFCLBenchReport:
    """BFCL-specific report - includes error classification and retry statistics"""
    mode: str = "raw"
    total_tasks: int = 0
    passed_tasks: int = 0
    error_breakdown: dict = field(default_factory=lambda: {
        "wrong_func_name": 0, "wrong_param_type": 0, "missing_param": 0,
        "wrong_enum_value": 0, "extra_call": 0, "format_error": 0,
    })
    retry_stats: dict = field(default_factory=lambda: {
        "total_retries": 0,
        "retry_success_count": 0,
        "retry_still_failed_count": 0,
        "total_repair_attempts": 0,
        "repair_efficiency": 0.0,
    })


def merge_agent_stats(report: BFCLBenchReport, agent):
    """After each Task execution, aggregate agent metrics into report"""
    for k, v in agent.current_error_breakdown.items():
        report.error_breakdown[k] = v

    stats = agent.current_retry_stats
    report.retry_stats["total_retries"] += stats["total_retries"]

    if stats["retry_triggered"]:
        report.retry_stats["total_repair_attempts"] += 1
        if stats["repair_success_count"] > 0:
            report.retry_stats["retry_success_count"] += 1
        else:
            report.retry_stats["retry_still_failed_count"] += 1

    total_attempted = report.retry_stats["total_repair_attempts"]
    if total_attempted > 0:
        success = report.retry_stats["retry_success_count"]
        report.retry_stats["repair_efficiency"] = round(success / total_attempted, 4)


class BFCLAdapter(BenchAdapter):
    """BFCL v3 Adapter"""

    @property
    def name(self) -> str:
        return "BFCL_v4"

    def load_tasks(self, split: str = "test") -> List[Dict]:
        """Load BFCL test data

        Supports three data sources:
        1. BFCL v4 JSONL file (pip install bfcl-eval)
        2. BFCL v3 JSON file (download from gorilla repo)
        3. Custom simplified format

        BFCL v4 data is split into question files and answer files:
        - question file: BFCL_v4_exec_simple.json (id, question, function)
        - answer file: possible_answer_BFCL_v4_exec_simple.json (id, ground_truth)
        Load by id pairing.
        """
        tasks = []
        if not self.data_dir or not os.path.isdir(self.data_dir):
            logger.warning(f"[BFCL] data_dir not found: {self.data_dir}")
            return tasks

        loaded_ids = set()

        # 1. Load answer file (ground_truth) first, index by id
        answer_index = {}
        for fname in sorted(os.listdir(self.data_dir)):
            if not fname.endswith(".json"):
                continue
            if "possible_answer" not in fname and "ground_truth" not in fname:
                continue
            fpath = os.path.join(self.data_dir, fname)
            try:
                items = self._load_json_or_jsonl(fpath)
                for item in items:
                    item_id = item.get("id", "")
                    if item_id and "ground_truth" in item:
                        answer_index[item_id] = item["ground_truth"]
            except Exception as e:
                logger.warning(f"[BFCL] Failed to load answer file {fname}: {e}")

        # 2. Load question files, pair with answers
        for fname in sorted(os.listdir(self.data_dir)):
            if not fname.endswith(".json"):
                continue
            if "possible_answer" in fname:
                continue
            fpath = os.path.join(self.data_dir, fname)
            try:
                items = self._load_json_or_jsonl(fpath)
                for i, item in enumerate(items):
                    # If item has no ground_truth, supplement from answer_index
                    item_id = item.get("id", "")
                    if "ground_truth" not in item and item_id in answer_index:
                        item = dict(item)
                        item["ground_truth"] = answer_index[item_id]
                    task = self._normalize_bfcl_item(item, f"{fname}_{i}")
                    if task and task["id"] not in loaded_ids:
                        tasks.append(task)
                        loaded_ids.add(task["id"])
            except Exception as e:
                logger.warning(f"[BFCL] Failed to load {fname}: {e}")

        logger.info(f"[BFCL] Loaded {len(tasks)} tasks from {self.data_dir}")
        return tasks

    def _load_json_or_jsonl(self, fpath: str) -> List[Dict]:
        """Load JSON or JSONL file"""
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read().strip()

        # Try standard JSON first
        try:
            data = json.loads(content)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return [data]
        except json.JSONDecodeError:
            pass

        # JSONL format (one JSON per line)
        items = []
        for line in content.split("\n"):
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return items

    def _normalize_bfcl_item(self, item: Dict, task_id: str) -> Optional[Dict]:
        """Normalize BFCL test item

        BFCL v4 format:
        {
            "id": "exec_simple_0",
            "function": [{"name": "...", "description": "...", "parameters": {...}}],
            "question": [[{"role": "user", "content": "..."}]],  # nested list
            "ground_truth": [{"func_name": {"param": ["value1", "value2"]}}]
        }

        BFCL v3 format:
        {
            "function": [...],
            "question": [{"role": "user", "content": "..."}],
            "ground_truth": [{"name": "...", "arguments": {...}}]
        }
        """
        # Use item's own id
        if "id" in item:
            task_id = item["id"]

        functions = item.get("function", [])
        question = item.get("question", [])
        ground_truth = item.get("ground_truth", [])

        if not functions and not ground_truth:
            return None

        # Normalize to OpenAI tool schema format
        tools = []
        for func_def in functions:
            if isinstance(func_def, dict):
                if "type" in func_def and "function" in func_def:
                    tools.append(func_def)
                elif "name" in func_def:
                    # BFCL parameters may use "dict" instead of "object"
                    params = func_def.get("parameters", {})
                    if isinstance(params, dict):
                        params.setdefault("type", "object")
                        if params.get("type") == "dict":
                            params["type"] = "object"
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": func_def["name"],
                            "description": func_def.get("description", ""),
                            "parameters": params,
                        }
                    })

        # Normalize messages format - v4 question is [[{role,content}]]
        messages = []
        if isinstance(question, list):
            # v4: [[{role,content}]] → take first sublist
            flat_q = question
            if question and isinstance(question[0], list):
                flat_q = question[0]
            for q in flat_q:
                if isinstance(q, dict) and "role" in q:
                    messages.append(q)
                elif isinstance(q, str):
                    messages.append({"role": "user", "content": q})
        elif isinstance(question, str):
            messages = [{"role": "user", "content": question}]

        # When no question but there are functions and ground_truth, construct a generic prompt
        if not messages and functions:
            func_names = [f.get("name", "") for f in functions if isinstance(f, dict)]
            messages = [{"role": "user", "content": f"Use the provided function(s) to accomplish the task."}]

        # Normalize ground_truth
        expected_calls = []
        for gt in ground_truth:
            if isinstance(gt, str):
                # exec format: "func_name(key=value, key2=value2)"
                parsed = self._parse_exec_gt(gt)
                if parsed:
                    expected_calls.append(parsed)
                    continue
                try:
                    gt = json.loads(gt)
                except json.JSONDecodeError:
                    continue
            if isinstance(gt, dict):
                # v3 format first: {name: "...", arguments: {...}}
                if "name" in gt:
                    expected_calls.append({
                        "name": gt.get("name", ""),
                        "arguments": gt.get("arguments", gt.get("parameters", {}))
                    })
                else:
                    # v4 format: {func_name: {param: [possible_values]}}
                    for func_name, args_spec in gt.items():
                        if isinstance(args_spec, dict):
                            args = {}
                            for k, v in args_spec.items():
                                if isinstance(v, list) and len(v) > 0:
                                    args[k] = v[0]
                                else:
                                    args[k] = v
                            expected_calls.append({"name": func_name, "arguments": args})
                        else:
                            expected_calls.append({"name": func_name, "arguments": {}})

        return {
            "id": task_id,
            "tools": tools,
            "messages": messages,
            "expected_calls": expected_calls,
            "_raw": item,
        }

    def _parse_exec_gt(self, gt_str: str) -> Optional[Dict]:
        """Parse exec format ground truth: func_name(key=value, key2=value2)
        
        Supports nested list/dict values (bracket depth tracking),
        Fixes old regex truncation bug on list params (e.g. vectorA=[0.5, 0.7] was truncated to '[0.5')
        """
        import re
        m = re.match(r'^(\w+)\((.+)\)$', gt_str.strip())
        if not m:
            return None
        func_name = m.group(1)
        args_str = m.group(2)
        
        # Character-by-character scan, track bracket depth
        args = {}
        current_key = None
        current_val = ''
        depth = 0
        for c in args_str:
            if c in '[{(':
                depth += 1
                current_val += c
            elif c in ']})':
                depth -= 1
                current_val += c
            elif c == '=' and depth == 0 and current_key is None:
                current_key = current_val.strip()
                current_val = ''
            elif c == ',' and depth == 0:
                if current_key and current_val:
                    val_str = current_val.strip()
                    try:
                        val = json.loads(val_str)
                    except (json.JSONDecodeError, ValueError):
                        val = val_str
                    args[current_key] = val
                current_key = None
                current_val = ''
            else:
                current_val += c
        
        # Last pair
        if current_key and current_val:
            val_str = current_val.strip()
            try:
                val = json.loads(val_str)
            except (json.JSONDecodeError, ValueError):
                val = val_str
            args[current_key] = val
        
        return {"name": func_name, "arguments": args}

    def run_single(self, task: Dict, agent_fn: Callable) -> BenchResult:
        """Execute single BFCL test"""
        tools = task["tools"]
        messages = task["messages"]
        expected = task["expected_calls"]

        # Call agent
        response = agent_fn(messages=messages, tools=tools)

        # Extract tool calls
        actual_calls = self._extract_tool_calls(response)

        # Validate
        passed, score, detail = self._evaluate_calls(actual_calls, expected)

        return BenchResult(
            task_id=task["id"],
            passed=passed,
            score=score,
            detail=detail,
        )

    def _extract_tool_calls(self, response: Any) -> List[Dict]:
        """Extract tool calls from agent response"""
        calls = []

        if isinstance(response, dict):
            # OpenAI format
            msg = response.get("choices", [{}])[0].get("message", {})
            tool_calls = msg.get("tool_calls", [])
            # Ollama fallback: parse <tool_call> XML from content
            if not tool_calls and msg.get("content"):
                import re as _re
                tc_match = _re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", msg["content"], _re.DOTALL)
                if tc_match:
                    try:
                        tc_data = json.loads(tc_match.group(1))
                        name = tc_data.get("name", "")
                        args = tc_data.get("arguments", {})
                        if isinstance(args, str):
                            try: args = json.loads(args)
                            except Exception: pass
                        calls.append({"name": name, "arguments": args})
                    except Exception: pass
                    return calls
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                calls.append({"name": name, "arguments": args})

            # Direct tool call format
            if not calls and "function" in response:
                name = response["function"].get("name", "")
                try:
                    args = json.loads(
                        response["function"].get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                calls.append({"name": name, "arguments": args})

        elif isinstance(response, list):
            for r in response:
                calls.extend(self._extract_tool_calls(r))

        return calls

    def _evaluate_calls(
        self, actual: List[Dict], expected: List[Dict]
    ) -> Tuple[bool, float, Dict]:
        """Validate tool calls

        Scoring rules:
        - function name match: 50%
        - argument match: 50% (by matched parameter ratio)
        """
        if not expected:
            return len(actual) == 0, 1.0 if len(actual) == 0 else 0.0, {
                "reason": "no_expected_calls"}

        if len(actual) != len(expected):
            return False, 0.0, {
                "reason": "call_count_mismatch",
                "expected_count": len(expected),
                "actual_count": len(actual),
            }

        total_score = 0.0
        details = []

        for exp, act in zip(expected, actual):
            # function name match
            name_match = exp.get("name", "") == act.get("name", "")
            name_score = 0.5 if name_match else 0.0

            # argument match
            exp_args = exp.get("arguments", {})
            act_args = act.get("arguments", {})
            arg_score = self._compare_args(exp_args, act_args)

            call_score = name_score + arg_score * 0.5
            total_score += call_score

            details.append({
                "expected_name": exp.get("name"),
                "actual_name": act.get("name"),
                "name_match": name_match,
                "arg_score": round(arg_score, 3),
                "call_score": round(call_score, 3),
            })

        avg_score = total_score / len(expected)
        passed = avg_score >= 0.8  # BFCL pass threshold

        return passed, round(avg_score, 4), {"calls": details}

    @staticmethod
    def _compare_args(expected: Dict, actual: Dict) -> float:
        """Compare argument match degree"""
        if not expected and not actual:
            return 1.0
        if not expected:
            return 1.0  # No expected params, any output counts as correct
        if not actual:
            return 0.0

        matched = 0
        for key, exp_val in expected.items():
            if key not in actual:
                continue
            act_val = actual[key]
            # Value comparison (supports lenient type matching)
            if isinstance(exp_val, (int, float)):
                try:
                    matched += 1 if abs(float(act_val) - float(exp_val)) < 1e-6 else 0
                except (ValueError, TypeError):
                    matched += 1 if str(act_val) == str(exp_val) else 0
            elif isinstance(exp_val, bool):
                matched += 1 if str(act_val).lower() == str(exp_val).lower() else 0
            else:
                matched += 1 if str(act_val) == str(exp_val) else 0

        return matched / len(expected)

    def run_by_category(self, agent_fn: Callable) -> Dict[str, Any]:
        """Run by BFCL category: single/multi/multi_plus

        Returns:
            {"simple": BenchReport, "multi": BenchReport, ...}
        """
        # Auto-classify by tool count
        if not self._tasks:
            self._tasks = self.load_tasks()

        categories = {"simple": [], "multi": [], "multi_plus": []}
        for task in self._tasks:
            n_tools = len(task.get("tools", []))
            if n_tools <= 1:
                categories["simple"].append(task)
            elif n_tools <= 5:
                categories["multi"].append(task)
            else:
                categories["multi_plus"].append(task)

        results = {}
        for cat, tasks in categories.items():
            if not tasks:
                continue
            cat_tasks = tasks
            report = BenchReport(
                benchmark_name=f"{self.name}_{cat}",
                total=len(cat_tasks),
            )
            latencies = []
            import time
            for i, task in enumerate(cat_tasks):
                t0 = time.time()
                try:
                    result = self.run_single(task, agent_fn)
                    result.latency_ms = (time.time() - t0) * 1000
                except Exception as e:
                    result = BenchResult(
                        task_id=task.get("id", str(i)),
                        passed=False, error=str(e))
                report.results.append(result)
                latencies.append(result.latency_ms)
                if result.passed:
                    report.passed += 1
                else:
                    report.failed += 1

            report.accuracy = report.passed / max(1, report.total)
            report.avg_latency_ms = (
                sum(latencies) / len(latencies) if latencies else 0)
            results[cat] = report

            logger.info(
                f"[BFCL/{cat}] accuracy={report.accuracy:.4f} "
                f"passed={report.passed}/{report.total}")

        # Merge agent stats (schema mode error classification + retry stats)
        if hasattr(agent_fn, "global_error_breakdown") and getattr(agent_fn, "mode", "raw") != "raw":
            from benchmarks.bench_bfcl import BFCLBenchReport
            gb = agent_fn.global_error_breakdown
            gs = agent_fn.global_retry_stats
            total_attempted = gs["tasks_with_retry"]
            efficiency = round(gs["repair_success"] / total_attempted, 4) if total_attempted > 0 else 0.0
            bfcl_report = BFCLBenchReport(
                mode=getattr(agent_fn, "mode", "raw"),
                total_tasks=sum(r.total for r in results.values()),
                passed_tasks=sum(r.passed for r in results.values()),
                error_breakdown=dict(gb),
                retry_stats={
                    "total_retries": gs["total_retries"],
                    "retry_success_count": gs["repair_success"],
                    "retry_still_failed_count": gs["repair_failed"],
                    "total_repair_attempts": total_attempted,
                    "repair_efficiency": efficiency,
                },
            )
            logger.info("=== Schema Validation Report ===")
            logger.info("Error breakdown: %s", dict(bfcl_report.error_breakdown))
            logger.info("Retry stats: %s", dict(bfcl_report.retry_stats))

        return results
