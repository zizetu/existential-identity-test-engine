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
Truthful Reporting Protocol - Truthful Reporting System
============================================

Prevent AI from misreporting/exaggerating/fabricating execution process.
Five core rules:
1. Mandatory source declaration - any "called external system" statement must attach actual tool call return code or output summary
2. Report only after verification - After executing an operation, must fetch actual output/return code; for side effects, must read-back to verify
3. Honest about capability boundaries - missing API key cannot claim "called", SSH unreachable cannot claim "normal"
4. Audit trail - every claimed external operation must have a corresponding log record (operation → result → report triple)
5. Violation handling - 3 false reports degrade trust; current report marked unverified; next time triggers re-verification

Author: Tical (Zize Tu)
Version: see tical_code.__version__
Spec: https://github.com/ticalzzt/tical-code-dev/blob/main/docs/truthful-reporting-spec.md
"""

import asyncio
import hashlib
import json
import logging
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class OperationResult:
    """
    Record the real evidence of an operation.

    Attributes:
        operation: Operation name (e.g. "ssh_oracle")
        success: Whether the operation succeeded
        output: Actual output (stdout / return value summary)
        return_code: Return code (e.g. HTTP status, exit code)
        error: Error message if failed
        duration: Execution duration in seconds
        timestamp: Unix timestamp when the operation completed
        evidence_hash: SHA256 hash of the output to prevent tampering
    """
    operation: str
    success: bool
    output: Optional[str] = None
    return_code: Optional[int] = None
    error: Optional[str] = None
    duration: float = 0.0
    timestamp: float = field(default_factory=time.time)
    evidence_hash: str = ""

    def __post_init__(self):
        """Compute evidence hash after initialization."""
        if not self.evidence_hash:
            self.evidence_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """Compute SHA256 hash of output/error for tamper detection."""
        content = f"{self.operation}:{self.output or ''}:{self.error or ''}:{self.return_code}"
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def to_dict(self) -> Dict:
        """Serialize to dictionary."""
        return {
            'operation': self.operation,
            'success': self.success,
            'output': self.output,
            'return_code': self.return_code,
            'error': self.error,
            'duration': self.duration,
            'timestamp': self.timestamp,
            'evidence_hash': self.evidence_hash,
        }


class TrustLevel(Enum):
    """Trust level for the AI reporter."""
    FULL = "full"           # Fully trusted
    REDUCED = "reduced"     # Reduced trust (1-2 false reports)
    UNTRUSTED = "untrusted" # Not trusted (3+ false reports, requires human approval)


# =============================================================================
# TruthReporter - Truthful Reporting Engine
# =============================================================================

class TruthReporter:
    """
    Truthful Reporting Engine.

    Enforces that every external claim by the AI is backed by real evidence.
    Tracks trust level and triggers human approval when trust is degraded.

    Usage:
        reporter = TruthReporter()

        # Execute an operation through the reporter
        result = await reporter.track("ssh_oracle", ssh_command())

        # Generate a verified report
        report = reporter.report("Oracle node check", result)

        # If the AI was caught lying
        await reporter.corrected("previously claimed Oracle was normal but SSH was unreachable", "SSH connection refused")
    """

    # Trust degradation thresholds
    REDUCED_THRESHOLD = 1    # 1 false report degrades to REDUCED
    UNTRUSTED_THRESHOLD = 3  # 3 false reports degrade to UNTRUSTED

    # Trust status filename (stored outside the repo)
    TRUST_STATE_FILENAME = ".tical_trust.json"

    # Capability check cache TTL (seconds)
    CAPABILITY_CACHE_TTL = 300  # 5 minutes

    def __init__(self, audit_log_dir: Optional[str] = None):
        """
        Initialize the TruthReporter.

        Args:
            audit_log_dir: Directory for audit logs. If None, uses the same
                           logic as SelfRepairEngine._get_audit_log_path().
        """
        # Audit log directory
        self._audit_log_dir = audit_log_dir or self._default_audit_log_dir()
        self._audit_log_path = os.path.join(self._audit_log_dir, 'truth_audit.jsonl')

        # Ensure audit log directory exists
        os.makedirs(self._audit_log_dir, exist_ok=True)

        # Trust tracker: {operation: {"count": int, "last_corrected": float}}
        self._correction_tracker: Dict[str, Dict] = {}

        # Total false-report count
        self._total_corrections: int = 0

        # Capability check cache: {capability: {"available": bool, "checked_at": float}}
        self._capability_cache: Dict[str, Dict] = {}

        # Recent operation results (used for verify_before_report)
        self._recent_results: Dict[str, OperationResult] = {}

        # Persisted load
        saved = self._load_trust_state()
        self._correction_tracker = saved.get('correction_tracker', {})
        self._total_corrections = saved.get('total_corrections', 0)

    # =========================================================================
    # Core: track - Execute operation and record real result
    # =========================================================================

    async def track(self, operation: str, coro: Awaitable) -> OperationResult:
        """
        Execute an operation and record the real result.

        Every operation that the AI claims to have performed MUST go through
        this method. It captures the actual output, return code, and timing,
        ensuring no claim can be made without evidence.

        Args:
            operation: A descriptive name for the operation (e.g. "ssh_oracle")
            coro: An awaitable (coroutine) to execute

        Returns:
            OperationResult with real evidence attached
        """
        start = time.time()
        try:
            output = await coro
            duration = time.time() - start

            # Attempt to extract return code (if output is dict or has return_code attribute)
            return_code = None
            output_str = None
            if isinstance(output, dict):
                return_code = output.get('return_code') or output.get('returncode') or output.get('status_code')
                output_str = json.dumps(output, ensure_ascii=False, default=str)[:2000]
            elif isinstance(output, str):
                output_str = output[:2000]
            elif hasattr(output, 'returncode'):
                return_code = output.returncode
                output_str = str(getattr(output, 'stdout', ''))[:2000]
            else:
                output_str = str(output)[:2000]

            result = OperationResult(
                operation=operation,
                success=True,
                output=output_str,
                return_code=return_code,
                duration=duration,
            )

            # Record recent result
            self._recent_results[operation] = result

            # Write audit log
            self._write_audit_log("track", result)

            logger.info(
                f"[TruthReporter] Operation '{operation}' succeeded "
                f"(duration={duration:.2f}s, rc={return_code})"
            )
            return result

        except Exception as e:
            duration = time.time() - start
            result = OperationResult(
                operation=operation,
                success=False,
                error=str(e)[:2000],
                duration=duration,
            )

            self._recent_results[operation] = result
            self._write_audit_log("track", result)

            logger.warning(
                f"[TruthReporter] Operation '{operation}' failed: {e} "
                f"(duration={duration:.2f}s)"
            )
            return result

    # =========================================================================
    # Core: report - Generate report with real evidence attached
    # =========================================================================

    def report(self, claim: str, result: OperationResult) -> Dict:
        """
        Generate a report with real evidence attached.

        The AI's response MUST include the evidence from this report.
        Reports without matching evidence are marked as unverified.

        Args:
            claim: The claim being made (e.g. "Oracle node check complete")
            result: The OperationResult from track()

        Returns:
            Report dict with claim, evidence, and verification status
        """
        trust_level = self.get_trust_level()
        verified = result.success and trust_level != TrustLevel.UNTRUSTED

        report_data = {
            "claim": claim,
            "actual_success": result.success,
            "evidence": result.output or result.error or "(no output)",
            "return_code": result.return_code,
            "evidence_hash": result.evidence_hash,
            "verified": verified,
            "trust_level": trust_level.value,
            "timestamp": time.time(),
        }

        # Write audit log - operation → result → report triple
        self._write_audit_report_log(claim, result, report_data)

        if not verified:
            logger.warning(
                f"[TruthReporter] Report UNVERIFIED: '{claim}' "
                f"(trust={trust_level.value}, success={result.success})"
            )

        return report_data

    # =========================================================================
    # Report only after verification
    # =========================================================================

    def verify_before_report(self, operation: str, expected_success: bool = True) -> bool:
        """
        Verify an operation actually succeeded before reporting.

        Checks the most recent OperationResult for the given operation
        against the expected success status.

        Args:
            operation: The operation name to verify
            expected_success: What success status we expect

        Returns:
            True if the operation result matches expectation
        """
        result = self._recent_results.get(operation)
        if result is None:
            logger.warning(
                f"[TruthReporter] verify_before_report: no result for '{operation}'"
            )
            return False

        matches = result.success == expected_success
        if not matches:
            logger.warning(
                f"[TruthReporter] Verification mismatch for '{operation}': "
                f"expected success={expected_success}, actual={result.success}"
            )
        return matches

    # =========================================================================
    # Capability boundary checks
    # =========================================================================

    def check_capability(self, capability: str) -> bool:
        """
        Check if a capability is actually available.

        The AI MUST NOT claim to have used a capability that is not available.
        For example: no Grok API key → cannot say "called Grok for review".

        Uses a cache to avoid repeated expensive checks.

        Args:
            capability: The capability to check (e.g. "grok_api", "ssh_oracle")

        Returns:
            True if the capability is available
        """
        now = time.time()

        # Check cache
        cached = self._capability_cache.get(capability)
        if cached and (now - cached.get('checked_at', 0)) < self.CAPABILITY_CACHE_TTL:
            return cached['available']

        # Actually check capability
        available = self._do_capability_check(capability)

        # Update cache
        self._capability_cache[capability] = {
            'available': available,
            'checked_at': now,
        }

        # Write audit log
        self._write_audit_log("capability_check", OperationResult(
            operation=f"check:{capability}",
            success=available,
            output=f"Capability '{capability}' {'available' if available else 'unavailable'}",
        ))

        if not available:
            logger.info(
                f"[TruthReporter] Capability '{capability}' is NOT available"
            )

        return available

    def _do_capability_check(self, capability: str) -> bool:
        """
        Perform the actual capability check.

        Args:
            capability: The capability to check

        Returns:
            True if available
        """
        # API key check - common external services
        api_key_map = {
            'grok_api': 'XAI_API_KEY',
            'gemini_api': 'GEMINI_API_KEY',
            'openai_api': 'OPENAI_API_KEY',
            'deepseek_api': 'DEEPSEEK_API_KEY',
            'anthropic_api': 'ANTHROPIC_API_KEY',
        }

        if capability in api_key_map:
            env_var = api_key_map[capability]
            return bool(os.environ.get(env_var, '').strip())

        # SSH connectivity check - format: ssh_<host_pattern>
        if capability.startswith('ssh_'):
            host_key = capability[4:]  # remove ssh_ prefix
            return self._check_ssh_capability(host_key)

        # Network connectivity check - format: network_<host>
        if capability.startswith('network_'):
            host = capability[8:]
            return self._check_network_capability(host)

        # Git check
        if capability == 'git':
            import subprocess
            try:
                result = subprocess.run(
                    ['git', '--version'],
                    capture_output=True, text=True, timeout=5
                )
                return result.returncode == 0
            except Exception as e:
                logger.debug(f"[TruthfulReporting] Unknown exception (non-blocking): {e}")
                return False

        # Unknown capability - conservative: return False
        logger.info(f"[TruthReporter] Unknown capability '{capability}', assuming unavailable")
        return False

    def _check_ssh_capability(self, host_key: str) -> bool:
        """Check SSH connectivity to a host."""
        import socket
        # Get host config from environment variables
        host = os.environ.get(f'SSH_HOST_{host_key.upper()}', '')
        port = int(os.environ.get(f'SSH_PORT_{host_key.upper()}', '22'))

        if not host:
            return False

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception as e:
            logger.debug(f"[TruthfulReporting] _check_ssh_capability exception (non-blocking): {e}")
            return False

    def _check_network_capability(self, host: str) -> bool:
        """Check basic network connectivity to a host."""
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            result = sock.connect_ex((host, 443))
            sock.close()
            return result == 0
        except Exception as e:
            logger.debug(f"[TruthfulReporting] _check_network_capability exception (non-blocking): {e}")
            return False

    # =========================================================================
    # False report correction
    # =========================================================================

    async def corrected(self, false_claim: str, reality: str):
        """
        Called when the AI is caught making a false report.

        Records the correction, degrades trust level, and marks the
        operation as needing re-verification on next startup.

        Args:
            false_claim: What the AI falsely claimed
            reality: What actually happened
        """
        self._total_corrections += 1

        # Record to tracker
        tracker_key = self._extract_operation_from_claim(false_claim)
        if tracker_key not in self._correction_tracker:
            self._correction_tracker[tracker_key] = {
                'count': 0,
                'last_corrected': 0.0,
                'claims': [],
            }

        self._correction_tracker[tracker_key]['count'] += 1
        self._correction_tracker[tracker_key]['last_corrected'] = time.time()
        self._correction_tracker[tracker_key]['claims'].append({
            'false_claim': false_claim[:500],
            'reality': reality[:500],
            'timestamp': time.time(),
        })

        # Only retain recent 10 correction records
        if len(self._correction_tracker[tracker_key]['claims']) > 10:
            self._correction_tracker[tracker_key]['claims'] = \
                self._correction_tracker[tracker_key]['claims'][-10:]

        # Persist
        self._save_trust_state()

        # Audit log
        self._write_audit_log("correction", OperationResult(
            operation=f"corrected:{tracker_key}",
            success=False,
            error=f"False claim: {false_claim[:200]} | Reality: {reality[:200]}",
        ))

        trust = self.get_trust_level()
        logger.warning(
            f"[TruthReporter] FALSE CLAIM CORRECTED: '{false_claim[:100]}' → "
            f"Reality: '{reality[:100]}' | Total corrections: {self._total_corrections} | "
            f"Trust level: {trust.value}"
        )

    def _extract_operation_from_claim(self, claim: str) -> str:
        """Extract operation keyword from claim as tracker key."""
        # Simple strategy: take first 30 non-empty characters as key
        clean = claim.strip().replace(' ', '_')[:30]
        return clean or "unknown_operation"

    # =========================================================================
    # Cross-Verify - Cross-Model Verification
    # =========================================================================

    # Verification prompt template
    _VERIFY_PROMPT_TEMPLATE = (
        "You are a verification agent. Your job is to determine if a task was actually completed, "
        "not just claimed to be completed.\n\n"
        "TASK: {task_description}\n\n"
        "CLAIMED OUTPUT:\n{output}\n\n"
        "Verify:\n"
        "1. Does the output actually fulfill the task requirements?\n"
        "2. Are there any signs of hallucination (vague claims without evidence, "
        "impossible results, contradictory statements)?\n"
        "3. Are there any missing parts that were requested but not delivered?\n\n"
        "Respond in JSON:\n"
        '{{"verified": true/false, "issues_found": [...], "confidence": 0.0-1.0, "reasoning": "..."}}'
    )

    # Supported verifier model configs - OpenAI-compatible API format
    _VERIFIER_CONFIGS = {
        'gemini': {
            'api_key_env': 'GEMINI_API_KEY',
            'url_env': 'GEMINI_API_URL',
            'default_url': os.environ.get('GEMINI_API_URL', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
            'model_env': 'GEMINI_VERIFIER_MODEL',
            'model': os.environ.get('GEMINI_VERIFIER_MODEL', 'gemini-2.0-flash'),
            'priority': 1,
        },
        'deepseek': {
            'api_key_env': 'DEEPSEEK_API_KEY',
            'url_env': 'DEEPSEEK_API_URL',
            'default_url': os.environ.get('DEEPSEEK_API_URL', 'https://api.deepseek.com/chat/completions'),
            'model_env': 'DEEPSEEK_VERIFIER_MODEL',
            'model': os.environ.get('DEEPSEEK_VERIFIER_MODEL', 'deepseek-chat'),
            'priority': 2,
        },
        'openai': {
            'api_key_env': 'OPENAI_API_KEY',
            'url_env': 'OPENAI_API_URL',
            'default_url': os.environ.get('OPENAI_API_URL', 'https://api.openai.com/v1/chat/completions'),
            'model_env': 'OPENAI_VERIFIER_MODEL',
            'model': os.environ.get('OPENAI_VERIFIER_MODEL', 'gpt-4o-mini'),
            'priority': 3,
        },
    }

    # API call timeout (seconds)
    _VERIFY_TIMEOUT = 30

    async def cross_verify(
        self,
        task_description: str,
        output: str,
        model: Optional[str] = None,
        executor_model: Optional[str] = None,
    ) -> Dict:
        """
        Cross-model verification - send the output and task description to a
        DIFFERENT model and ask "was this actually completed?"

        The verifier and executor MUST be isolated:
        - Executor: whatever model produced the output (DeepSeek/GPT/etc.)
        - Verifier: a different model, given only task + output, zero chat history

        Args:
            task_description: Original task description (brief, no chat history)
            output: The produced output (code / report / command output etc.)
            model: Specify which verifier model to use (None = auto-select)
            executor_model: The model that produced the output (used to avoid
                            picking the same model as verifier)

        Returns:
            {
                "verified": bool | None,    # None if verification itself failed
                "verifier_model": str,      # Which model was used
                "verifier_response": str,   # Verifier's full response
                "issues_found": [str],      # Issues discovered
                "confidence": float,        # Verification confidence 0-1
                "error": str | None,        # Error if verification failed
            }
        """
        # 1. Select verifier model
        verifier_info = self._get_available_verifier(
            preferred=model,
            executor_model=executor_model,
        )

        if verifier_info is None:
            # No external model available, fallback to local self-verify
            logger.info(
                "[TruthReporter] No external verifier available, "
                "falling back to self-verify (reduced confidence)"
            )
            return await self._self_verify(task_description, output)

        verifier_name = verifier_info['name']
        api_key = verifier_info['api_key']
        api_url = verifier_info['url']
        verifier_model = verifier_info['model']

        logger.info(
            f"[TruthReporter] Cross-verifying with {verifier_name}/{verifier_model}"
        )

        # 2. Construct verification prompt
        prompt = self._VERIFY_PROMPT_TEMPLATE.format(
            task_description=task_description[:2000],
            output=output[:6000],  # Truncate to avoid exceeding context
        )

        # 3. Call verifier API
        start = time.time()
        try:
            response_text = await self._call_verifier_api(
                api_url=api_url,
                api_key=api_key,
                model=verifier_model,
                prompt=prompt,
            )
            duration = time.time() - start

        except Exception as e:
            duration = time.time() - start
            error_msg = f"Verifier API call failed ({verifier_name}): {e}"
            logger.warning(f"[TruthReporter] {error_msg}")

            # Audit log
            self._write_audit_log("cross_verify", OperationResult(
                operation=f"cross_verify:{verifier_name}",
                success=False,
                error=error_msg,
                duration=duration,
            ))

            return {
                "verified": None,
                "verifier_model": f"{verifier_name}/{verifier_model}",
                "verifier_response": "",
                "issues_found": [],
                "confidence": 0.0,
                "error": error_msg,
            }

        # 4. Parse verification result
        parsed = self._parse_verifier_response(response_text)

        result = {
            "verified": parsed.get('verified'),
            "verifier_model": f"{verifier_name}/{verifier_model}",
            "verifier_response": response_text[:2000],
            "issues_found": parsed.get('issues_found', []),
            "confidence": parsed.get('confidence', 0.5),
            "error": None,
        }

        # 5. Audit log
        self._write_audit_log("cross_verify", OperationResult(
            operation=f"cross_verify:{verifier_name}",
            success=result["verified"] is not False,
            output=json.dumps(result, ensure_ascii=False)[:500],
            duration=duration,
        ))

        # Extra: write audit log with verifier details
        self._write_cross_verify_audit(result, task_description[:200], duration)

        logger.info(
            f"[TruthReporter] Cross-verify result: verified={result['verified']}, "
            f"confidence={result['confidence']:.2f}, "
            f"issues={len(result['issues_found'])}, "
            f"verifier={verifier_name}/{verifier_model}, "
            f"duration={duration:.2f}s"
        )

        return result

    def _get_available_verifier(
        self,
        preferred: Optional[str] = None,
        executor_model: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Select an available verifier model, preferring one different from the executor.

        Model selection strategy:
        1. If preferred is specified and available, use it (unless same as executor)
        2. Otherwise, pick the highest-priority available model different from executor
        3. Returns None if no external model is available

        Args:
            preferred: Preferred verifier name (e.g. "gemini")
            executor_model: Name of the model that produced the output

        Returns:
            Dict with name, api_key, url, model - or None
        """
        # Exclude the same model as the executor
        exclude = set()
        if executor_model:
            exclude.add(executor_model.lower())

        # Check specified preference
        if preferred:
            pref_lower = preferred.lower()
            configs = {k: v for k, v in self._VERIFIER_CONFIGS.items()
                      if k == pref_lower}
        else:
            configs = self._VERIFIER_CONFIGS

        # Sort by priority
        sorted_configs = sorted(configs.items(), key=lambda x: x[1]['priority'])

        for name, cfg in sorted_configs:
            # Skip if same name as executor model
            if name.lower() in exclude and len(sorted_configs) > 1:
                continue

            api_key = os.environ.get(cfg['api_key_env'], '').strip()
            if not api_key:
                continue

            # Get API URL (allow env var override)
            api_url = os.environ.get(cfg['url_env'], '').strip() or cfg['default_url']

            return {
                'name': name,
                'api_key': api_key,
                'url': api_url,
                'model': cfg['model'],
            }

        return None

    async def _call_verifier_api(
        self,
        api_url: str,
        api_key: str,
        model: str,
        prompt: str,
    ) -> str:
        """
        Call the verifier model's API using urllib (zero new dependencies).

        Uses the OpenAI-compatible chat completions format which is supported
        by Gemini, DeepSeek, and OpenAI.

        Args:
            api_url: The chat completions endpoint URL
            api_key: API key for authentication
            model: Model name to use
            prompt: The verification prompt

        Returns:
            The model's response text

        Raises:
            Exception on API errors
        """
        # Construct OpenAI-compatible request body
        request_body = json.dumps({
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0,  # Use minimum temperature for verification to ensure determinism
            "max_tokens": 1024,
        }).encode('utf-8')

        # SSRF defense-in-depth: validate URL before connecting
        from tical_code.core.security_baseline import _check_ssrf as _ssrf_check
        _ssrf_check(api_url)

        # Construct request
        req = urllib.request.Request(
            api_url,
            data=request_body,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
            },
            method='POST',
        )

        # Execute request (in executor to avoid blocking event loop)
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: urllib.request.urlopen(req, timeout=self._VERIFY_TIMEOUT),
            )
            response_data = json.loads(response.read().decode('utf-8'))

            # Extract reply text (OpenAI-compatible format)
            choices = response_data.get('choices', [])
            if choices:
                message = choices[0].get('message', {})
                return message.get('content', '')

            return json.dumps(response_data, ensure_ascii=False)[:2000]

        except urllib.error.HTTPError as e:
            error_body = ''
            try:
                error_body = e.read().decode('utf-8')[:500]
            except Exception as e:
                logger.debug(f"[TruthfulReporting] Unknown exception (non-blocking): {e}")
                pass
            raise Exception(
                f"HTTP {e.code}: {error_body or e.reason}"
            ) from e

        except urllib.error.URLError as e:
            raise Exception(f"URL error: {e.reason}") from e

        except Exception as e:
            raise Exception(f"API call failed: {e}") from e

    def _parse_verifier_response(self, response_text: str) -> Dict:
        """
        Parse the verifier's JSON response.

        Tries multiple extraction strategies:
        1. Direct JSON parse
        2. Extract JSON from markdown code blocks
        3. Fallback: heuristic extraction

        Args:
            response_text: Raw response from the verifier model

        Returns:
            Parsed dict with verified, issues_found, confidence, reasoning
        """
        # Strategy 1: Direct parse
        try:
            return json.loads(response_text)
        except (json.JSONDecodeError, TypeError):
            logger.debug("truthful_reporting: direct JSON parse failed, trying code block extraction")

        # Strategy 2: Extract from markdown code blocks
        import re
        json_patterns = [
            r'```json\s*\n(.*?)\n```',    # ```json ... ```
            r'```\s*\n(.*?)\n```',          # ``` ... ```
            r'\{[^{}]*"verified"[^{}]*\}',  # Bare JSON object
        ]
        for pattern in json_patterns:
            matches = re.findall(pattern, response_text, re.DOTALL)
            for match in matches:
                try:
                    return json.loads(match)
                except (json.JSONDecodeError, TypeError):
                    continue

        # Strategy 3: Heuristic extraction
        result = {
            'verified': None,
            'issues_found': [],
            'confidence': 0.3,  # Low confidence when cannot parse
            'reasoning': 'Could not parse verifier response as JSON',
        }

        text_lower = response_text.lower()
        if '"verified": true' in text_lower or '"verified":true' in text_lower:
            result['verified'] = True
            result['confidence'] = 0.5
        elif '"verified": false' in text_lower or '"verified":false' in text_lower:
            result['verified'] = False
            result['confidence'] = 0.5

        return result

    async def _self_verify(self, task_description: str, output: str) -> Dict:
        """
        Fallback self-verification when no external model is available.

        Performs basic heuristic checks:
        - Non-empty output
        - No obvious hallucination markers
        - Output length is reasonable

        Marked as 'self-verify' with reduced confidence.

        Args:
            task_description: Original task description
            output: The produced output

        Returns:
            Verification result dict (confidence capped at 0.4)
        """
        issues = []

        # Check 1: Is output empty?
        if not output or not output.strip():
            issues.append("Output is empty")
            return {
                "verified": False,
                "verifier_model": "self-verify",
                "verifier_response": "Self-verification: output is empty",
                "issues_found": issues,
                "confidence": 0.2,
                "error": None,
            }

        # Check 2: Common hallucination markers
        hallucination_markers = [
            "i have successfully",  # Overly confident declaration
            "as you can see",       # Reference to non-existent evidence
            "the output clearly shows",  # Unspecified evidence
        ]
        output_lower = output.lower()
        for marker in hallucination_markers:
            if marker in output_lower:
                issues.append(f"Possible hallucination marker: '{marker}'")

        # Check 3: Output length reasonableness
        if len(output) < 20:
            issues.append("Output suspiciously short for the described task")

        verified = len(issues) == 0
        confidence = 0.4 if verified else 0.2  # Self-verification confidence ceiling is 0.4

        result = {
            "verified": verified,
            "verifier_model": "self-verify",
            "verifier_response": f"Self-verification: {'passed' if verified else 'issues found'}",
            "issues_found": issues,
            "confidence": confidence,
            "error": None,
        }

        # Audit log
        self._write_audit_log("cross_verify", OperationResult(
            operation="cross_verify:self-verify",
            success=verified,
            output=json.dumps(result, ensure_ascii=False)[:500],
        ))

        logger.info(
            f"[TruthReporter] Self-verify result: verified={verified}, "
            f"confidence={confidence}, issues={len(issues)}"
        )

        return result

    def _write_cross_verify_audit(self, result: Dict, task_summary: str, duration: float):
        """
        Write a detailed cross-verification audit entry.

        Args:
            result: The cross_verify result dict
            task_summary: Brief summary of the task
            duration: Duration of the verification call
        """
        try:
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "action": "cross_verify_detail",
                "task_summary": task_summary,
                "verifier_model": result.get('verifier_model', 'unknown'),
                "verified": result.get('verified'),
                "confidence": result.get('confidence', 0.0),
                "issues_found": result.get('issues_found', []),
                "duration": round(duration, 3),
            }
            with open(self._audit_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.warning(f"[TruthReporter] Failed to write cross-verify audit: {e}")

    # =========================================================================
    # Trust level
    # =========================================================================

    def get_trust_level(self) -> TrustLevel:
        """
        Get current trust level based on correction count.

        Returns:
            TrustLevel enum value
        """
        if self._total_corrections >= self.UNTRUSTED_THRESHOLD:
            return TrustLevel.UNTRUSTED
        elif self._total_corrections >= self.REDUCED_THRESHOLD:
            return TrustLevel.REDUCED
        else:
            return TrustLevel.FULL

    def require_human_approval(self) -> bool:
        """
        Check if human approval is required for important operations.

        Returns:
            True if trust is degraded to UNTRUSTED level
        """
        return self.get_trust_level() == TrustLevel.UNTRUSTED

    # =========================================================================
    # Persistence
    # =========================================================================

    def _default_audit_log_dir(self) -> str:
        """Default audit log directory - same logic as SelfRepairEngine, stored outside the repo."""
        log_dirs = ['/var/log/tical', os.path.expanduser('~/.tical/logs')]
        for d in log_dirs:
            try:
                os.makedirs(d, exist_ok=True)
                test_file = os.path.join(d, '.test')
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                return d
            except (PermissionError, OSError):
                continue
        # Last fallback
        return os.path.expanduser('~/.tical-code/truth_logs')

    def _load_trust_state(self) -> Dict:
        """
        Load trust state from .tical_trust.json.

        The file is stored OUTSIDE the repo to prevent AI tampering.

        Returns:
            Trust state dict
        """
        state_path = os.path.join(self._audit_log_dir, self.TRUST_STATE_FILENAME)
        try:
            if os.path.exists(state_path):
                with open(state_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data
        except Exception as e:
            logger.warning(f"[TruthReporter] Failed to load trust state: {e}")
        return {}

    def _save_trust_state(self):
        """
        Persist trust state to .tical_trust.json.

        The file is stored OUTSIDE the repo and is in PROTECTED_FILES
        to prevent AI from tampering with its own trust status.
        """
        state_path = os.path.join(self._audit_log_dir, self.TRUST_STATE_FILENAME)
        try:
            data = {
                'correction_tracker': self._correction_tracker,
                'total_corrections': self._total_corrections,
                'trust_level': self.get_trust_level().value,
                'updated': datetime.now().isoformat(),
            }
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[TruthReporter] Failed to save trust state: {e}")

    # =========================================================================
    # Audit log writing
    # =========================================================================

    def _write_audit_log(self, action: str, result: OperationResult):
        """
        Write an audit log entry (append-only JSONL).

        Args:
            action: The action type (track / capability_check / correction)
            result: The OperationResult to log
        """
        try:
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "operation": result.operation,
                "success": result.success,
                "output": (result.output or "")[:500],
                "return_code": result.return_code,
                "error": (result.error or "")[:500],
                "duration": round(result.duration, 3),
                "evidence_hash": result.evidence_hash,
            }
            with open(self._audit_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.warning(f"[TruthReporter] Failed to write audit log: {e}")

    def _write_audit_report_log(self, claim: str, result: OperationResult, report_data: Dict):
        """
        Write a complete audit trail: operation → result → report (triple).

        Args:
            claim: The claim being reported
            result: The OperationResult
            report_data: The full report dict
        """
        try:
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "action": "report",
                # Operation
                "operation": result.operation,
                # Result
                "result_success": result.success,
                "result_evidence": (result.output or result.error or "")[:500],
                "result_return_code": result.return_code,
                "result_evidence_hash": result.evidence_hash,
                # Report
                "claim": claim[:500],
                "report_verified": report_data.get('verified', False),
                "trust_level": report_data.get('trust_level', 'unknown'),
            }
            with open(self._audit_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.warning(f"[TruthReporter] Failed to write report audit log: {e}")

    # =========================================================================
    # Helper methods
    # =========================================================================

    def get_stats(self) -> Dict:
        """
        Get current statistics about the truthful reporting system.

        Returns:
            Stats dict
        """
        # Check available verifiers
        available_verifiers = []
        for name, cfg in self._VERIFIER_CONFIGS.items():
            api_key = os.environ.get(cfg['api_key_env'], '').strip()
            if api_key:
                available_verifiers.append(name)

        return {
            'total_corrections': self._total_corrections,
            'trust_level': self.get_trust_level().value,
            'require_human_approval': self.require_human_approval(),
            'corrected_operations': list(self._correction_tracker.keys()),
            'recent_operations': list(self._recent_results.keys()),
            'cached_capabilities': list(self._capability_cache.keys()),
            'available_verifiers': available_verifiers,
        }

    def invalidate_capability_cache(self, capability: Optional[str] = None):
        """
        Invalidate the capability cache.

        Args:
            capability: Specific capability to invalidate, or None for all
        """
        if capability:
            self._capability_cache.pop(capability, None)
        else:
            self._capability_cache.clear()
