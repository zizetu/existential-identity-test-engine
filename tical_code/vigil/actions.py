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

"""
Guardian actions executor - the effector layer of the Vigil immune system.

This module translates VigilVerdict objects into concrete, observable actions.
It is the "muscle" of the guardian - the judge decides what to do, and this
module does it. Actions range from silent no-ops to multi-stage emergency
escalations.

ACTION MAPPING:

    protect          - No-op. The human is in a protected flow state.
    notify           - Queues a low-priority notification.
    prompt           - Sends a prompt message via the configured sender.
    interrupt        - Sends a check-in message ("Are you OK?") to break
                       the human's flow and request acknowledgment.
    alert_emergency  - Sends an emergency message AND starts an escalation
                       loop that retries up to 3 times at 5-minute intervals.
                       On the 4th attempt, sends email to configured emergency
                       contacts via SMTP.
"""
import asyncio, smtplib, time
from email.mime.text import MIMEText
from typing import Callable, Awaitable, Optional
from .vigil_judge import VigilVerdict
from .vigil_config import VigilCoreConfig

MessageSender = Callable[[str], Awaitable[None]]

class VigilActions:
    def __init__(self, config=None, send_message=None, smtp_config=None):
        self._cfg = config or VigilCoreConfig()
        self._send = send_message or self._noop_sender
        self._smtp = smtp_config or {}
        self._pending_ack_traces = {}
    async def execute(self, verdict, trace_id=""):
        action = verdict.action
        if action == "protect": return
        elif action == "notify": self._queue_notification(verdict.reason)
        elif action == "prompt": await self._send(self._format_prompt(verdict))
        elif action == "interrupt": await self._send(f" {self._cfg.prompt_messages.get('check_in', 'Are you OK?')}")
        elif action == "alert_emergency": await self._handle_emergency(verdict, trace_id)
    async def handle_ack(self, trace_id): self._pending_ack_traces.pop(trace_id, None)
    async def _handle_emergency(self, verdict, trace_id):
        msg = self._cfg.prompt_messages.get("emergency", "Abnormal state detected. Confirm safety.")
        await self._send(f" {msg}")
        self._pending_ack_traces[trace_id] = time.time()
        asyncio.create_task(self._escalation_loop(verdict, trace_id))
    async def _escalation_loop(self, verdict, trace_id):
        for attempt in range(1, 4):
            await asyncio.sleep(300)
            if trace_id not in self._pending_ack_traces: return
            await self._send(f"Emergency alert (attempt {attempt+1}/3)")
            if attempt == 3 and self._smtp: self._send_email(verdict)
    async def _noop_sender(self, text): pass
    def _queue_notification(self, msg): pass
    def _format_prompt(self, verdict): return verdict.reason
    def _send_email(self, verdict):
        try:
            cfg = self._smtp
            msg = MIMEText(f"Guardian alert: {verdict.reason}\nEvidence: {verdict.evidence}")
            msg["Subject"] = "Guardian Emergency Alert"; msg["From"] = cfg.get("from", "")
            for contact in self._cfg.emergency_contacts:
                msg["To"] = contact.get("email", "")
                with smtplib.SMTP(cfg.get("host", ""), cfg.get("port", 587)) as s:
                    s.starttls(); s.login(cfg.get("user", ""), cfg.get("password", ""));
                    s.send_message(msg)
        except Exception: pass
