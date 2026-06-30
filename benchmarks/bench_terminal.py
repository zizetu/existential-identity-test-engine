"""Terminal Bench 2.0 Adapter - terminal task testing

Tests Agent in real terminal environment:
- Execute shell commands
- Parse command output
- Multi-step operation composition
- Error recovery

tical-code existing foundation:
- worker_loop + agent_runtime bash execution
- security_baseline command security review
- FileTool file read/write

Adaptation strategy:
- Task description -> LLM planning -> execute shell -> validate output/file state
"""

import json
import os
import shlex
import subprocess
import time
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from benchmarks.bench_base import BenchAdapter, BenchResult

logger = logging.getLogger("tical-code.benchmark.terminal")


class TerminalBenchAdapter(BenchAdapter):
    """Terminal Bench 2.0 Adapter"""

    @property
    def name(self) -> str:
        return "terminal_bench_2"

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
                    logger.warning(f"[Terminal] Failed to load {fname}: {e}")

        if not tasks:
            tasks = self._generate_mock_tasks()
            logger.info(f"[Terminal] Using {len(tasks)} mock tasks")

        logger.info(f"[Terminal] Loaded {len(tasks)} tasks")
        return tasks

    def _normalize(self, item: Dict, task_id: str) -> Optional[Dict]:
        return {
            "id": task_id,
            "description": item.get("description", item.get("prompt", "")),
            "setup_commands": item.get("setup_commands", []),
            "expected_stdout": item.get("expected_stdout", None),
            "expected_file": item.get("expected_file", None),
            "expected_file_content": item.get("expected_file_content", None),
            "expected_exit_code": item.get("expected_exit_code", 0),
            "timeout": item.get("timeout", 60),
            "evaluation_type": item.get("evaluation_type", "output_match"),
            "_raw": item,
        }

    def _generate_mock_tasks(self) -> List[Dict]:
        return [
            {
                "id": "mock_term_001",
                "description": "Create file /tmp/bench_test.txt, write 'Hello Terminal Bench'",
                "setup_commands": ["rm -f /tmp/bench_test.txt"],
                "expected_file": "/tmp/bench_test.txt",
                "expected_file_content": "Hello Terminal Bench",
                "evaluation_type": "file_content",
            },
            {
                "id": "mock_term_002",
                "description": "List all .py files in current directory, sorted by modification time",
                "setup_commands": [],
                "expected_stdout": None,  # Flexible validation
                "evaluation_type": "command_success",
            },
            {
                "id": "mock_term_003",
                "description": "Calculate sum of 1 to 100, output result",
                "setup_commands": [],
                "expected_stdout": "5050",
                "evaluation_type": "output_contains",
            },
            {
                "id": "mock_term_004",
                "description": "Find lines containing root in /etc/passwd",
                "setup_commands": [],
                "expected_stdout": None,
                "evaluation_type": "command_success",
            },
        ]

    def run_single(self, task: Dict, agent_fn: Callable) -> BenchResult:
        """Execute a single terminal task

        agent_fn signature: (messages, tools) -> response
        But terminal tasks are more direct: give agent description, let it generate commands, we execute
        """
        # 1. Setup
        for cmd in task.get("setup_commands", []):
            try:
                subprocess.run(shlex.split(cmd), timeout=10,
                               capture_output=True)
            except Exception:
                pass

        # 2. Call agent to get commands
        tools = self._build_terminal_tools()
        messages = [
            {"role": "system", "content": (
                "You are a terminal operation assistant. The user will give you a task description, "
                "you need to generate shell commands to complete it. Use the shell_exec tool to execute commands. "
                "Execute one command at a time, observe output before deciding the next step."
            )},
            {"role": "user", "content": task["description"]},
        ]

        max_attempts = 5
        attempt = 0
        all_commands = []
        last_output = ""
        last_exit_code = 0

        while attempt < max_attempts:
            attempt += 1
            try:
                response = agent_fn(messages=messages, tools=tools)
            except Exception as e:
                return BenchResult(task_id=task["id"], passed=False,
                                   error=f"agent_error: {e}")

            # Extract tool calls
            tool_calls = self._extract_tool_calls(response)
            if not tool_calls:
                # Agent may consider task complete
                break

            for tc in tool_calls:
                if tc.get("name") == "shell_exec":
                    cmd = tc.get("arguments", {}).get("cmd", "")
                    if not cmd:
                        continue

                    # Security check
                    from security_baseline import check_shell_command
                    if hasattr(check_shell_command, '__call__'):
                        # Simple security check
                        dangerous = ["rm -rf /", "mkfs", "dd if=", ":(){ :|:&"]
                        if any(d in cmd for d in dangerous):
                            messages.append({
                                "role": "assistant",
                                "content": f"Refused to execute dangerous command: {cmd}"
                            })
                            messages.append({
                                "role": "user",
                                "content": "This command was rejected by security policy, please use another method to complete the task."
                            })
                            continue

                    all_commands.append(cmd)

                    # Execute
                    try:
                        proc = subprocess.run(
                            shlex.split(cmd), timeout=task.get("timeout", 60),
                            capture_output=True, text=True)
                        last_output = proc.stdout + proc.stderr
                        last_exit_code = proc.returncode
                    except subprocess.TimeoutExpired:
                        last_output = "TIMEOUT"
                        last_exit_code = -1
                    except Exception as e:
                        last_output = f"ERROR: {e}"
                        last_exit_code = -1

                    # Tell agent execution result
                    messages.append({"role": "assistant",
                                     "content": f"$ {cmd}\n{last_output}"})
                    messages.append({"role": "user",
                                     "content": "Continue or done? If task is complete, tell me the result directly."})

            # Check if already complete
            if self._quick_check(task, last_output, last_exit_code):
                break

        # 3. Evaluate
        passed, score, detail = self._evaluate(task, all_commands,
                                                last_output, last_exit_code)

        return BenchResult(task_id=task["id"], passed=passed, score=score,
                           detail=detail)

    def _build_terminal_tools(self) -> List[Dict]:
        return [
            {"type": "function", "function": {
                "name": "shell_exec",
                "description": "Execute shell command and return output",
                "parameters": {"type": "object",
                               "properties": {
                                   "cmd": {"type": "string", "description": "Command to execute"}},
                               "required": ["cmd"]}}},
        ]

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
                        args = {"cmd": args}
                calls.append({"name": func.get("name", ""), "arguments": args})
        return calls

    def _quick_check(self, task: Dict, output: str, exit_code: int) -> bool:
        """Quickly determine if task may already be complete"""
        eval_type = task.get("evaluation_type", "output_match")
        if eval_type == "file_content" and task.get("expected_file"):
            return os.path.isfile(task["expected_file"])
        return False

    def _evaluate(self, task: Dict, commands: List[str],
                  last_output: str, last_exit_code: int
                  ) -> Tuple[bool, float, Dict]:
        eval_type = task.get("evaluation_type", "output_match")
        detail = {"commands_run": commands, "last_output": last_output[:500],
                  "last_exit_code": last_exit_code}

        if eval_type == "output_match":
            expected = task.get("expected_stdout")
            if expected is None:
                # No expected output, pass if command succeeded
                return last_exit_code == 0, 1.0 if last_exit_code == 0 else 0.0, detail
            passed = expected.strip() in last_output.strip()
            return passed, 1.0 if passed else 0.0, detail

        elif eval_type == "output_contains":
            expected = task.get("expected_stdout", "")
            passed = expected in last_output
            return passed, 1.0 if passed else 0.0, detail

        elif eval_type == "file_content":
            expected_file = task.get("expected_file")
            expected_content = task.get("expected_file_content")
            if not expected_file or not os.path.isfile(expected_file):
                return False, 0.0, {**detail, "reason": "file_not_found"}
            try:
                with open(expected_file, "r") as f:
                    actual = f.read().strip()
                expected = (expected_content or "").strip()
                passed = expected in actual
                return passed, 1.0 if passed else 0.5, {
                    **detail, "actual_content": actual[:200]}
            except Exception as e:
                return False, 0.0, {**detail, "error": str(e)}

        elif eval_type == "command_success":
            passed = last_exit_code == 0 and len(commands) > 0
            return passed, 1.0 if passed else 0.0, detail

        return False, 0.0, detail
