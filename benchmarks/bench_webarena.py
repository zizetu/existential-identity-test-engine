#!/usr/bin/env python3
"""
WebArena Browser Test Adapter - tical-code integrated version
============================================
Inherits BenchAdapter, uses agent-browser CLI to control Chrome,
Tests AI models' browser task execution capability.

Usage:
    CHROME_PATH=/usr/bin/google-chrome-stable \\
    python -m tical_code.benchmarks.runner --bench webarena \\
    --backend openai --model grok-4.3
"""
import json, os, re, subprocess, time
from typing import Any, Dict, List, Optional
from pathlib import Path

from benchmarks.bench_base import BenchAdapter, BenchResult, BenchReport


class BrowserError(Exception):
    pass


class Browser:
    """agent-browser CLI safe wrapper (no shell=True, anti-injection)"""

    def __init__(self):
        self.chrome = os.environ.get("CHROME_PATH", "/usr/bin/google-chrome-stable")

    def _run(self, args: List[str]) -> str:
        env = os.environ.copy()
        env["CHROME_PATH"] = self.chrome
        try:
            r = subprocess.run(["agent-browser"] + args, capture_output=True,
                               text=True, timeout=30, env=env)
            if r.returncode != 0:
                raise BrowserError(f"agent-browser {' '.join(args)} failed: {r.stderr.strip()[:200]}")
            return r.stdout.strip()
        except FileNotFoundError:
            raise BrowserError("agent-browser not installed. Run: npm install -g agent-browser")
        except subprocess.TimeoutExpired:
            raise BrowserError(f"agent-browser {' '.join(args)} timeout")

    def navigate(self, url: str) -> str: return self._run(["navigate", url])
    def click(self, ref: str) -> str: return self._run(["click", ref])
    def type(self, ref: str, text: str) -> str: return self._run(["type", ref, text])
    def snapshot(self) -> str: return self._run(["snapshot"])
    def back(self) -> str: return self._run(["back"])


class WebArenaAdapter(BenchAdapter):
    """WebArena adapter: browser task testing"""

    @property
    def name(self) -> str:
        return "webarena"

    def __init__(self, data_dir: str = "", output_dir: str = ""):
        super().__init__(data_dir, output_dir)
        self.browser = Browser()
        self.max_steps = 5

    def load_tasks(self, split: str = "test") -> List[Dict]:
        """Load test tasks from bench_data"""
        tasks = []

        # Find bench_data/webarena/ first
        data_paths = [
            Path(self.data_dir) / "webarena",
            Path(self.data_dir) / "bench_data" / "webarena",
            Path.cwd() / "bench_data" / "webarena",
        ]

        for dp in data_paths:
            if dp.exists():
                for f in sorted(dp.glob("*.json")):
                    try:
                        data = json.loads(f.read_text())
                        if isinstance(data, list):
                            tasks.extend(data)
                        elif isinstance(data, dict):
                            tasks.append(data)
                    except Exception:
                        continue
                break

        # Use built-in mock tasks if no real data
        if not tasks:
            tasks = self._mock_tasks()

        return tasks

    def _mock_tasks(self) -> List[Dict]:
        """WebArena test tasks - using real public websites"""
        return [
            {
                "id": "web_001",
                "goal": "Open example.com and confirm page title contains Example Domain",
                "start_url": "https://example.com",
                "success_match": "Example Domain",
            },
            {
                "id": "web_002",
                "goal": "Visit httpbin.org/get and confirm returned JSON contains url field",
                "start_url": "https://httpbin.org/get",
                "success_match": '"url"',
            },
            {
                "id": "web_003",
                "goal": "Search Wikipedia for Python article, confirm Python appears on page",
                "start_url": "https://en.wikipedia.org/wiki/Main_Page",
                "success_match": "Python",
            },
            {
                "id": "web_004",
                "goal": "Open books.toscrape.com and confirm page title contains Books",
                "start_url": "https://books.toscrape.com",
                "success_match": "Books",
            },
            {
                "id": "web_005",
                "goal": "Check if there are form elements on the httpbin.org/forms page",
                "start_url": "https://httpbin.org/forms/post",
                "success_match": "form",
            },
            {
                "id": "web_006",
                "goal": "Open example.com and click the Learn more link",
                "start_url": "https://example.com",
                "success_match": "RFC",
            },
            {
                "id": "web_007",
                "goal": "Visit httpbin.org/ip and check the returned IP info",
                "start_url": "https://httpbin.org/ip",
                "success_match": '"origin"',
            },
            {
                "id": "web_008",
                "goal": "Open httpbin.org/headers and check request header info",
                "start_url": "https://httpbin.org/headers",
                "success_match": '"headers"',
            },
            {
                "id": "web_009",
                "goal": "Confirm User-Agent field exists on httpbin.org/user-agent page",
                "start_url": "https://httpbin.org/user-agent",
                "success_match": '"user-agent"',
            },
            {
                "id": "web_010",
                "goal": "Find the first book title on books.toscrape.com",
                "start_url": "https://books.toscrape.com",
                "success_match": "A Light in the Attic",
            },
        ]

    def run_single(self, task: Dict, agent_fn) -> BenchResult:
        """Execute a single WebArena task"""
        task_id = task.get("id", "unknown")
        goal = task.get("goal", task.get("instruction", ""))
        start_url = task.get("start_url", task.get("url", "about:blank"))
        success_match = task.get("success_match", task.get("expected", ""))

        logs = []
        error = None

        try:
            # Navigate to start page
            logs.append({"step": 0, "action": f"navigate {start_url}"})
            self.browser.navigate(start_url)
            current = self.browser.snapshot()

            for step in range(1, self.max_steps + 1):
                # Build prompt
                prompt = (
                    f"You are a browser-controlling AI. Output browser action commands.\n\n"
                    f"Goal: {goal}\n\n"
                    f"Current page:\n{current[:2000]}\n\n"
                    f"Commands:\n"
                    f"  navigate <url>\n  click <ref>\n  type <ref> <text>\n"
                    f"  snapshot\n  back\n\n"
                    f"Output only one command at a time."
                )

                # Call the model
                response = agent_fn(messages=[{"role": "user", "content": prompt}])
                text = self._extract_text(response)

                # Parse command
                cmd = self._parse_command(text)
                logs.append({"step": step, "model": text[:200], "cmd": str(cmd)})

                if not cmd:
                    continue

                # Execute
                action, args = cmd
                if action == "navigate":
                    self.browser.navigate(args[0])
                elif action == "click":
                    self.browser.click(args[0])
                elif action == "type":
                    self.browser.type(args[0], args[1])
                elif action == "back":
                    self.browser.back()

                # Get page state
                current = self.browser.snapshot()
                logs[-1]["snapshot"] = current[:200]

                # Check if successful
                if self._check_success(current, success_match):
                    return BenchResult(
                        task_id=task_id, passed=True, score=1.0,
                        detail={"logs": logs, "steps": step}
                    )

        except Exception as e:
            error = str(e)

        return BenchResult(
            task_id=task_id, passed=False, score=0.0,
            error=error, detail={"logs": logs}
        )

    @staticmethod
    def _extract_text(response) -> str:
        """Extract text from agent_fn response (compatible with OpenAI/RealAgent format)"""
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            # RealAgent response format: {"choices": [{"message": {"content": "...", "tool_calls": [...]}}]}
            choices = response.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                if msg.get("content"):
                    return msg["content"]
            # Fallback: {"content": "..."} or {"text": "..."}
            return response.get("content") or response.get("text") or json.dumps(response)
        return str(response)

    @staticmethod
    def _parse_command(text: str) -> Optional[tuple]:
        """Parse browser commands from model output"""
        if not text:
            return None

        # Extract first code block content
        code = re.search(r"```(?:\w+)?\s*([^`]+)```", text, re.DOTALL)
        if code:
            text = code.group(1).strip()

        # Parse line by line
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue

            # navigate URL (compatible with "navigate https://...", "navigate to ...", "navigate URL ...")
            m = re.match(r"^navigate\s+(?:URL\s+|to\s+)?(.+)", line, re.I)
            if m:
                url = m.group(1).strip().strip("\"'")
                # Add https:// if no protocol prefix
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                return ("navigate", [url])

            # click REF
            m = re.match(r"^click\s+(\S+)", line, re.I)
            if m:
                return ("click", [m.group(1)])

            # type REF text
            m = re.match(r"^type\s+(\S+)\s+(.+)", line, re.I)
            if m:
                ref = m.group(1)
                text_val = m.group(2).strip().strip("\"'")
                return ("type", [ref, text_val])

            # snapshot
            if re.match(r"^snapshot", line, re.I):
                return ("snapshot", [])

            # back
            if re.match(r"^back$", line, re.I):
                return ("back", [])

        return None

    @staticmethod
    def _check_success(page_state: str, match) -> bool:
        """Check if page state meets success condition"""
        if not match:
            return False
        if isinstance(match, str):
            return match.lower() in page_state.lower()
        if isinstance(match, list):
            return all(str(m).lower() in page_state.lower() for m in match)
        if isinstance(match, dict):
            value = match.get("value", "")
            mode = match.get("mode", "contains")
            if mode == "regex":
                return bool(re.search(value, page_state, re.I))
            return str(value).lower() in page_state.lower()
        return False

    def run(self, agent_fn, split: str = "test", max_tasks: int = 0) -> BenchReport:
        """Run all WebArena tasks"""
        self._tasks = self.load_tasks(split)
        tasks = self._tasks[:max_tasks] if max_tasks > 0 else self._tasks

        results = [self.run_single(t, agent_fn) for t in tasks]

        passed = sum(1 for r in results if r.passed)
        report = BenchReport(
            benchmark_name=self.name, total=len(results),
            passed=passed, failed=len(results) - passed,
            accuracy=passed / len(results) if results else 0.0,
            results=results,
        )

        # Save report
        import json, os
        report_path = os.path.join(self.output_dir, f"{self.name}_report.json")
        with open(report_path, "w") as f:
            json.dump({
                "benchmark_name": report.benchmark_name,
                "total": report.total,
                "passed": report.passed,
                "failed": report.failed,
                "accuracy": report.accuracy,
                "results": [{
                    "task_id": r.task_id, "passed": r.passed,
                    "score": r.score, "error": r.error,
                } for r in results],
            }, f, indent=2)

        return report
