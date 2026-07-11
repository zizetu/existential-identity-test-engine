#!/usr/bin/env python3
"""
SG Full Benchmark Suite - Grok 4.3
Runs all benchmarks sequentially using Grok API.
Auto-saves reports to $PROJECT_DIR/results/
"""

import subprocess, sys, os, time, json, shlex
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", str(Path.home() / "project")))
LOG = Path("/tmp/sg_full_bench.log")
RESULTS = PROJECT_DIR / "results"
RESULTS.mkdir(exist_ok=True)

# Grok credentials
GROK_KEY = os.environ.get("GROK_API_KEY", "")
GROK_URL = "https://api.x.ai/v1"
GROK_MODEL = "grok-4.3"

ENV = os.environ.copy()
ENV["OPENAI_API_KEY"] = GROK_KEY
ENV["OPENAI_BASE_URL"] = GROK_URL

def log(m):
    t = time.strftime("%H:%M:%S")
    line = f"[{t}] {m}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def run_bench(cmd, desc, timeout=3600):
    log(f"--- {desc} ---")
    log(f"CMD: {cmd[:200]}")
    t0 = time.time()
    try:
        r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=timeout, env=ENV)
        elapsed = time.time() - t0
        log(f"Exit: {r.returncode} ({elapsed:.0f}s)")
        for line in (r.stdout + r.stderr).split('\n')[-10:]:
            if any(w in line.lower() for w in ['score', 'acc', 'passed', 'failed', 'error', 'report']):
                log(f"  {line.strip()[:120]}")
        return r.returncode
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT after {timeout}s")
        return -1

# ============================================================
log("=" * 60)
log("SG FULL BENCHMARK - Grok 4.3")
log("=" * 60)

# 1. BFCL (1720 tasks, ~2h)
run_bench(
    f"cd {PROJECT_DIR} && python3 -m tical_code.benchmarks.runner --bench bfcl "
    f"--backend openai --model {GROK_MODEL} --mode raw "
    f"--data-dir {PROJECT_DIR}/bench_data/bfcl_real "
    f"--output-dir {RESULTS}",
    "BFCL 1720 (raw mode, Grok 4.3)",
    timeout=7200
)

# 2. τ-bench
run_bench(
    f"cd {PROJECT_DIR} && python3 -m tical_code.benchmarks.runner --bench tau "
    f"--backend openai --model {GROK_MODEL} "
    f"--data-dir {PROJECT_DIR}/bench_data/tau_real "
    f"--output-dir {RESULTS}",
    "τ-bench (Grok 4.3)",
    timeout=1800
)

# 3. Terminal (fast)
run_bench(
    f"cd {PROJECT_DIR} && python3 -m tical_code.benchmarks.runner --bench terminal "
    f"--backend openai --model {GROK_MODEL} "
    f"--data-dir {PROJECT_DIR}/bench_data "
    f"--output-dir {RESULTS}",
    "Terminal Bench (Grok 4.3)",
    timeout=600
)

# 4. WebArena
run_bench(
    f"cd {PROJECT_DIR} && CHROME_PATH=/usr/bin/google-chrome python3 -m tical_code.benchmarks.runner "
    f"--bench webarena --backend openai --model {GROK_MODEL} "
    f"--data-dir {PROJECT_DIR}/bench_data "
    f"--output-dir {RESULTS}",
    "WebArena (Grok 4.3)",
    timeout=1800
)

# 5. AgentBench OS
run_bench(
    f"cd {PROJECT_DIR} && python3 -m tical_code.benchmarks.runner --bench agentbench "
    f"--backend openai --model {GROK_MODEL} "
    f"--data-dir {PROJECT_DIR}/bench_data "
    f"--output-dir {RESULTS}",
    "AgentBench OS (Grok 4.3)",
    timeout=1800
)

# Summary
log("=" * 60)
log("ALL BENCHMARKS COMPLETE")
log(f"Reports in: {RESULTS}")
for f in sorted(RESULTS.glob("*_report.json")):
    size = f.stat().st_size
    log(f"  {f.name} ({size/1024:.0f}KB)")
log("=" * 60)
