"""GAIA Adapter - General Assistant Multi-Step Reasoning Test

Meta + HuggingFace release, 466 general assistant tasks:
- Level 1: Single-step tool call (search → number)
- Level 2: Multi-step reasoning + multi-tool (web + data extraction + calculation)
- Level 3: 20+ step long-chain reasoning

Human baseline 92%, current best Agent ~74%

EITElite capability mapping:
- web_sense: web search + scraping
- agent-browser: complex web browsing
- FileTool: file read/write
- TaskContinuity: multi-step task continuity
- signal_calibrator: reasoning calibration

Adaptation strategy:
- GAIA task → parse required tool types → step-by-step calls → result verification
- Level 1-2: tool-calling direct resolution
- Level 3: multi-step task + intermediate state saving

Data: https://huggingface.co/datasets/gaia-benchmark/GAIA
"""

import json
import math
import os
import re
import time
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from benchmarks.bench_base import BenchAdapter, BenchResult

logger = logging.getLogger("EITElite.benchmark.gaia")


class GAIAGrader:
    """GAIA answer validator

    GAIA answers are singular, clear, and verifiable:
    - Numbers: numeric match (1% tolerance)
    - Strings: exact match (ignore leading/trailing whitespace and case)
    - Lists: ordered/unordered match
    """

    @staticmethod
    def grade(prediction: str, ground_truth: str) -> Tuple[bool, float]:
        """Score, returns (exact_match, partial_score)"""
        pred = prediction.strip().lower()
        truth = ground_truth.strip().lower()

        if pred == truth:
            return True, 1.0

        # Number matching
        try:
            pred_num = float(pred.replace(",", ""))
            truth_num = float(truth.replace(",", ""))
            if abs(pred_num - truth_num) / max(abs(truth_num), 1e-9) < 0.01:
                return True, 1.0
            # Partial score
            relative_error = abs(pred_num - truth_num) / max(abs(truth_num), 1e-9)
            partial = max(0, 1.0 - relative_error)
            return False, round(partial, 4)
        except (ValueError, ZeroDivisionError):
            pass

        # List matching (comma-separated)
        if "," in truth:
            pred_items = set(x.strip() for x in pred.split(","))
            truth_items = set(x.strip() for x in truth.split(","))
            if pred_items == truth_items:
                return True, 1.0
            overlap = len(pred_items & truth_items)
            total = len(truth_items)
            if overlap > 0:
                return False, round(overlap / total, 4)

        # Substring matching
        if truth in pred or pred in truth:
            return False, 0.5

        return False, 0.0


class GAIAAdapter(BenchAdapter):
    """GAIA Adapter"""

    @property
    def name(self) -> str:
        return "GAIA"

    def load_tasks(self, split: str = "test") -> List[Dict]:
        tasks = []
        if self.data_dir and os.path.isdir(self.data_dir):
            for fname in sorted(os.listdir(self.data_dir)):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(self.data_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    items = data if isinstance(data, list) else [data]
                    for i, item in enumerate(items):
                        task = self._normalize(item, f"{fname}_{i}")
                        if task:
                            tasks.append(task)
                except Exception as e:
                    logger.warning(f"[GAIA] Failed to load {fname}: {e}")

        if not tasks:
            tasks = self._generate_mock_tasks()
            logger.info(f"[GAIA] Using {len(tasks)} mock tasks")

        logger.info(f"[GAIA] Loaded {len(tasks)} tasks")
        return tasks

    def _normalize(self, item: Dict, task_id: str) -> Optional[Dict]:
        # GAIA format: {"Question": ..., "Level": ..., "Final answer": ..., ...}
        question = item.get("Question", item.get("question", ""))
        level = item.get("Level", item.get("level", 1))
        answer = item.get("Final answer", item.get("answer", ""))
        file_name = item.get("file_name", item.get("file_path", ""))

        if not question:
            return None

        return {
            "id": task_id,
            "question": question,
            "level": int(level) if level else 1,
            "ground_truth": str(answer),
            "file_name": file_name,
            "tools_needed": self._infer_tools(question),
            "_raw": item,
        }

    def _infer_tools(self, question: str) -> List[str]:
        """Infer which tools the task needs"""
        tools = ["web_search"]  # Most tasks need search
        q_lower = question.lower()

        if any(kw in q_lower for kw in ["calculate", "sum", "average",
                                          "percentage", "difference"]):
            tools.append("calculator")
        if any(kw in q_lower for kw in ["file", "attachment", "pdf",
                                          "csv", "excel", "spreadsheet"]):
            tools.append("file_read")
        if any(kw in q_lower for kw in ["navigate", "browse", "website",
                                          "page", "click"]):
            tools.append("browser")
        if any(kw in q_lower for kw in ["code", "python", "script"]):
            tools.append("code_execute")

        return tools

    def _generate_mock_tasks(self) -> List[Dict]:
        return [
            {
                "id": "mock_gaia_L1_001",
                "question": "What is the population of Tokyo according to Wikipedia?",
                "level": 1,
                "ground_truth": "13960000",
                "file_name": "",
                "tools_needed": ["web_search"],
            },
            {
                "id": "mock_gaia_L2_001",
                "question": "Based on BLS.gov data, what was the US unemployment rate in January 2024?",
                "level": 2,
                "ground_truth": "3.7%",
                "file_name": "",
                "tools_needed": ["web_search", "calculator"],
            },
            {
                "id": "mock_gaia_L3_001",
                "question": "Identify the fruits in the painting 'Still Life with Apples' by Cézanne, cross-reference with a 1950s grocery catalog, and list them in alphabetical order.",
                "level": 3,
                "ground_truth": "apples, oranges, peaches",
                "file_name": "",
                "tools_needed": ["web_search", "browser"],
            },
        ]

    def run_single(self, task: Dict, agent_fn: Callable) -> BenchResult:
        """Execute a single GAIA task"""
        question = task["question"]
        level = task.get("level", 1)
        tools_needed = task.get("tools_needed", ["web_search"])

        # Build tools
        tools = self._build_gaia_tools(tools_needed)

        # Build messages
        messages = [
            {"role": "system", "content": (
                "You are a general AI assistant, skilled at using tools to answer complex questions."
                "Please use the provided tools to find answers."
                "The final answer must be concise and precise - a number, a name, or a comma-separated list."
                "Mark your final answer with [ANSWER] at the end of your response."
                f"This is a Level {level} difficulty question."
            )},
            {"role": "user", "content": question},
        ]

        # Max reasoning steps increases with level
        max_steps = {1: 3, 2: 8, 3: 20}.get(level, 10)

        for step in range(max_steps):
            try:
                response = agent_fn(messages=messages, tools=tools)
            except Exception as e:
                return BenchResult(task_id=task["id"], passed=False,
                                   error=f"agent_error_step{step}: {e}")

            # Extract response
            text = self._extract_text(response)
            tool_calls = self._extract_tool_calls(response)

            messages.append({"role": "assistant", "content": text})

            if not tool_calls:
                # Agent gave final answer
                break

            # Execute tool calls, return results
            for tc in tool_calls:
                result = self._execute_tool(tc)
                messages.append({
                    "role": "tool",
                    "content": json.dumps(result, ensure_ascii=False),
                    "tool_call_id": tc.get("id", ""),
                })

        # Extract final answer
        prediction = self._extract_final_answer(messages)
        ground_truth = task.get("ground_truth", "")

        # Validate
        passed, score = GAIAGrader.grade(prediction, ground_truth)

        return BenchResult(
            task_id=task["id"], passed=passed, score=score,
            detail={"prediction": prediction, "ground_truth": ground_truth,
                    "level": level, "steps": step + 1})

    def _build_gaia_tools(self, tools_needed: List[str]) -> List[Dict]:
        """Build the tool set needed by GAIA"""
        tool_map = {
            "web_search": {
                "type": "function", "function": {
                    "name": "web_search",
                    "description": "Search the internet for information",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "query": {"type": "string", "description": "Search keyword"}},
                                   "required": ["query"]}}},
            "calculator": {
                "type": "function", "function": {
                    "name": "calculator",
                    "description": "Perform mathematical calculations",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "expression": {"type": "string", "description": "Mathematical expression"}},
                                   "required": ["expression"]}}},
            "file_read": {
                "type": "function", "function": {
                    "name": "file_read",
                    "description": "Read file contents",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "path": {"type": "string", "description": "File path"}},
                                   "required": ["path"]}}},
            "browser": {
                "type": "function", "function": {
                    "name": "browser_navigate",
                    "description": "Browse web pages and extract content",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "url": {"type": "string", "description": "Web page URL"},
                                       "question": {"type": "string", "description": "Information to find on the page"}},
                                   "required": ["url"]}}},
            "code_execute": {
                "type": "function", "function": {
                    "name": "code_execute",
                    "description": "Execute Python code",
                    "parameters": {"type": "object",
                                   "properties": {
                                       "code": {"type": "string", "description": "Python code"}},
                                   "required": ["code"]}}},
        }

        return [tool_map[t] for t in tools_needed if t in tool_map]

    def _execute_tool(self, tool_call: Dict) -> Dict:
        """Execute a single tool call (mock result)"""
        name = tool_call.get("name", "")
        args = tool_call.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}

        # Return mock results here
        # In real execution, integrate with web_sense/file_read etc.
        if name == "web_search":
            return {"status": "mock", "query": args.get("query", ""),
                    "note": "Need to integrate with web_sense module"}
        elif name == "calculator":
            try:
                expr = args.get("expression", "")
                # Safe computation (only allow math expressions)
                result = eval(expr, {"__builtins__": {}},
                              {"abs": abs, "round": round, "min": min, "max": max,
                               "sum": sum, "len": len, "math": math})
                return {"result": result}
            except Exception as e:
                return {"error": str(e)}
        elif name == "file_read":
            return {"status": "mock", "path": args.get("path", "")}
        elif name == "browser_navigate":
            return {"status": "mock", "url": args.get("url", "")}
        elif name == "code_execute":
            return {"status": "mock", "code_length": len(args.get("code", ""))}

        return {"status": "unknown_tool"}

    def _extract_text(self, response: Any) -> str:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            msg = response.get("choices", [{}])[0].get("message", {})
            return msg.get("content", "")
        return str(response)

    def _extract_tool_calls(self, response: Any) -> List[Dict]:
        calls = []
        if isinstance(response, dict):
            msg = response.get("choices", [{}])[0].get("message", {})
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                calls.append({
                    "id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": args,
                })
        return calls

    def _extract_final_answer(self, messages: List[Dict]) -> str:
        """Extract final answer from conversation"""
        # Find [ANSWER] marker
        for msg in reversed(messages):
            content = msg.get("content", "")
            match = re.search(r"\[ANSWER\]\s*(.+?)(?:\n|$)", content)
            if match:
                return match.group(1).strip()

        # Find last assistant message
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "").strip()
                # Take the last line
                lines = [l.strip() for l in content.split("\n") if l.strip()]
                if lines:
                    return lines[-1]

        return ""

    def run_by_level(self, agent_fn: Callable,
                     max_tasks: int = 0) -> Dict[str, Any]:
        """Run by difficulty level"""
        if not self._tasks:
            self._tasks = self.load_tasks()

        results = {}
        for level in [1, 2, 3]:
            level_tasks = [t for t in self._tasks if t.get("level") == level]
            if not level_tasks:
                continue
            if max_tasks > 0:
                level_tasks = level_tasks[:max_tasks]

            report = self._run_task_list(agent_fn, level_tasks,
                                          f"GAIA_L{level}")
            results[f"Level_{level}"] = report

            logger.info(
                f"[GAIA/L{level}] accuracy={report.accuracy:.4f} "
                f"passed={report.passed}/{report.total}")

        return results

    def _run_task_list(self, agent_fn: Callable, tasks: List[Dict],
                       name: str) -> 'BenchReport':
        from benchmarks.bench_base import BenchReport
        report = BenchReport(benchmark_name=name, total=len(tasks))
        latencies = []

        for i, task in enumerate(tasks):
            t0 = time.time()
            try:
                result = self.run_single(task, agent_fn)
                result.latency_ms = (time.time() - t0) * 1000
            except Exception as e:
                result = BenchResult(task_id=task["id"], passed=False,
                                     error=str(e))

            report.results.append(result)
            latencies.append(result.latency_ms)
            if result.passed:
                report.passed += 1
            else:
                report.failed += 1

        report.accuracy = report.passed / max(1, report.total)
        report.avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0
        return report
