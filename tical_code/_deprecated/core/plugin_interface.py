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
EITE Plugin Interface - Evaluation Plugin SPI
===============================================

Service Provider Interface for EITE evaluation plugins. Plugins can add
custom benchmarks, scoring functions, output validators, and report
generators to the evaluation framework.

Core philosophy: "unified interface, plugin-based extension"

This module defines:
- PluginMetadata: Plugin metadata structure
- PluginContext: Restricted context passed to plugins
- PluginInterface: Abstract base class for all plugins
- PluginManager: Loads and manages plugins

Usage:
    from tical_code.core.plugin_interface import (
        PluginInterface, PluginMetadata, PluginContext
    )

    class MyBenchmarkPlugin(PluginInterface):
        def get_metadata(self) -> PluginMetadata:
            return PluginMetadata(
                name="my_benchmark",
                display_name="My Custom Benchmark",
                version="1.0.0",
            )

        def init(self, context: PluginContext) -> None:
            self.context = context

        def get_tools(self) -> list:
            return [...]  # Custom benchmark tools

Author: EITE Team
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger("eite-agent.plugin")


# =============================================================================
# Plugin Metadata
# =============================================================================

@dataclass
class PluginMetadata:
    """Plugin metadata - basic information about a plugin.

    All plugins MUST provide this information.

    Attributes:
        name: Unique identifier for this plugin.
        display_name: Human-readable name.
        version: Semantic version string.
        author: Plugin author.
        description: Plugin description.
        dependencies: Other plugin names this plugin depends on.
        min_eite_version: Minimum EITE framework version required.
        auto_load: Whether to auto-load on startup.
    """
    name: str
    display_name: str
    version: str
    author: str = ""
    description: str = ""
    dependencies: List[str] = field(default_factory=list)
    min_eite_version: str = "0.1.5"
    auto_load: bool = True

    def validate(self) -> List[str]:
        """Validate plugin metadata.

        Returns:
            List of validation errors (empty if valid).
        """
        errors = []
        if not self.name:
            errors.append("Plugin name is required")
        elif not self.name.replace("_", "").replace("-", "").isalnum():
            errors.append("Plugin name must be alphanumeric (underscores/hyphens allowed)")
        if not self.display_name:
            errors.append("Plugin display_name is required")
        if not self.version:
            errors.append("Plugin version is required")
        return errors

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "dependencies": self.dependencies,
            "min_eite_version": self.min_eite_version,
            "auto_load": self.auto_load,
        }


# =============================================================================
# Plugin Context
# =============================================================================

@dataclass
class PluginContext:
    """Restricted context passed to plugins during initialization.

    Provides access to framework resources in a controlled way.

    Attributes:
        config: Plugin-specific configuration (read-only).
        checkpoint_manager: CheckpointManager instance for state persistence.
        llm_backend: LLM backend for model calls.
        allowed_dirs: Directories the plugin is allowed to access.
    """
    config: Dict[str, Any]
    checkpoint_manager: Any = None
    llm_backend: Any = None
    allowed_dirs: List[str] = field(default_factory=lambda: ["."])

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get a configuration value."""
        return self.config.get(key, default)


# =============================================================================
# Plugin Interface (SPI)
# =============================================================================

class PluginInterface(ABC):
    """Plugin SPI - all evaluation plugins must implement this interface.

    Lifecycle:
    1. get_metadata() -> PluginMetadata: Return plugin metadata.
    2. init(context) -> None: Initialize plugin with context.
    3. get_tools() -> list: Return tool definitions provided by this plugin.
    4. shutdown() -> None: Cleanup resources.

    Optional hooks:
    - on_eval_start(eval_id): Called when an evaluation run starts.
    - on_eval_end(eval_id, summary): Called when an evaluation run ends.
    - on_test_result(test_id, result): Called after each test result.
    - validate_config(config): Validate plugin-specific configuration.
    """

    @abstractmethod
    def get_metadata(self) -> PluginMetadata:
        """Return plugin metadata.

        Returns:
            PluginMetadata instance with plugin information.
        """
        pass

    @abstractmethod
    async def init(self, context: PluginContext) -> None:
        """Initialize the plugin with framework context.

        Called once during evaluation runner bootstrap.

        Args:
            context: PluginContext with framework resources.
        """
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """Clean up plugin resources.

        Called during evaluation runner shutdown.
        Use this to release resources, save state, close connections.
        """
        pass

    def get_tools(self) -> List[Any]:
        """Return the tools provided by this plugin.

        Returns:
            List of tool definitions (ToolDefinition objects).
            Default: empty list.
        """
        return []

    # =====================================================================
    # Optional Hooks
    # =====================================================================

    async def on_eval_start(self, eval_id: str) -> None:
        """Hook: Called when an evaluation run starts.

        Args:
            eval_id: Evaluation run ID.
        """
        pass

    async def on_eval_end(self, eval_id: str, summary: Dict[str, Any]) -> None:
        """Hook: Called when an evaluation run ends.

        Args:
            eval_id: Evaluation run ID.
            summary: Evaluation summary dict.
        """
        pass

    async def on_test_result(
        self,
        test_id: str,
        result: Dict[str, Any],
    ) -> None:
        """Hook: Called after each test result is recorded.

        Args:
            test_id: Test case ID.
            result: Test result dict (includes passed, score, metrics).
        """
        pass

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate plugin configuration.

        Args:
            config: Configuration to validate.

        Returns:
            List of validation errors (empty if valid).
        """
        return []


# =============================================================================
# Plugin Manager
# =============================================================================

class PluginManager:
    """Plugin manager - loads and manages evaluation plugins.

    Handles:
    - Plugin discovery and loading
    - Plugin initialization and lifecycle
    - Tool registration
    - Dependency resolution
    """

    def __init__(self):
        self._plugins: Dict[str, PluginInterface] = {}
        self._context: Optional[PluginContext] = None

    def set_context(self, context: PluginContext) -> None:
        """Set the plugin context."""
        self._context = context

    async def load_plugin(self, plugin_class: type) -> PluginInterface:
        """Load and initialize a plugin.

        Args:
            plugin_class: Plugin class (must implement PluginInterface).

        Returns:
            Initialized plugin instance.

        Raises:
            ValueError: If plugin_class does not implement PluginInterface.
            RuntimeError: If dependencies are not satisfied.
        """
        if not issubclass(plugin_class, PluginInterface):
            raise ValueError(f"{plugin_class.__name__} must implement PluginInterface")

        plugin = plugin_class()

        # Validate metadata
        metadata = plugin.get_metadata()
        errors = metadata.validate()
        if errors:
            raise ValueError(f"Invalid plugin metadata: {errors}")

        # Check dependencies
        for dep in metadata.dependencies:
            if dep not in self._plugins:
                raise RuntimeError(
                    f"Plugin {metadata.name} requires {dep} but it is not loaded"
                )

        # Initialize plugin
        if self._context:
            await plugin.init(self._context)

        # Register plugin
        self._plugins[metadata.name] = plugin
        logger.info("Plugin loaded: %s v%s", metadata.name, metadata.version)
        return plugin

    async def load_plugins(self, plugin_classes: List[type]) -> int:
        """Load multiple plugins.

        Args:
            plugin_classes: List of plugin classes to load.

        Returns:
            Number of plugins loaded.
        """
        loaded = 0
        for plugin_class in plugin_classes:
            try:
                plugin = await self.load_plugin(plugin_class)
                loaded += 1
            except Exception as e:
                logger.error(
                    "Failed to load plugin %s: %s",
                    getattr(plugin_class, "__name__", "?"), e,
                )
        return loaded

    async def unload_plugin(self, name: str) -> bool:
        """Unload a plugin.

        Args:
            name: Plugin name.

        Returns:
            True if unloaded successfully.
        """
        if name not in self._plugins:
            return False

        plugin = self._plugins[name]

        # Check if other plugins depend on this one
        for p in self._plugins.values():
            if name in p.get_metadata().dependencies:
                logger.error(
                    "Cannot unload %s: other plugins depend on it", name,
                )
                return False

        await plugin.shutdown()
        del self._plugins[name]
        logger.info("Plugin unloaded: %s", name)
        return True

    def get_plugin(self, name: str) -> Optional[PluginInterface]:
        """Get a loaded plugin by name."""
        return self._plugins.get(name)

    def list_plugins(self) -> List[PluginMetadata]:
        """List all loaded plugins."""
        return [p.get_metadata() for p in self._plugins.values()]

    def get_all_tools(self) -> List[Any]:
        """Get all tools from all plugins."""
        tools = []
        for plugin in self._plugins.values():
            tools.extend(plugin.get_tools())
        return tools

    async def shutdown_all(self) -> None:
        """Shutdown all plugins."""
        for name in list(self._plugins.keys()):
            await self.unload_plugin(name)
