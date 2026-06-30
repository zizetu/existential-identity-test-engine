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
Verify Module - EITE Verification framework for evaluation quality assurance
==============================================================================

Provides the canonical VerifyLevel enum, force_verify decorator,
PluginVerifyMixin base class, and VerifyResult dataclass.

All EITE evaluation components depend on this module for operation
verification during benchmark runs.
"""

import time
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Verify Levels
# =============================================================================

class VerifyLevel(IntEnum):
    """Granularity of operation verification in the EITE evaluation framework."""
    NONE = 0       # No verification required
    BASIC = 1      # Basic truthiness check
    SCHEMA = 2     # Schema validation
    DUAL = 3       # Dual-execution compare
    HUMAN = 4      # Human-in-the-loop approval
    IDENTITY = 5   # Identity-bound verification


# =============================================================================
# Verify Result
# =============================================================================

@dataclass
class VerifyResult:
    """Outcome of a verification check in the EITE evaluation framework."""
    passed: bool = True
    level: VerifyLevel = VerifyLevel.NONE
    method: str = "none"
    details: str = ""
    elapsed_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "level": self.level.name,
            "method": self.method,
            "details": self.details,
            "elapsed_ms": self.elapsed_ms,
        }


# =============================================================================
# force_verify Decorator
# =============================================================================

def force_verify(level: VerifyLevel = VerifyLevel.BASIC, schema: dict = None,
                 tool_name: str = "") -> Callable:
    """Decorator that marks a function for verification enforcement.

    In the current implementation this is a no-op marker -- the actual
    verification logic runs in the tool dispatch layer. The decorator
    exists so evaluation components can declare their verification
    intent at the function level for future enforcement.

    Args:
        level: Required verification level (default BASIC).
        schema: Optional JSON schema for SCHEMA-level verification.
        tool_name: Optional tool name override.

    Returns:
        The original function unchanged (marker decorator).
    """
    def decorator(func: Callable) -> Callable:
        func._verify_level = level
        func._verify_schema = schema
        return func
    return decorator


def verify_result(result: Any, level: VerifyLevel = VerifyLevel.BASIC,
                  method: str = "basic") -> VerifyResult:
    """Create a VerifyResult from an operation outcome.

    Args:
        result: The operation result (truthy = passed).
        level: Verification level used.
        method: Verification method name.

    Returns:
        VerifyResult instance.
    """
    return VerifyResult(
        passed=result is not None,
        level=level,
        method=method,
    )


# =============================================================================
# PluginVerifyMixin
# =============================================================================

class PluginVerifyMixin:
    """Mixin that adds verification capabilities to evaluation plugins.

    Provides convenience methods for evaluation components to verify
    their operations using the EITE verification framework. Components
    should inherit from both their base class and this mixin.

    Methods:
        verify_operation: Run a verification check on an operation result.
        verify_with_schema: Validate output against a JSON schema.
    """

    def verify_operation(self, result: Any, level: VerifyLevel = VerifyLevel.BASIC,
                         method: str = "plugin") -> VerifyResult:
        """Verify an evaluation plugin operation result.

        Args:
            result: The operation result to verify.
            level: Verification strictness level.
            method: Identifying method name for the verification.

        Returns:
            VerifyResult with pass/fail status.
        """
        return verify_result(result, level=level, method=method)

    def verify_with_schema(self, data: dict, schema: dict,
                           method: str = "schema") -> VerifyResult:
        """Validate data against a JSON schema (basic key-existence check).

        NOTE: Full JSON Schema validation requires the jsonschema library.
        This basic implementation checks that all required keys from the
        schema's 'required' array are present in the data.

        Args:
            data: The data dict to validate.
            schema: JSON Schema dict with optional 'required' key.
            method: Identifying method name.

        Returns:
            VerifyResult with pass/fail status.
        """
        required = schema.get("required", []) if schema else []
        missing = [k for k in required if k not in data]
        if missing:
            return VerifyResult(
                passed=False,
                level=VerifyLevel.SCHEMA,
                method=method,
                details=f"Missing required keys: {missing}",
            )
        return VerifyResult(
            passed=True,
            level=VerifyLevel.SCHEMA,
            method=method,
            details=f"Schema check passed ({len(required)} required keys present)",
        )
