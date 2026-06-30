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
EITE Capability Integration - Evaluation Capabilities
=======================================================

Unified interface for discovering and invoking evaluation capabilities.
EITE integrates capabilities for running benchmarks, executing test cases,
scoring results, and generating reports.

Architecture:
    Any model or evaluation harness can discover and call all evaluation
    capabilities through a unified interface. The integrator discovers
    capability modules, generates tool schemas, and routes calls.

    EITE-specific capabilities:
    - benchmark_list: List available benchmarks
    - benchmark_run: Run a benchmark by name
    - test_run: Execute a single test case
    - score_analyze: Analyze scoring results
    - report_generate: Generate evaluation report

Module developers:
    Add a CAPABILITIES dict to your module to auto-register evaluation
    capabilities. Each entry: {name, description, params, handler}.

Author: EITE Team
"""
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("eite-agent.capability")


# =============================================================================
# Capability Definition
# =============================================================================

class CapabilityDef:
    """Descriptor for a single evaluation capability.

    Attributes:
        name: The name used to invoke this capability.
        module: The module that owns this capability.
        description: Human-readable description.
        params: JSON Schema parameter definitions.
        handler: Callable(capability_integrator, params) -> dict.
    """

    def __init__(
        self,
        name: str,
        module: str,
        description: str,
        params: Dict[str, Any],
        handler: Callable,
    ):
        self.name = name
        self.module = module
        self.description = description
        self.params = params
        self.handler = handler

    def to_tool_schema(self) -> Dict[str, Any]:
        """Generate an OpenAI function-calling schema entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params,
            },
        }

    def to_manifest_entry(self) -> Dict[str, Any]:
        """Generate a manifest entry."""
        return {
            "name": self.name,
            "module": self.module,
            "description": self.description.split(".")[0] if self.description else "",
        }


# =============================================================================
# Capability Registry
# =============================================================================

_capabilities: Dict[str, CapabilityDef] = {}


def register_capability(cap: CapabilityDef) -> None:
    """Register a capability. Later registrations overwrite earlier ones."""
    _capabilities[cap.name] = cap


def get_capability(name: str) -> Optional[CapabilityDef]:
    """Get a capability definition by name."""
    return _capabilities.get(name)


def get_all_capabilities() -> List[CapabilityDef]:
    """Return all registered capabilities."""
    return list(_capabilities.values())


def clear_capabilities() -> None:
    """Clear all registered capabilities (for testing/reload)."""
    _capabilities.clear()


# =============================================================================
# Built-in evaluation capability discoverers
# =============================================================================

def _discover_benchmark_caps() -> List[CapabilityDef]:
    """Discover benchmark management capabilities."""

    def _list_benchmarks(params: dict) -> dict:
        try:
            from eite_benchmarks import list_benchmarks
            benchmarks = list_benchmarks()
            return {"success": True, "count": len(benchmarks), "benchmarks": benchmarks}
        except ImportError:
            return {"success": False, "error": "eite_benchmarks module not available"}

    def _run_benchmark(params: dict) -> dict:
        try:
            from eite_benchmarks import run_benchmark
            result = run_benchmark(
                name=params["name"],
                model=params.get("model", ""),
                config=params.get("config", {}),
            )
            return {"success": True, "result": result}
        except ImportError:
            return {"success": False, "error": "eite_benchmarks module not available"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return [
        CapabilityDef(
            name="benchmark_list",
            module="benchmark",
            description="List all available evaluation benchmarks with descriptions and version info.",
            params={"type": "object", "properties": {}},
            handler=_list_benchmarks,
        ),
        CapabilityDef(
            name="benchmark_run",
            module="benchmark",
            description="Run a named benchmark with the specified model and configuration.",
            params={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Benchmark name (e.g. 'mmlu', 'gsm8k', 'code_eval')",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model identifier to evaluate",
                    },
                    "config": {
                        "type": "object",
                        "description": "Optional config overrides (temperature, max_tokens, etc.)",
                    },
                },
                "required": ["name"],
            },
            handler=_run_benchmark,
        ),
    ]


def _discover_test_caps() -> List[CapabilityDef]:
    """Discover test execution capabilities."""

    def _run_test(params: dict) -> dict:
        try:
            from eite_core.test_runner import run_test_case
            result = run_test_case(
                test_id=params["test_id"],
                model=params.get("model", ""),
                input_data=params.get("input", {}),
            )
            return {"success": True, "result": result}
        except ImportError:
            return {"success": False, "error": "eite_core.test_runner not available"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return [
        CapabilityDef(
            name="test_run",
            module="test",
            description="Execute a single test case by ID with the specified model and input data.",
            params={
                "type": "object",
                "properties": {
                    "test_id": {
                        "type": "string",
                        "description": "Test case identifier",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model identifier to use",
                    },
                    "input": {
                        "type": "object",
                        "description": "Input data for the test case",
                    },
                },
                "required": ["test_id"],
            },
            handler=_run_test,
        ),
    ]


def _discover_scoring_caps() -> List[CapabilityDef]:
    """Discover scoring and analysis capabilities."""

    def _analyze_score(params: dict) -> dict:
        try:
            from eite_core.scoring import analyze_results
            analysis = analyze_results(
                results=params.get("results", []),
                method=params.get("method", "exact_match"),
            )
            return {"success": True, "analysis": analysis}
        except ImportError:
            return {"success": False, "error": "eite_core.scoring not available"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _generate_report(params: dict) -> dict:
        try:
            from eite_core.reporting import generate_report
            report = generate_report(
                eval_id=params.get("eval_id", ""),
                format=params.get("format", "json"),
            )
            return {"success": True, "report": report}
        except ImportError:
            return {"success": False, "error": "eite_core.reporting not available"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return [
        CapabilityDef(
            name="score_analyze",
            module="scoring",
            description="Analyze evaluation results using the specified scoring method.",
            params={
                "type": "object",
                "properties": {
                    "results": {
                        "type": "array",
                        "description": "List of test results to analyze",
                        "items": {"type": "object"},
                    },
                    "method": {
                        "type": "string",
                        "enum": ["exact_match", "fuzzy_match", "llm_judge", "code_execution"],
                        "description": "Scoring method",
                    },
                },
                "required": ["results"],
            },
            handler=_analyze_score,
        ),
        CapabilityDef(
            name="report_generate",
            module="scoring",
            description=(
                "Generate an evaluation report from a completed evaluation run. "
                "Supports JSON, Markdown, and HTML formats."
            ),
            params={
                "type": "object",
                "properties": {
                    "eval_id": {
                        "type": "string",
                        "description": "Evaluation run ID",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["json", "markdown", "html"],
                        "description": "Report output format",
                        "default": "json",
                    },
                },
                "required": ["eval_id"],
            },
            handler=_generate_report,
        ),
    ]


# =============================================================================
# CapabilityIntegrator - facade
# =============================================================================

class CapabilityIntegrator:
    """EITE capability integration layer - discovers, registers, and dispatches.

    Usage:
        integrator = CapabilityIntegrator()
        integrator.discover()
        caps = integrator.list_capabilities()
        result = integrator.call("benchmark_list", {})
    """

    def __init__(self):
        self._discovered: List[CapabilityDef] = []

    def discover(self) -> List[CapabilityDef]:
        """Discover all evaluation capabilities from all modules."""
        discoverers = [
            _discover_benchmark_caps,
            _discover_test_caps,
            _discover_scoring_caps,
        ]

        all_caps: List[CapabilityDef] = []
        for discoverer in discoverers:
            try:
                caps = discoverer()
                all_caps.extend(caps)
            except Exception as e:
                logger.debug("Capability discoverer %s failed: %s", discoverer.__name__, e)

        for cap in all_caps:
            register_capability(cap)

        self._discovered = all_caps
        logger.info(
            "CapabilityIntegrator: discovered %d capabilities",
            len(all_caps),
        )
        return all_caps

    def list_capabilities(self) -> List[Dict[str, Any]]:
        """Return capability manifest as list of dicts."""
        return [c.to_manifest_entry() for c in get_all_capabilities()]

    def call(self, capability_name: str, params: dict) -> dict:
        """Dispatch a capability call to the appropriate handler.

        Args:
            capability_name: Name of the capability.
            params: Parameters for the capability handler.

        Returns:
            Dict with "success": bool and capability-specific result data.
        """
        cap = get_capability(capability_name)
        if cap is None:
            return {"success": False, "error": f"Unknown capability: {capability_name}"}

        try:
            result = cap.handler(params)
            return result if isinstance(result, dict) else {"success": True, "data": result}
        except Exception as e:
            logger.error("Capability %s failed: %s", capability_name, e)
            return {"success": False, "error": str(e)}

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Generate OpenAI function-calling schemas for all capabilities."""
        return [cap.to_tool_schema() for cap in get_all_capabilities()]


# =============================================================================
# Global integrator instance
# =============================================================================

_integrator: Optional[CapabilityIntegrator] = None


def set_integrator(integrator: CapabilityIntegrator) -> None:
    """Set the global integrator instance."""
    global _integrator
    _integrator = integrator


def capability_list(args: dict = None) -> dict:
    """Tool handler: list all available capabilities."""
    global _integrator
    if _integrator is None:
        return {"success": False, "error": "CapabilityIntegrator not initialized"}
    try:
        caps = _integrator.list_capabilities()
        return {"success": True, "count": len(caps), "capabilities": caps}
    except Exception as e:
        return {"success": False, "error": str(e)}


def capability_call(args: dict) -> dict:
    """Tool handler: invoke a named capability."""
    global _integrator
    if _integrator is None:
        return {"success": False, "error": "CapabilityIntegrator not initialized"}
    cap_name = args.get("name", "")
    params = args.get("params", {})
    if not cap_name:
        return {"success": False, "error": "Missing required parameter: name"}
    return _integrator.call(cap_name, params)
