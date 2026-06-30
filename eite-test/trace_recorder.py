#!/usr/bin/env python3
"""
TraceRecorder - Model 0 Training Data Collector

Embedded in Worker's tool execution loop, records the complete trace of each task execution.
Does not interfere with execution logic, just acts as a 'recorder'.

Outputs to training_data/eite_trace/ directory,
format consistent with data_pipeline.py's benchmark_to_samples output.

Usage (auto-invoked by unified_worker.py):
  from eite_test.trace_recorder import TraceRecorder
  rec = TraceRecorder()
  rec.start_task(task_id, prompt, system_name)
  rec.record_tool(tool_name, args, result, verified)
  rec.end_task(success)
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

# --- Path: prefer EITE_DATA_ROOT, otherwise default to eitelite / tical-code project root ---

def _get_training_dir() -> Path:
    """Training data directory, controlled by EITE_DATA_ROOT environment variable"""
    root = Path(os.getenv("EITE_DATA_ROOT", 
               Path(__file__).resolve().parent.parent))
    return root / "training_data" / "eite_trace"


# --- Single Task Trace ---

class TaskTrace:
    """Records all tool calls of a task from start to finish"""
    
    def __init__(self, task_id: str, prompt: str, system: str):
        self.task_id = task_id
        self.prompt = prompt[:500]          # Truncate to prevent excessive size
        self.system = system
        self.tools: list = []
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.success = False
    
    def record_tool(self, name: str, args: dict, result: dict, verified: bool):
        """Record a tool call (called after execute() + eite.verify())"""
        entry = {
            "tool": name,
            "args_safe": _sanitize_args(name, args),   # Sanitize sensitive parameters
            "exit_code": result.get("exit_code", result.get("verified") is True),
            "eite_verified": verified,
            "timestamp": time.time(),
        }
        self.tools.append(entry)
    
    def finish(self, success: bool):
        self.success = success
        self.end_time = time.time()
    
    def to_sample(self) -> dict:
        """Convert to training sample format (compatible with data_pipeline.py)"""
        raw = f"{self.system}_{self.task_id}_{len(self.tools)}"
        return {
            "id": hashlib.sha256(raw.encode()).hexdigest()[:12],
            "instruction": f"[EITE Trace] {self.task_id}",
            "output": json.dumps({
                "tools": self.tools,
                "total_steps": len(self.tools),
                "success": self.success,
                "elapsed_s": round((self.end_time or time.time()) - self.start_time, 2),
            }, ensure_ascii=False),
            "system": self.system,
            "level": "TRACE",
            "task_id": self.task_id,
            "verified": self.success,
            "steps": len(self.tools),
            "source": "eite_trace",
            "timestamp": self.start_time,
        }


# --- Sensitive Parameter Sanitization ---

_SENSITIVE_KEYS = {"token", "key", "password", "secret", "auth", "api_key", 
                   "private_key", "credential", "bearer", "authorization"}

def _sanitize_args(tool_name: str, args: dict) -> dict:
    """Sanitize: replace sensitive field values, keep paths and commands"""
    safe = {}
    for k, v in args.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            safe[k] = "***"
        elif tool_name == "file_write" and k == "content":
            safe[k] = f"<{len(str(v))} bytes>"  # Don't record file content
        elif tool_name == "bash" and k == "command":
            safe[k] = str(v)[:200]                # Truncate long commands
        else:
            safe[k] = str(v)[:200]
    return safe


    # --- Collector (Singleton) ---

class TraceRecorder:
    """
    Trace recorder embedded in Worker loop.
    Each Worker instance holds one TraceRecorder.
    
    Records locally and auto-POSTs to target_url after accumulating batch_size entries.
    If the test station endpoint isn't ready yet, data stays safely local - no data loss.
    """
    
    def __init__(self, system_name: str = "eitelite", enabled: bool = True,
                 target_url: str = "", batch_size: int = 10):
        self.enabled = enabled
        self.system = system_name
        self.target_url = target_url
        self.batch_size = batch_size
        self._trace: Optional[TaskTrace] = None
        self._output_dir = _get_training_dir()
        self._pending_count = 0  # Cumulative count of pending uploads
        if enabled:
            self._output_dir.mkdir(parents=True, exist_ok=True)
    
    # --- Three-Phase Hooks ---
    
    def on_task_start(self, task_id: str, prompt: str = ""):
        """Called when Worker starts processing a message"""
        if not self.enabled:
            return
        self._trace = TaskTrace(task_id, prompt, self.system)
    
    def on_tool_result(self, tool_name: str, args: dict, result: dict, verified: bool):
        """Called after each tool execution and verification (after execute() + eite.verify())"""
        if not self.enabled or self._trace is None:
            return
        self._trace.record_tool(tool_name, args, result, verified)
    
    def on_task_end(self, success: bool):
        """Called after Worker completes one round of processing"""
        if not self.enabled or self._trace is None:
            return
        self._trace.finish(success)
        sample = self._trace.to_sample()
        self._write(sample)
        self._pending_count += 1
        self._trace = None
        
        # Enough batches accumulated → try upload
        if self.target_url and self._pending_count >= self.batch_size:
            self.flush()
    
    # --- Write ---
    
    def _write(self, sample: dict):
        """Append write to .jsonl file"""
        if not self.enabled:
            return
        date = time.strftime("%Y%m%d")
        path = self._output_dir / f"trace_{date}.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    # --- Batch Upload ---

    def flush(self):
        """
        Batch POST locally cached trace data to target_url.
        Resets pending_count on success.
        On failure, keeps data locally and retries on next flush().
        """
        if not self.target_url or self._pending_count == 0:
            return
        
        samples = []
        try:
            # Read all unsent samples from today's file
            date = time.strftime("%Y%m%d")
            path = self._output_dir / f"trace_{date}.jsonl"
            if not path.exists():
                self._pending_count = 0
                return
            
            samples = []
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        samples.append(json.loads(line))
            
            if not samples:
                self._pending_count = 0
                return
            
            # POST send
            import urllib.request
            body = json.dumps(samples, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.target_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Trace-System": self.system,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    self._pending_count = 0
                    import logging
                    logging.getLogger("tical-code.trace").info(
                        f"flushed {len(samples)} traces to {self.target_url}"
                    )
        except Exception as e:
            # Any failure does not clear pending_count, retry next time
            import logging
            logging.getLogger("tical-code.trace").warning(
                f"flush failed ({len(samples) if 'samples' in dir() else '?'} traces): {e}"
            )


# --- Can also run directly: manual call demo ---

if __name__ == "__main__":
    # Test
    rec = TraceRecorder(system_name="test", enabled=True)
    rec.on_task_start("test_task_001", "write a Fibonacci function")
    rec.on_tool_result("file_write", {"path": "/tmp/fib.py", "content": "def fib(n):..."}, {"exit_code": 0}, True)
    rec.on_tool_result("bash", {"command": "python3 /tmp/fib.py"}, {"exit_code": 0, "stdout": "55"}, True)
    rec.on_task_end(True)
    print(f"Test sample written to: {_get_training_dir()}")
    for f in _get_training_dir().glob("*.jsonl"):
        print(f"  {f.name}")
