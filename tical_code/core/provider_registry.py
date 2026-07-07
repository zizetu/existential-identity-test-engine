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

# provenance:ticalasi-zzt-2026
"""Provider Registry - load model cluster configs from git-tracked JSON files."""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("EITElite.provider_registry")


class ProviderRegistry:
    """Load and resolve provider and worker configurations from JSON files in the repository.

    Reads provider definitions and worker-specific configuration from the
    config/ directory, resolves environment variable overrides for API keys
    and endpoints, and produces a list of provider dictionaries suitable for
    consumption by ModelFailover. Supports multi-key providers where multiple
    API keys are distributed across environment variables (e.g., MIMO_API_KEY_1
    through MIMO_API_KEY_4).

    Config locations (first match wins):
      1. {repo_root}/config/providers.json
      2. {repo_root}/config/worker-configs/{worker_name}.json

    Env vars can override provider keys (e.g. OPENROUTER_API_KEY).
    """

    def __init__(self, repo_root: str = None, worker_name: str = None):
        """Initialize the provider registry with repository root and worker identity.

        Determines the repository root from the TICAL_CODE_ROOT environment
        variable or the current working directory, and resolves the worker
        name from the WORKER_NAME environment variable if not explicitly
        provided. Configuration is not loaded until load() is called.

        Args:
            repo_root: Absolute path to the EITElite repository root directory.
                If None, uses the TICAL_CODE_ROOT env var or os.getcwd().
            worker_name: Name identifier for this worker instance, used to locate
                the worker-specific config file under config/worker-configs/.
                If None, uses the WORKER_NAME environment variable.
        """
        if repo_root is None:
            repo_root = os.environ.get("TICAL_CODE_ROOT", os.getcwd())
        self.repo_root = Path(repo_root)
        self.worker_name = worker_name or os.environ.get("WORKER_NAME", "unknown")
        self._provider_defs: Dict = {}
        self._worker_config: Dict = {}
        self._loaded = False

    def load(self) -> "ProviderRegistry":
        """Load provider definitions and worker configuration from JSON files on disk.

        Reads config/providers.json for the master list of available providers
        and config/worker-configs/{worker_name}.json for worker-specific
        overrides such as provider ordering and disabled providers. Sets the
        internal _loaded flag to True so subsequent accessors skip re-reading
        the filesystem.

        If no providers.json exists, falls back to auto-discovery from
        common environment variables (DEEPSEEK_API_KEY, OPENAI_API_KEY, etc.)
        so new users can start without any configuration file.
        """
        # Load provider definitions
        providers_path = self.repo_root / "config" / "providers.json"
        if providers_path.exists():
            with open(providers_path) as f:
                data = json.load(f)
            self._provider_defs = data.get("providers", {})
            logger.info(
                "Loaded %d providers from %s",
                len(self._provider_defs),
                providers_path,
            )

        # Auto-discover from env vars if no providers.json or empty
        if not self._provider_defs:
            from tical_code.core.provider_autodiscover import auto_discover

            discovered = auto_discover()
            if discovered:
                self._provider_defs = {p["name"]: p for p in discovered}
                logger.info(
                    "Auto-discovered %d providers from environment variables",
                    len(discovered),
                )

        # Load worker config (optional — config/worker-configs/ may not exist in fresh installs)
        worker_path = (
            self.repo_root
            / "config"
            / "worker-configs"
            / f"{self.worker_name}.json"
        )
        if worker_path.exists():
            try:
                with open(worker_path) as f:
                    self._worker_config = json.load(f)
                logger.info("Loaded worker config for %s", self.worker_name)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load worker config %s: %s", worker_path, e)
                self._worker_config = {}
        else:
            logger.info("No worker config at %s (optional, using defaults)", worker_path)
            self._worker_config = {}

        self._loaded = True
        return self

    def get_providers(self) -> List[Dict]:
        """Build an ordered list of resolved provider dictionaries for ModelFailover consumption.

        Respects the worker config's provider ordering and disabled_providers
        list, resolves environment variables for API keys and endpoint URLs,
        and expands multi-key providers into individual entries. Automatically
        calls load() if configuration has not yet been loaded.

        Returns:
            A list of provider dictionaries, each containing keys such as
            "name", "model", "endpoint", "key", "auth_style", "priority",
            "timeout", and "is_fallback", ordered by the worker config's
            provider priority list.
        """
        if not self._loaded:
            self.load()

        result = []
        ordered_names = self._worker_config.get(
            "providers", list(self._provider_defs.keys())
        )
        disabled = set(self._worker_config.get("disabled_providers", []))

        for name in ordered_names:
            if name in disabled:
                continue
            if name not in self._provider_defs:
                logger.warning("Unknown provider in worker config: %s", name)
                continue

            pdef = self._provider_defs[name]
            provider_dicts = self._resolve_provider(name, pdef)
            result.extend(provider_dicts)

        return result

    def _resolve_provider(self, name: str, pdef: Dict) -> List[Dict]:
        """Resolve one provider definition into provider dict(s) for ModelFailover.

        Handles multi_key providers (e.g. mimo-token-plan with 4 keys).
        """
        if pdef.get("multi_key"):
            return self._resolve_multi_key(name, pdef)

        # Check if provider is disabled via environment variable
        enabled_env = pdef.get("enabled_env")
        if enabled_env:
            enabled_val = os.environ.get(enabled_env, pdef.get("enabled_default", "1"))
            if enabled_val.lower() in ("0", "false", "no", "off"):
                logger.info("Provider %s disabled by %s=%s", name, enabled_env, enabled_val)
                return []

        key = self._resolve_env(pdef.get("env_key"))
        if not key and pdef.get("auth_style") not in ("mimo-cli-subprocess",):
            # Skip providers without keys (unless free channel)
            return []

        endpoint = (
            self._resolve_env(pdef.get("env_base_url"))
            or pdef.get("default_base_url", "")
        )
        model = pdef.get("default_model", "")

        return [
            {
                "name": name,
                "model": model,
                "endpoint": endpoint,
                "key": key or "",
                "auth_style": pdef.get("auth_style", "bearer"),
                "priority": pdef.get("priority", 10),
                "timeout": pdef.get("timeout", 0),
                "is_fallback": pdef.get("priority", 10) >= 10,
            }
        ]

    def _resolve_multi_key(self, name: str, pdef: Dict) -> List[Dict]:
        """Resolve multi-key provider (e.g. MIMO_API_KEY_1 through _4)."""
        results = []
        key_count = pdef.get("key_count", 1)
        key_prefix = pdef.get("env_key_prefix", "")
        endpoint_prefix = pdef.get("env_endpoint_prefix", "")
        model_prefix = pdef.get("env_model_prefix", "")

        for i in range(1, key_count + 1):
            key = os.environ.get(f"{key_prefix}{i}", "")
            if not key:
                continue
            endpoint = (
                os.environ.get(f"{endpoint_prefix}{i}", "")
                or pdef.get("default_base_url", "")
            )
            model = (
                os.environ.get(f"{model_prefix}{i}", "")
                or pdef.get("default_model", "")
            )

            results.append(
                {
                    "name": f"{name}-{i}",
                    "model": model,
                    "endpoint": endpoint,
                    "key": key,
                    "auth_style": pdef.get("auth_style", "bearer"),
                    "auth_header_name": pdef.get("auth_header_name"),
                    "priority": pdef.get("priority", 10),
                    "timeout": pdef.get("timeout", 0),
                    "is_fallback": pdef.get("priority", 10) >= 10,
                }
            )
        return results

    @staticmethod
    def _resolve_env(env_key: Optional[str]) -> str:
        """Resolve env var or return empty string."""
        if env_key:
            return os.environ.get(env_key, "")
        return ""

    def get_worker_config(self) -> Dict:
        """Return a copy of the worker-specific configuration dictionary.

        Provides access to worker-level settings such as provider ordering,
        disabled providers, and other operational parameters defined in the
        worker's JSON config file under config/worker-configs/. Automatically
        calls load() if configuration has not yet been loaded.

        Returns:
            A dictionary copy of the loaded worker configuration, so mutations
            by the caller do not affect the internal state.
        """
        if not self._loaded:
            self.load()
        return dict(self._worker_config)

    def list_providers(self) -> List[Dict]:
        """List all registered provider definitions with metadata and availability details.

        Returns a list of dictionaries describing each provider defined in the
        providers.json configuration file, including display name, model family,
        default model, cost tier, priority, authentication style, and a list
        of available models. This is intended for inspection and debugging,
        not for direct consumption by ModelFailover (use get_providers for that).

        Returns:
            A list of provider metadata dictionaries, each with keys such as
            "name", "display_name", "family", "default_model", "cost",
            "priority", "auth_style", and "available_models".
        """
        if not self._loaded:
            self.load()
        result = []
        for name, pdef in self._provider_defs.items():
            result.append(
                {
                    "name": name,
                    "display_name": pdef.get("name", name),
                    "family": pdef.get("family", "unknown"),
                    "default_model": pdef.get("default_model", "?"),
                    "cost": pdef.get("cost", "unknown"),
                    "priority": pdef.get("priority", 10),
                    "auth_style": pdef.get("auth_style", "bearer"),
                    "available_models": pdef.get("available_models", []),
                }
            )
        return result


def from_registry(
    repo_root: str = None, worker_name: str = None
) -> "ModelFailover":
    """Build a ModelFailover instance from the git-managed provider configuration registry.

    Constructs a ProviderRegistry, loads its configuration from the JSON files
    (or auto-discovers from env vars), resolves all providers, and returns
    a fully initialized ModelFailover instance.
    """
    from tical_code.core.model_failover import ModelFailover

    registry = ProviderRegistry(
        repo_root=repo_root, worker_name=worker_name
    )
    registry.load()
    providers = registry.get_providers()

    if not providers:
        logger.warning(
            "No providers found (no config, no env vars). "
            "Set DEEPSEEK_API_KEY, OPENAI_API_KEY or similar."
        )
        return ModelFailover(providers=[])

    return ModelFailover(providers=providers)
