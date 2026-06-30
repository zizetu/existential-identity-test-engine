# tical-code -- AI Agent Platform
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
TraceRecorder - EITE training data collector for evaluation traces.

Embedded in the EITE evaluation worker's tool execution loop, records
complete traces of each evaluation task. Does not interfere with execution
logic, acts as a 'recorder' only.

Outputs to training_data/eite_trace/ directory, format compatible with
the EITE data pipeline for benchmark-to-sample conversion.

Usage (called automatically by the evaluation worker):
  from tical_code.core.trace_recorder import TraceRecorder
  rec = TraceRecorder()
  rec.on_task_start(task_id, prompt, system_name)
  rec.on_tool_result(tool_name, args, result, verified)
  rec.on_task_end(success)
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

# --- Path: EITE_DATA_ROOT env var, or default to project root -----------------

def _get_training_dir() -> Path:
    """Training data directory, controlled by EITE_DATA_ROOT env var."""
    root = Path(os.getenv("EITE_DATA_ROOT",
               Path(__file__).resolve().parent.parent))
    return root / "training_data" / "eite_trace"


# --- Single task trace --------------------------------------------------------

class TaskTrace:
    """Record all tool calls for a single evaluation task from start to end."""

    def __init__(self, task_id: str, prompt: str, system: str):
        self.task_id = task_id
        self.prompt = prompt[:500]          # truncate to prevent bloat
        self.system = system
        self.tools: list = []
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.success = False

    def record_tool(self, name: str, args: dict, result: dict, verified: bool):
        """Record one tool call (called after execute() + eite verify())."""
        entry = {
            "tool": name,
            "args_safe": _sanitize_args(name, args),   # sanitize sensitive args
            "exit_code": result.get("exit_code", result.get("verified") is True),
            "eite_verified": verified,
            "timestamp": time.time(),
        }
        self.tools.append(entry)

    def finish(self, success: bool):
        self.success = success
        self.end_time = time.time()

    def to_sample(self) -> dict:
        """Convert to training sample format (compatible with EITE data pipeline)."""
        raw = f"{self.system}_{self.task_id}_{len(self.tools)}"
        return {
            "id": hashlib.sha256(raw.encode()).hexdigest()[:12],
            "instruction": f"[EITE Trace] {self.task_id}",
            "output": json.dumps({
                "tools": self.tools,
                "total_steps": len(self.tools),
                "success": self.success,
                "elapsed_s": round(
                    (self.end_time or time.time()) - self.start_time, 2,
                ),
            }, ensure_ascii=False),
            "system": self.system,
            "level": "TRACE",
            "task_id": self.task_id,
            "verified": self.success,
            "steps": len(self.tools),
            "source": "eite_trace",
            "timestamp": self.start_time,
        }


# --- Sensitive argument sanitization --------------------------------------------

_SENSITIVE_KEYS = {
    "token", "key", "password", "secret", "auth", "api_key",
    "private_key", "credential", "bearer", "authorization",
}


def _sanitize_args(tool_name: str, args: dict) -> dict:
    """Sanitize: replace sensitive field values, keep paths and commands."""
    safe = {}
    for k, v in args.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            safe[k] = "***"
        elif tool_name == "file_write" and k == "content":
            safe[k] = f"<{len(str(v))} bytes>"  # do not record file contents
        elif tool_name == "bash" and k == "command":
            safe[k] = str(v)[:200]                # truncate long commands
        else:
            safe[k] = str(v)[:200]
    return safe


# --- Collector (singleton) ----------------------------------------------------

class TraceRecorder:
    """
    Trace collector embedded in the EITE evaluation worker loop.

    Each evaluation worker instance holds one TraceRecorder.

    Records locally, auto-POSTs to target_url after batch_size samples
    accumulate. If the test endpoint is not ready, data stays safely local
    and is never lost.
    """

    def __init__(self, system_name: str = "eitelite", enabled: bool = True,
                 target_url: str = "", batch_size: int = 10):
        self.enabled = enabled
        self.system = system_name
        self.target_url = target_url
        self.batch_size = batch_size
        self._trace: Optional[TaskTrace] = None
        self._output_dir = _get_training_dir()
        self._pending_count = 0  # accumulated pending upload count
        if enabled:
            self._output_dir.mkdir(parents=True, exist_ok=True)

    # --- Three-phase hooks ----------------------------------------------------

    def on_task_start(self, task_id: str, prompt: str = ""):
        """Called when the evaluation worker starts processing a message."""
        if not self.enabled:
            return
        self._trace = TaskTrace(task_id, prompt, self.system)

    def on_tool_result(self, tool_name: str, args: dict, result: dict, verified: bool):
        """Called after each tool execution and verification."""
        if not self.enabled or self._trace is None:
            return
        self._trace.record_tool(tool_name, args, result, verified)

    def on_task_end(self, success: bool):
        """Called when the evaluation worker finishes one processing round."""
        if not self.enabled or self._trace is None:
            return
        self._trace.finish(success)
        sample = self._trace.to_sample()
        self._write(sample)
        self._pending_count += 1
        self._trace = None

        # batch_size reached -> try upload
        if self.target_url and self._pending_count >= self.batch_size:
            self.flush()

    # --- Write ---------------------------------------------------------------

    def _write(self, sample: dict):
        """Append to .jsonl file."""
        if not self.enabled:
            return
        date = time.strftime("%Y%m%d")
        path = self._output_dir / f"trace_{self.system}_{date}.jsonl"
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a",
        ) as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    # --- Batch upload ---------------------------------------------------------

    def flush(self):
        """
        Batch-POST locally cached trace data to target_url.

        Resets pending_count on success.
        On failure, data stays local for next flush() retry.
        """
        if not self.target_url or self._pending_count == 0:
            return

        samples = []
        try:
            # Read all unuploaded samples from today's file
            date = time.strftime("%Y%m%d")
            path = self._output_dir / f"trace_{self.system}_{date}.jsonl"
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
                    logging.getLogger("eite.trace").info(
                        "flushed %d traces to %s", len(samples), self.target_url,
                    )
        except Exception as e:
            # On any failure, keep pending_count for next retry
            logging.getLogger("eite.trace").warning(
                "flush failed (%d traces): %s",
                len(samples) if samples else 0, e,
            )


# --- Direct run: manual usage example -----------------------------------------

if __name__ == "__main__":
    rec = TraceRecorder(system_name="test", enabled=True)
    rec.on_task_start("test_task_001", "Write a Fibonacci function")
    rec.on_tool_result(
        "file_write",
        {"path": "/tmp/fib.py", "content": "def fib(n):..."},
        {"exit_code": 0}, True,
    )
    rec.on_tool_result(
        "bash",
        {"command": "python3 /tmp/fib.py"},
        {"exit_code": 0, "stdout": "55"}, True,
    )
    rec.on_task_end(True)
    print(f"Test samples written to: {_get_training_dir()}")
    for f in _get_training_dir().glob("*.jsonl"):
        print(f"  {f.name}")
