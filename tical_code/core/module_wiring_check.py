#!/usr/bin/env python3
"""
Runtime module wiring verification.

Ensures that every module registered as "active" has its attribute
properly set on the worker and that the SharedContext attribute
names match. Catches the class of bug where module_registry says
"compactor: active" but unified_worker.py does
"getattr(self, 'context_compactor', None)" → None.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("EITElite.wiring_check")

# Where the worker writes its module wiring report on startup
_WIRING_REPORT_PATH = "/opt/tical-guardian/module_wiring.json"


def verify_worker_wiring(
    worker: Any,
    active_modules: Dict[str, Any],
    ctx: Any,
    strict: bool = True,
) -> List[str]:
    """Verify all active module attributes are properly wired on worker and ctx.

    Runs at worker startup right after SharedContext is built.
    Returns list of error messages (empty = all good).

    Args:
        worker: The Worker/AsyncWorker instance
        active_modules: Dict returned by load_modules()
        ctx: The SharedContext just built
        strict: If True, raises RuntimeError on critical mismatches
    """
    from tical_code.core.module_registry import _registry

    errors: List[str] = []
    warnings: List[str] = []

    for name, instance in active_modules.items():
        spec = _registry.get(name)
        if spec is None:
            warnings.append(f"Module '{name}' in active but not in registry")
            continue

        # 1. Verify worker attribute is set
        worker_val = getattr(worker, spec.attr_name, None)
        if worker_val is None:
            errors.append(
                f"Module '{name}': worker.{spec.attr_name} is None "
                f"(registry says active but attribute not set)"
            )
            continue

        if worker_val is not instance:
            errors.append(
                f"Module '{name}': worker.{spec.attr_name}={type(worker_val).__name__} "
                f"!= registry instance {type(instance).__name__}"
            )
            continue

        # 2. Verify ctx attribute exists (if it was supposed to be passed)
        # We check the attr_name that SharedContext should expose
        ctx_val = getattr(ctx, spec.attr_name, "___NOT_FOUND___")
        if ctx_val == "___NOT_FOUND___":
            warnings.append(
                f"Module '{name}': ctx.{spec.attr_name} not found "
                f"(SharedContext may use a different attribute name)"
            )

    # Log results
    if errors:
        logger.critical(
            "WIRING VERIFICATION FAILED: %d module(s) miswired: %s",
            len(errors),
            "; ".join(errors),
        )
        if strict:
            raise RuntimeError(
                f"Module wiring verification failed: {'; '.join(errors)}"
            )

    if warnings:
        logger.warning(
            "WIRING VERIFICATION WARNINGS: %s",
            "; ".join(warnings),
        )

    # Write report for guardian
    _write_wiring_report(active_modules, errors, warnings)

    return errors


def verify_ctx_critical_attrs(ctx: Any) -> List[str]:
    """Verify specific critical ctx attributes that message_handler depends on.

    These are the attributes that caused the L4 guardrail bug:
    ctx.compactor vs ctx.context_compactor.
    """
    critical_checks = [
        # (expected_attr, module_name, description)
        ("context_compactor", "context_compactor", "ContextCompactor"),
        ("doom_loop", "doom_loop", "DoomLoopDetector"),
    ]

    errors = []
    for attr, module_name, desc in critical_checks:
        val = getattr(ctx, attr, None)
        if val is None:
            errors.append(
                f"CRITICAL: ctx.{attr} is None — {desc} not wired. "
                f"Module '{module_name}' may be registered as active but "
                f"getattr(self, attr_name) returned None at SharedContext build time."
            )

    if errors:
        logger.critical("CTX CRITICAL ATTRS MISSING: %s", "; ".join(errors))

    return errors


def _write_wiring_report(
    active_modules: Dict[str, Any],
    errors: List[str],
    warnings: List[str],
) -> None:
    """Write module wiring status to a JSON file for the guardian daemon."""
    try:
        from tical_code.core.module_registry import _registry

        modules_status = {}
        for name, instance in active_modules.items():
            spec = _registry.get(name)
            modules_status[name] = {
                "attr_name": spec.attr_name if spec else "unknown",
                "type": type(instance).__name__,
                "wired": True,
            }

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "modules": modules_status,
            "errors": errors,
            "warnings": warnings,
            "healthy": len(errors) == 0,
        }

        os.makedirs(os.path.dirname(_WIRING_REPORT_PATH), exist_ok=True)
        with open(_WIRING_REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2)
        logger.debug("Module wiring report written to %s", _WIRING_REPORT_PATH)
    except Exception as e:
        logger.warning("Failed to write module wiring report: %s", e)


# ── Guardian check ──────────────────────────────────────────────────────

def check_module_wiring() -> Tuple[bool, str]:
    """Guardian check: reads the worker's module wiring report.

    Returns (ok, detail_string).
    """
    try:
        if not os.path.exists(_WIRING_REPORT_PATH):
            return False, "No module wiring report found — worker may not have started with verification"

        with open(_WIRING_REPORT_PATH) as f:
            report = json.load(f)

        ts = report.get("timestamp", "unknown")
        errors = report.get("errors", [])
        warnings = report.get("warnings", [])
        healthy = report.get("healthy", False)
        modules = report.get("modules", {})

        wired_count = sum(1 for m in modules.values() if m.get("wired"))
        total = len(modules)

        if not healthy:
            return False, f"[{ts}] {len(errors)} wiring errors: {'; '.join(errors[:3])}"

        if warnings:
            return True, f"[{ts}] {wired_count}/{total} wired, {len(warnings)} warnings"

        return True, f"[{ts}] {wired_count}/{total} modules wired correctly"

    except json.JSONDecodeError:
        return False, "Module wiring report corrupted"
    except Exception as e:
        return False, f"Module wiring check failed: {e}"
