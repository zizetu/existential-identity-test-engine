"""benchmarks entry point - run all benches with one command

Usage:
    python -m benchmarks.run --bench bfcl --data-dir ./bench_data
    python -m benchmarks.run --bench all --max-tasks 10
    python -m benchmarks.run --bench bfcl --backend mimo
    python -m benchmarks.run --bench tau --backend deepseek --model deepseek-chat

Supported benchmarks:
- bfcl: BFCL v3 (tool calling)
- tau: τ²-Bench (policy compliance)
- terminal: Terminal Bench 2.0 (terminal tasks)
- webarena: WebArena (browser tasks)
- gaia: GAIA (general assistant)
- all: all

Backends:
- mock: mock agent (default)
- mimo: MiMo Token Plan (needs MIMO_API_KEY)
- deepseek: DeepSeek (needs DEEPSEEK_API_KEY)
- openai: OpenAI compatible (needs OPENAI_API_KEY+OPENAI_BASE_URL)
- worker: EITElite worker internal
- auto: auto-detect available backends
"""

import argparse
import json
import logging
import os
import sys

# Ensure EITElite root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.bench_bfcl import BFCLAdapter
from benchmarks.bench_tau import TauBenchAdapter
from benchmarks.bench_terminal import TerminalBenchAdapter
from benchmarks.bench_webarena import WebArenaAdapter
from benchmarks.bench_gaia import GAIAAdapter
from benchmarks.real_agent import create_real_agent, mimo_agent, deepseek_agent
from benchmarks.bench_bfcl import BFCLBenchReport, merge_agent_stats

logger = logging.getLogger("EITElite.benchmark")


ADAPTERS = {
    "bfcl": BFCLAdapter,
    "tau": TauBenchAdapter,
    "terminal": TerminalBenchAdapter,
    "webarena": WebArenaAdapter,
    "gaia": GAIAAdapter,
}


def create_mock_agent():
    """Create mock agent for testing adapters themselves"""
    call_count = [0]

    def mock_agent(messages, tools=None):
        call_count[0] += 1
        # Return a simple tool call
        if tools and len(tools) > 0:
            first_tool = tools[0]
            func = first_tool.get("function", {})
            name = func.get("name", "unknown")
            params = func.get("parameters", {}).get("properties", {})
            args = {k: f"mock_value_{k}" for k in params.keys()}
            return {
                "choices": [{
                    "message": {
                        "content": f"Execute {name}",
                        "tool_calls": [{
                            "id": f"call_{call_count[0]}",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            }
                        }]
                    }
                }]
            }
        return {
            "choices": [{
                "message": {
                    "content": "Task complete",
                    "tool_calls": []
                }
            }]
        }

    return mock_agent


def run_benchmark(bench_name: str, data_dir: str = "",
                  output_dir: str = "", max_tasks: int = 0,
                  agent_fn=None):
    """Run specified benchmark"""
    if bench_name not in ADAPTERS:
        logger.error(f"Unknown benchmark: {bench_name}")
        logger.info(f"Available: {list(ADAPTERS.keys())}")
        return None

    adapter_cls = ADAPTERS[bench_name]
    adapter = adapter_cls(data_dir=data_dir, output_dir=output_dir)

    if agent_fn is None:
        agent_fn = create_mock_agent()
        logger.info(f"Using mock agent for {bench_name}")

    report = adapter.run(agent_fn, max_tasks=max_tasks)

    print(f"\n{'='*60}")
    print(f"  {report.benchmark_name} Results")
    print(f"{'='*60}")
    print(f"  Total:   {report.total}")
    print(f"  Passed:  {report.passed}")
    print(f"  Failed:  {report.failed}")
    print(f"  Skipped: {report.skipped}")
    print(f"  Accuracy: {report.accuracy:.4f}")
    print(f"  Avg Latency: {report.avg_latency_ms:.1f}ms")
    print(f"{'='*60}\n")

    return report


def resolve_agent(backend: str, model: str = "", mode: str = "raw"):
    """Create agent_fn based on backend parameter"""
    if backend == "mock":
        return create_mock_agent()
    elif backend == "mimo":
        return mimo_agent(model=model or "mimo-v2-pro", mode=mode)
    elif backend == "deepseek":
        return deepseek_agent(model=model or "deepseek-chat", mode=mode)
    elif backend == "openai":
        return create_real_agent(backend="openai", model=model, mode=mode)
    elif backend == "worker":
        return create_real_agent(backend="worker", model=model, mode=mode)
    elif backend == "auto":
        return create_real_agent(backend="auto", model=model, mode=mode)
    else:
        raise ValueError(f"Unknown backend: {backend}. Use mock/mimo/deepseek/openai/worker/auto")


def main():
    parser = argparse.ArgumentParser(description="EITElite Benchmark Runner")
    parser.add_argument("--bench", type=str, default="all",
                        choices=list(ADAPTERS.keys()) + ["all"],
                        help="Benchmark to run")
    parser.add_argument("--data-dir", type=str, default="",
                        help="Benchmark data directory")
    parser.add_argument("--output-dir", type=str, default="",
                        help="Output directory for reports")
    parser.add_argument("--max-tasks", type=int, default=0,
                        help="Max tasks per benchmark (0=all)")
    parser.add_argument("--mock", action="store_true",
                        help="Use mock agent (shorthand for --backend mock)")
    parser.add_argument("--backend", type=str, default="mock",
                        choices=["mock", "mimo", "deepseek", "openai", "worker", "auto"],
                        help="LLM backend (default: mock)")
    parser.add_argument("--model", type=str, default="",
                        help="Model name override (e.g. mimo-v2-pro, deepseek-reasoner)")
    parser.add_argument("--mode", type=str, default="raw",
                        choices=["raw", "schema", "cognitive"],
                        help="Agent mode: raw=baseline, schema=validate+retry, cognitive=reserved")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    bench_names = list(ADAPTERS.keys()) if args.bench == "all" else [args.bench]

    # --mock is a shortcut for --backend mock
    backend = "mock" if args.mock else args.backend
    agent_fn = resolve_agent(backend, args.model, args.mode)
    if backend != "mock":
        logger.info(f"Using real backend: {backend}, model: {args.model or 'default'}")

    results = {}
    for name in bench_names:
        report = run_benchmark(
            name, data_dir=args.data_dir, output_dir=args.output_dir,
            max_tasks=args.max_tasks, agent_fn=agent_fn)
        if report:
            results[name] = report.to_dict()

    # Agent stats
    if hasattr(agent_fn, "stats"):
        stats = agent_fn.stats()
        print(f"\nAgent Stats: {stats}")

    # Save summary
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        summary_path = os.path.join(args.output_dir, "all_benchmarks_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"Summary saved to {summary_path}")


if __name__ == "__main__":
    main()
