# EITElite -- AI Agent Platform
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

"""Trace System - observability module for EITElite.

Records structured trace events (LLM calls, tool executions, decisions, errors)
for debugging and observability. Thread-safe, writes JSONL log files to
~/.EITElite/traces/{trace_id}.jsonl.

Usage:
    from tical_code.core.trace import TraceLogger, TraceEvent

    logger = TraceLogger()
    trace_id = logger.new_trace_id()

    # Log an LLM call
    logger.log_event(TraceEvent(
        trace_id=trace_id,
        event_type="llm_call",
        provider="deepseek",
        latency_ms=234.5,
        input_summary="Write a function that...",
        output_summary="Here is the function...",
    ))

    # Log a tool execution
    logger.log_event(TraceEvent(
        trace_id=trace_id,
        event_type="tool_exec",
        latency_ms=12.3,
        input_summary="bash: ls -la",
        output_summary="exit_code=0, stdout=...",
    ))

    # Retrieve all events for a trace
    events = logger.get_trace(trace_id)
"""

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("EITElite.trace")


@dataclass
class TraceEvent:
    """A single trace event recorded during execution.

    Attributes:
        trace_id: Unique trace identifier (UUID4).
        timestamp: Unix timestamp with microsecond precision.
        event_type: One of llm_call, tool_exec, decision, error, llm_error, tool_error.
        provider: LLM provider name (e.g., deepseek, openai). Empty for non-LLM events.
        latency_ms: Wall-clock latency of the operation in milliseconds.
        input_summary: Truncated summary of input (prompt, args, etc.).
        output_summary: Truncated summary of output (response, result, etc.).
        metadata: Arbitrary additional key-value data.
    """
    trace_id: str = ""
    timestamp: float = field(default_factory=time.time)
    event_type: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    input_summary: str = ""
    output_summary: str = ""
    metadata: Dict = field(default_factory=dict)


class TraceLogger:
    """Thread-safe trace event logger with in-memory ring buffer and JSONL output.

    Events are appended to both an in-memory ring buffer (last N events) and
    a JSONL file on disk under ~/.EITElite/traces/{trace_id}.jsonl.

    Thread-safe: all operations that mutate shared state acquire a lock.
    JSONL writes happen outside the lock to avoid I/O contention.
    """

    _DEFAULT_OUTPUT_DIR = "~/.EITElite/traces"
    _DEFAULT_RING_SIZE = 100

    def __init__(self, output_dir: Optional[str] = None, ring_size: int = _DEFAULT_RING_SIZE):
        resolved = Path(output_dir) if output_dir else Path(os.path.expanduser(self._DEFAULT_OUTPUT_DIR))
        self._output_dir = resolved
        self._ring_size = ring_size
        self._buffer: deque = deque(maxlen=ring_size)
        self._lock = threading.Lock()

        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Directory creation may fail in restricted environments; tracing is best-effort.
            pass

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    def new_trace_id(self) -> str:
        """Generate a new unique trace identifier (UUID4)."""
        return str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Event logging
    # ------------------------------------------------------------------

    def log_event(self, event: TraceEvent) -> None:
        """Log a trace event.

        Appends to the in-memory ring buffer (thread-safe) and writes
        to the JSONL file on disk (best-effort, outside lock).
        """
        with self._lock:
            self._buffer.append(event)

        # Write to disk outside the lock to avoid blocking other threads on I/O.
        self._write_to_disk(event)

    def _write_to_disk(self, event: TraceEvent) -> None:
        """Append a single event as a JSON line to the trace file."""
        try:
            file_path = self._output_dir / f"{event.trace_id}.jsonl"
            event_dict = asdict(event)
            event_dict["timestamp"] = round(event_dict["timestamp"], 6)
            line = json.dumps(event_dict, ensure_ascii=True) + "\n"
            with open(file_path, "a") as f:
                f.write(line)
        except Exception as e:
            logger.warning("Failed to write trace event to disk: %s", e)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_trace(self, trace_id: str) -> List[TraceEvent]:
        """Return all events for a given trace ID (from the in-memory ring buffer).

        Note: only events still in the ring buffer are returned.
        For historical traces, read the JSONL file directly.
        """
        with self._lock:
            return [e for e in self._buffer if e.trace_id == trace_id]

    def get_recent_events(self, limit: Optional[int] = None) -> List[TraceEvent]:
        """Return the most recent events from the ring buffer.

        Args:
            limit: Maximum number of events to return (default: all in buffer).
        """
        with self._lock:
            items = list(self._buffer)
            if limit is not None:
                items = items[-limit:]
            return items

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Flush any pending writes.

        JSONL writes are synchronous (no buffering), so this is currently
        a no-op. Provided for API compatibility with future buffered modes.
        """
        pass

    def clear_buffer(self) -> None:
        """Clear the in-memory ring buffer (does not affect disk files)."""
        with self._lock:
            self._buffer.clear()

    @property
    def buffer_size(self) -> int:
        """Number of events currently in the ring buffer."""
        with self._lock:
            return len(self._buffer)
