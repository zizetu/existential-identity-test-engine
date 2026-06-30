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

# provenance:ticalasi-zzt-2026​
"""
EITE Sandbox - Evaluation Safety Filter
=========================================

Security-first evaluation sandbox for validating model outputs and test code.
EITE uses this to ensure model-generated code and responses are safe before
scoring or executing them.

EITE evaluation context:
- Validates model outputs for dangerous content (code injection, system commands)
- Sanitizes model responses before they are presented to the scoring system
- Does NOT execute arbitrary user code (EITE evaluates, it does not run agents)
- Provides content safety checks for evaluation test cases
- Filters personally identifiable information (PII) from evaluation results

Architecture:
    validate_output_safety() - checks model output for dangerous patterns.
    sanitize_output() - strips/redacts sensitive content from output.
    validate_test_code() - checks benchmark test code for safety.
    EvalSanitizer - unified entry point for output sanitization.

Author: EITE Team
"""
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("eite-agent.sandbox")


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class SafetyResult:
    """Result of a safety validation check.

    Attributes:
        is_safe: Whether the content passed all checks.
        issues: List of issues found (empty if safe).
        redacted_text: The potentially redacted/cleaned text.
    """
    is_safe: bool = True
    issues: List[str] = field(default_factory=list)
    redacted_text: str = ""


# =============================================================================
# Forbidden content patterns (evaluation context)
# =============================================================================

# Patterns that are dangerous in model outputs during evaluation.
# These block code injection, system manipulation, and data exfiltration.
OUTPUT_DANGEROUS_PATTERNS: List[Tuple[str, str]] = [
    (r'\b__import__\b', '__import__ call - code injection risk'),
    (r'\beval\s*\(', 'eval() call - code injection risk'),
    (r'\bexec\s*\(', 'exec() call - code injection risk'),
    (r'\bcompile\s*\(', 'compile() call - code injection risk'),
    (r'\bbreakpoint\s*\(', 'breakpoint() call - unwanted debugger'),
    (r'\bsubprocess\b', 'subprocess call - system execution risk'),
    (r'\bos\.system\b', 'os.system() call - system execution risk'),
    (r'\bos\.popen\b', 'os.popen() call - system execution risk'),
    (r'\bsocket\b', 'socket module - network access risk'),
]

# Patterns for PII/credential redaction in evaluation outputs.
PII_REDACTION_PATTERNS: List[Tuple[str, str]] = [
    (r'(?i)(?:api[_-]?key|API[_-]?KEY)\s*[=:]\s*\S+', 'API key redacted'),
    (r'(?i)(?:secret|SECRET)\s*[=:]\s*\S{8,}', 'Secret redacted'),
    (r'(?i)(?:password|PASSWORD|passwd)\s*[=:]\s*\S+', 'Password redacted'),
    (r'(?i)(?:token|TOKEN)\s*[=:]\s*\S{8,}', 'Token redacted'),
    (r'(?i)(?:bearer\s+)[a-z0-9._-]{20,}', 'Bearer token redacted'),
    (r'(?i)sk-[a-zA-Z0-9]{20,}', 'OpenAI-style key redacted'),
]

# Patterns for code injection attempts in benchmark test definitions.
TEST_CODE_FORBIDDEN_PATTERNS: List[Tuple[str, str]] = [
    (r'\brm\s+-rf\b', 'Recursive delete blocked'),
    (r'\bdd\s+if=', 'Disk write blocked'),
    (r'\bmkfs\.', 'Filesystem format blocked'),
    (r'\b>:?\s*/dev/', 'Device write blocked'),
    (r'\bchmod\s+777\b', 'Overly permissive mode blocked'),
]


# =============================================================================
# Output safety validation
# =============================================================================

def validate_output_safety(text: str) -> SafetyResult:
    """Check model output for dangerous patterns.

    Scans model-generated text for code injection, system commands,
    and other dangerous constructs. Used before evaluating model output.

    Args:
        text: The model output text to validate.

    Returns:
        SafetyResult with is_safe flag and any issues found.
    """
    if not text or not isinstance(text, str):
        return SafetyResult(is_safe=True)

    issues = []
    for pattern, description in OUTPUT_DANGEROUS_PATTERNS:
        if re.search(pattern, text):
            issues.append(f"Dangerous pattern detected: {description}")

    return SafetyResult(
        is_safe=len(issues) == 0,
        issues=issues,
        redacted_text=text,
    )


def sanitize_output(text: str) -> str:
    """Remove or redact sensitive content from model output.

    Strips API keys, secrets, tokens, and credentials from evaluation
    outputs so they are safe to log, display, or store.

    Args:
        text: The output text to sanitize.

    Returns:
        Sanitized text with sensitive content redacted.
    """
    if not text or not isinstance(text, str):
        return text or ""

    result = text
    for pattern, replacement in PII_REDACTION_PATTERNS:
        result = re.sub(pattern, f"[{replacement}]", result)

    return result


# =============================================================================
# Test code safety validation
# =============================================================================

def validate_test_code(code: str) -> SafetyResult:
    """Validate benchmark test code for safety.

    Checks that user-provided or model-generated test code does not
    contain destructive or invasive operations.

    Args:
        code: The code string to validate.

    Returns:
        SafetyResult with is_safe flag and any issues found.
    """
    if not code or not isinstance(code, str):
        return SafetyResult(is_safe=True)

    issues = []
    for pattern, description in TEST_CODE_FORBIDDEN_PATTERNS:
        if re.search(pattern, code):
            issues.append(f"Forbidden pattern in test code: {description}")

    # Check for import of dangerous modules
    dangerous_modules = ["socket", "subprocess", "requests", "urllib"]
    for mod in dangerous_modules:
        if re.search(rf'\bimport\s+{mod}\b', code) or re.search(rf'\bfrom\s+{mod}\b', code):
            issues.append(f"Import of potentially dangerous module: {mod}")

    return SafetyResult(
        is_safe=len(issues) == 0,
        issues=issues,
        redacted_text=code,
    )


# =============================================================================
# EvalSanitizer - unified output sanitizer
# =============================================================================

class EvalSanitizer:
    """Unified entry point for evaluation output sanitization.

    Combines output safety validation and PII redaction in a single
    configurable pipeline. Used by the evaluation runner before storing
    or displaying test results.

    Example:
        sanitizer = EvalSanitizer()
        safe, result = sanitizer.process("The API key is sk-abc123...")
        print(result.redacted_text)  # "The API key is [OpenAI-style key redacted]"
    """

    def __init__(self, enabled: bool = True, redact_pii: bool = True):
        self.enabled = enabled
        self.redact_pii = redact_pii

    def process(self, text: str) -> SafetyResult:
        """Run the full sanitization pipeline on output text.

        Args:
            text: The output text to validate and sanitize.

        Returns:
            SafetyResult with safety flags and redacted text.
        """
        if not self.enabled:
            return SafetyResult(is_safe=True, redacted_text=text)

        # Step 1: Check for dangerous patterns
        safety = validate_output_safety(text)
        if not safety.is_safe:
            return SafetyResult(
                is_safe=False,
                issues=safety.issues,
                redacted_text=text,
            )

        # Step 2: Redact PII
        if self.redact_pii:
            clean_text = sanitize_output(text)
        else:
            clean_text = text

        return SafetyResult(is_safe=True, redacted_text=clean_text)


# =============================================================================
# Global singleton
# =============================================================================

_global_sanitizer: Optional[EvalSanitizer] = None


def get_sanitizer() -> EvalSanitizer:
    """Get the global EvalSanitizer instance."""
    global _global_sanitizer
    if _global_sanitizer is None:
        _global_sanitizer = EvalSanitizer()
    return _global_sanitizer


def reset_sanitizer() -> None:
    """Reset the global sanitizer instance for testing."""
    global _global_sanitizer
    _global_sanitizer = None
