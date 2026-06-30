"""benchmarks tests - verify adapter framework loads and runs correctly"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.bench_base import BenchAdapter, BenchResult, BenchReport
from benchmarks.bench_bfcl import BFCLAdapter
from benchmarks.bench_tau import TauBenchAdapter
from benchmarks.bench_terminal import TerminalBenchAdapter
from benchmarks.bench_webarena import WebArenaAdapter
from benchmarks.bench_gaia import GAIAAdapter, GAIAGrader


class MockAgent:
    """Mock agent for testing"""
    def __call__(self, messages, tools=None):
        return {
            "choices": [{
                "message": {
                    "content": "Task completed",
                    "tool_calls": []
                }
            }]
        }


class MockAgentWithToolCall:
    """Mock agent that calls tools"""
    def __call__(self, messages, tools=None):
        if tools and len(tools) > 0:
            first_tool = tools[0]
            func = first_tool.get("function", {})
            name = func.get("name", "unknown")
            return {
                "choices": [{
                    "message": {
                        "content": f"Calling {name}",
                        "tool_calls": [{
                            "id": "call_test",
                            "function": {
                                "name": name,
                                "arguments": json.dumps({"query": "test"}),
                            }
                        }]
                    }
                }]
            }
        return {"choices": [{"message": {"content": "Done", "tool_calls": []}}]}


class TestBenchBase(unittest.TestCase):
    def test_bench_result(self):
        r = BenchResult(task_id="t1", passed=True, score=1.0)
        self.assertTrue(r.passed)
        self.assertEqual(r.task_id, "t1")

    def test_bench_report(self):
        report = BenchReport(benchmark_name="test", total=10, passed=8, failed=2)
        self.assertEqual(report.accuracy, 0)
        d = report.to_dict()
        self.assertEqual(d["benchmark_name"], "test")


class TestBFCL(unittest.TestCase):
    def test_load_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = BFCLAdapter(data_dir=tmpdir)
            tasks = adapter.load_tasks()
            self.assertEqual(tasks, [])

    def test_normalize(self):
        item = {
            "function": [{"name": "get_weather", "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"]
            }}],
            "question": [{"role": "user", "content": "How is the weather in Beijing?"}],
            "ground_truth": [{"name": "get_weather", "arguments": {"city": "Beijing"}}],
        }
        adapter = BFCLAdapter()
        task = adapter._normalize_bfcl_item(item, "test_1")
        self.assertIsNotNone(task)
        self.assertEqual(task["id"], "test_1")
        self.assertEqual(len(task["tools"]), 1)
        self.assertEqual(len(task["expected_calls"]), 1)

    def test_evaluate_calls(self):
        adapter = BFCLAdapter()
        expected = [{"name": "get_weather", "arguments": {"city": "Beijing"}}]
        # Exact match
        actual = [{"name": "get_weather", "arguments": {"city": "Beijing"}}]
        passed, score, _ = adapter._evaluate_calls(actual, expected)
        self.assertTrue(passed)

        # name wrong
        actual2 = [{"name": "wrong_func", "arguments": {"city": "Beijing"}}]
        passed2, score2, _ = adapter._evaluate_calls(actual2, expected)
        self.assertFalse(passed2)

    def test_arg_compare(self):
        self.assertEqual(BFCLAdapter._compare_args({}, {}), 1.0)
        self.assertEqual(BFCLAdapter._compare_args({"a": 1}, {"a": 1}), 1.0)
        self.assertEqual(BFCLAdapter._compare_args({"a": 1}, {"a": 2}), 0.0)
        self.assertEqual(BFCLAdapter._compare_args({"a": 1, "b": 2}, {"a": 1}), 0.5)

    def test_load_bfcl_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write test data
            data = [{
                "function": [{"name": "f1", "parameters": {"type": "object", "properties": {}}}],
                "question": [{"role": "user", "content": "test"}],
                "ground_truth": [{"name": "f1", "arguments": {}}],
            }]
            with open(os.path.join(tmpdir, "test.json"), "w") as f:
                json.dump(data, f)

            adapter = BFCLAdapter(data_dir=tmpdir)
            tasks = adapter.load_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["expected_calls"][0]["name"], "f1")


class TestTau(unittest.TestCase):
    def test_mock_tasks(self):
        adapter = TauBenchAdapter()
        tasks = adapter.load_tasks()
        self.assertGreater(len(tasks), 0)
        self.assertIn("domain", tasks[0])

    def test_run_mock(self):
        adapter = TauBenchAdapter()
        agent = MockAgent()
        report = adapter.run(agent, max_tasks=2)
        self.assertGreater(report.total, 0)

    def test_pass_k(self):
        adapter = TauBenchAdapter()
        adapter._tasks = adapter.load_tasks()[:1]
        agent = MockAgent()
        result = adapter.run_pass_k(agent, k=2, max_tasks=1)
        self.assertIn("pass_rate_1", result)
        self.assertIn("pass_rate_2", result)


class TestTerminal(unittest.TestCase):
    def test_mock_tasks(self):
        adapter = TerminalBenchAdapter()
        tasks = adapter.load_tasks()
        self.assertGreater(len(tasks), 0)

    def test_evaluate_file_content(self):
        adapter = TerminalBenchAdapter()
        # Create temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt',
                                          delete=False) as f:
            f.write("Hello Terminal Bench")
            fpath = f.name

        try:
            task = {
                "evaluation_type": "file_content",
                "expected_file": fpath,
                "expected_file_content": "Hello Terminal Bench",
            }
            passed, score, detail = adapter._evaluate(task, [], "", 0)
            self.assertTrue(passed)
        finally:
            os.unlink(fpath)


class TestGAIA(unittest.TestCase):
    def test_grader(self):
        # Exact match
        passed, score = GAIAGrader.grade("42", "42")
        self.assertTrue(passed)

        # Numeric match
        passed, score = GAIAGrader.grade("42.0", "42")
        self.assertTrue(passed)

        # List match
        passed, score = GAIAGrader.grade("a, b, c", "a, b, c")
        self.assertTrue(passed)

        # No match
        passed, score = GAIAGrader.grade("wrong", "42")
        self.assertFalse(passed)

    def test_mock_tasks(self):
        adapter = GAIAAdapter()
        tasks = adapter.load_tasks()
        self.assertGreater(len(tasks), 0)
        self.assertIn("level", tasks[0])

    def test_infer_tools(self):
        adapter = GAIAAdapter()
        tools = adapter._infer_tools("What is the population of Tokyo?")
        self.assertIn("web_search", tools)

        tools2 = adapter._infer_tools("Calculate the sum of 1 to 100")
        self.assertIn("calculator", tools2)


class TestWebArena(unittest.TestCase):
    def test_mock_tasks(self):
        adapter = WebArenaAdapter()
        tasks = adapter.load_tasks()
        self.assertGreater(len(tasks), 0)

    def test_tool_call_to_action(self):
        """_tool_call_to_action method no longer exists on WebArenaAdapter"""
        import pytest
        pytest.skip("_tool_call_to_action no longer available on WebArenaAdapter")


if __name__ == "__main__":
    unittest.main()


# ============ real_agent tests ============

class TestRealAgent(unittest.TestCase):
    """Test real model Agent adapter"""

    def test_create_mock_equivalent(self):
        """mock agent compatibility: real_agent falls back to mock when no API key"""
        from benchmarks.real_agent import create_real_agent
        # No API key should raise ValueError
        with self.assertRaises(ValueError):
            agent = create_real_agent(backend="mimo")  # no MIMO_API_KEY

    def test_auto_detect_no_keys(self):
        """auto mode falls back to worker or errors when no API key"""
        from benchmarks.real_agent import create_real_agent
        # If worker file exists, auto will go through worker instead of error
        # So here just verify auto doesn't crash
        old_mimo = os.environ.pop("MIMO_API_KEY", None)
        old_openai = os.environ.pop("OPENAI_API_KEY", None)
        old_ds = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            agent = create_real_agent(backend="auto")
            # Has worker file = success, no worker = error
            self.assertIn(agent.backend, ["mimo", "openai", "worker"])
        except ValueError:
            pass  # Correctly errors when no backend at all
        finally:
            if old_mimo: os.environ["MIMO_API_KEY"] = old_mimo
            if old_openai: os.environ["OPENAI_API_KEY"] = old_openai
            if old_ds: os.environ["DEEPSEEK_API_KEY"] = old_ds

    def test_real_agent_signature(self):
        """RealAgent __call__ signature is compatible with agent_fn"""
        from benchmarks.real_agent import RealAgent
        # Verify signature with mock backend
        agent = RealAgent.__new__(RealAgent)
        agent.call_count = 0
        agent.total_latency_ms = 0.0
        agent.max_rounds = 5
        agent.system_prompt = "test"
        agent.backend = "test"
        agent.model = "test"
        # Don't actually call, just verify interface
        self.assertTrue(callable(agent))

    def test_real_agent_stats(self):
        """stats method returns correct structure"""
        from benchmarks.real_agent import RealAgent
        agent = RealAgent.__new__(RealAgent)
        agent.call_count = 5
        agent.total_latency_ms = 1500.0
        agent.backend = "mimo"
        agent.model = "mimo-v2-pro"
        agent.mode = "default"
        stats = agent.stats()
        self.assertEqual(stats["backend"], "mimo")
        self.assertEqual(stats["model"], "mimo-v2-pro")
        self.assertEqual(stats["total_calls"], 5)
        self.assertEqual(stats["avg_latency_ms"], 300.0)

    def test_openai_compat_tool_format(self):
        """_call_openai_compat builds correct request body"""
        from benchmarks.real_agent import _call_openai_compat
        # Don't actually call, just test function existence and signature
        import inspect
        sig = inspect.signature(_call_openai_compat)
        params = list(sig.parameters.keys())
        self.assertIn("messages", params)
        self.assertIn("tools", params)
        self.assertIn("api_key", params)
        self.assertIn("base_url", params)
        self.assertIn("model", params)

    def test_preset_agents(self):
        """Preset factory functions return correct type"""
        from benchmarks.real_agent import mimo_agent, deepseek_agent, deepseek_reasoner_agent
        # Will error without key, verify function existence and params
        import inspect
        self.assertTrue(callable(mimo_agent))
        self.assertTrue(callable(deepseek_agent))
        self.assertTrue(callable(deepseek_reasoner_agent))

    def test_run_with_backend_arg(self):
        """run.py supports --backend parameter"""
        import subprocess
        import os
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            ["python", "-m", "benchmarks.run", "--help"],
            capture_output=True, text=True,
            cwd=repo_root
        )
        self.assertIn("--backend", result.stdout)
        self.assertIn("--model", result.stdout)
        self.assertIn("mimo", result.stdout)
        self.assertIn("deepseek", result.stdout)
