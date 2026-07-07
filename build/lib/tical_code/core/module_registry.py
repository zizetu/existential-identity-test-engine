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
Module Registry - single source of truth for all worker modules.

Every optional module registers itself here. The registry replaces the
~470 lines of try/except blocks in unified_worker.py with a structured
load pipeline that handles dependency ordering, config gating, and
error isolation.

Usage:
    from tical_code.core.module_registry import register, load_modules, get_active

    @register(
        name="decision_engine",
        config_key="decision_engine",
        default_enabled=True,
        description="Structured decision pipeline (clarify + conditions + strategy + verify)",
        dependencies=["constitution"],
    )
    def _init_decision_engine(worker, cfg):
        from tical_code.core.decision_engine import DecisionEngine
        return DecisionEngine(
            max_iterations=cfg.get("max_tool_iterations", 15),
            constitution_enforcer=worker.constitution,
            agent_type=cfg.get("agent_type", "default"),
        )

    # Then in Worker.__init__:
    self._active_modules = load_modules(self, cfg, profile="full")
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("EITElite.registry")

_registry: Dict[str, "ModuleSpec"] = {}
_load_order: List[str] = []  # topological sort result


@dataclass
class ModuleSpec:
    """Descriptor for a single optional module."""
    name: str                          # "decision_engine"
    attr_name: str                     # worker attribute name (self.decision_engine)
    config_key: str                    # key in modules config dict
    default_enabled: bool              # True = on by default
    description: str                   # human-readable one-liner
    dependencies: List[str] = field(default_factory=list)  # must load before this
    init_fn: Optional[Callable] = None # fn(worker, cfg) -> instance or None
    profile: str = "full"             # "full" (EITElite) or "light" (EITElite)


def register(
    name: str,
    *,
    attr_name: Optional[str] = None,
    config_key: Optional[str] = None,
    default_enabled: bool = True,
    description: str = "",
    dependencies: Optional[List[str]] = None,
    profile: str = "full",
):
    """Decorator: register a module init function.

    Args:
        name: Module name (used as attr_name if not specified).
        attr_name: Worker attribute name. Defaults to name.
        config_key: Key in modules config dict. Defaults to name.
        default_enabled: Whether ON by default.
        description: One-line description for prompt generation.
        dependencies: Names of modules that must load before this one.
        profile: "full" (EITElite) or "light" (EITElite).
    """
    def decorator(fn: Callable):
        spec = ModuleSpec(
            name=name,
            attr_name=attr_name or name,
            config_key=config_key or name,
            default_enabled=default_enabled,
            description=description,
            dependencies=dependencies or [],
            init_fn=fn,
            profile=profile,
        )
        _registry[name] = spec
        return fn
    return decorator


def _topological_sort() -> List[str]:
    """Return module names in dependency-safe load order."""
    visited: set = set()
    temp: set = set()
    order: List[str] = []

    def visit(name: str):
        if name in temp:
            # Circular dependency - log and skip
            logger.error("Circular dependency detected involving %s", name)
            return
        if name in visited:
            return
        temp.add(name)
        spec = _registry.get(name)
        if spec:
            for dep in spec.dependencies:
                if dep in _registry:
                    visit(dep)
                else:
                    logger.warning("Module %s depends on unknown module %s", name, dep)
        temp.discard(name)
        visited.add(name)
        order.append(name)

    for name in _registry:
        visit(name)
    return order


def load_modules(worker: Any, cfg: dict, profile: str = "full") -> Dict[str, Any]:
    """Load all registered modules onto the worker.

    Args:
        worker: The Worker instance (modules are set as attributes).
        cfg: Full worker config dict.
        profile: "full" or "light" - filters which modules are eligible.

    Returns:
        Dict of {name: instance} for successfully loaded modules.
    """
    global _load_order
    if not _load_order:
        _load_order = _topological_sort()

    modules_cfg = cfg.get("modules", {})
    active: Dict[str, Any] = {}

    # Pre-initialize ALL module attributes to None so worker code never
    # hits AttributeError - even for profile-filtered or disabled modules.
    for name, spec in _registry.items():
        if not hasattr(worker, spec.attr_name):
            setattr(worker, spec.attr_name, None)

    for name in _load_order:
        spec = _registry.get(name)
        if spec is None:
            continue

        # Profile filter: "full" modules only load when profile="full"
        if spec.profile == "full" and profile != "full":
            continue

        # Config gate: check if enabled
        enabled = modules_cfg.get(spec.config_key, spec.default_enabled)
        if not enabled:
            logger.debug("Module %s: disabled by config", name)
            # Set attribute to None so worker code can check it
            setattr(worker, spec.attr_name, None)
            continue

        # Check dependencies
        missing_deps = []
        for dep in spec.dependencies:
            if dep not in active:
                missing_deps.append(dep)
        if missing_deps:
            logger.warning(
                "Module %s: skipping - dependencies not loaded: %s",
                name, ", ".join(missing_deps),
            )
            setattr(worker, spec.attr_name, None)
            continue

        # Try to initialize
        try:
            instance = spec.init_fn(worker, cfg)
            setattr(worker, spec.attr_name, instance)
            if instance is not None:
                active[name] = instance
                logger.info("Module %s: active - %s", name, spec.description)
            else:
                logger.debug("Module %s: init returned None (disabled internally)", name)
        except Exception as e:
            logger.warning("Module %s: init failed - %s", name, e)
            setattr(worker, spec.attr_name, None)

    return active


def get_active_descriptions(active: Dict[str, Any]) -> List[Tuple[str, str, bool]]:
    """Return [(name, description, is_full_profile), ...] for all loaded modules.

    Used by prompt.py to build the capability manifest dynamically.
    """
    results = []
    for name, _instance in active.items():
        spec = _registry.get(name)
        if spec:
            results.append((spec.name, spec.description, spec.profile == "full"))
    return results


def get_all_specs(profile: str = "full") -> List[ModuleSpec]:
    """Return all registered module specs for a given profile."""
    return [s for s in _registry.values() if s.profile == profile or profile == "full"]
