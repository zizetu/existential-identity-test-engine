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
# Original repository: https://github.com/zizetu/existential-identity-test-engine
#

"""
Priority instruction queue with TTL expiry for the Vigil guardian layer.

When the interrupt evaluator defers an instruction (e.g., non-urgent parallel
requests or redirects during near-complete tasks), it lands here. The queue
holds instructions in priority order and automatically expires stale entries
after a configurable TTL (default: 1 hour).

The queue is consumed by the main worker loop: when the AI finishes its
current task, it dequeues the next pending instruction. This ensures nothing
is lost but the human isn't disrupted mid-execution.

FEATURES:
    - Priority-ordered (lower number = higher priority).
    - TTL expiry with automatic cleanup.
    - Status tracking: pending → executing → (removed).
    - Size query and peek without dequeue.

Classes:
    QueuedInstruction - dataclass: a deferred instruction with metadata.
    InstructionQueue - the priority queue with TTL management.
"""
import time
from dataclasses import dataclass, field
from typing import List, Optional
from .interrupt_evaluator import NewInstruction, InterruptVerdict

@dataclass(order=True)
class QueuedInstruction:
    priority: int; queued_at: float = field(compare=False)
    instruction: NewInstruction = field(compare=False); verdict: InterruptVerdict = field(compare=False)
    status: str = field(compare=False, default="pending")

class InstructionQueue:
    _DEFAULT_TTL_SECONDS = 3600
    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS):
        self._items: List[QueuedInstruction] = []; self._ttl = ttl_seconds
    def enqueue(self, instruction, priority, verdict):
        item = QueuedInstruction(priority=priority, queued_at=time.time(), instruction=instruction, verdict=verdict, status="pending")
        self._items.append(item); self._items.sort(); return item
    def dequeue(self):
        self._purge_expired()
        for item in self._items:
            if item.status == "pending":
                item.status = "executing"; self._items.remove(item); return item
        return None
    def peek(self):
        self._purge_expired()
        for item in self._items:
            if item.status == "pending": return item
        return None
    def size(self):
        self._purge_expired(); return sum(1 for i in self._items if i.status == "pending")
    def cleanup_expired(self):
        now = time.time(); expired = [i for i in self._items if now - i.queued_at > self._ttl]
        for i in expired: self._items.remove(i)
        return expired
    def all_pending(self):
        return [i for i in self._items if i.status == "pending"]
    def _purge_expired(self):
        now = time.time(); self._items = [i for i in self._items if now - i.queued_at <= self._ttl]
