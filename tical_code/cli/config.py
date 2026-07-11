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
Configuration Management
========================

Handles eite-agent configuration storage and retrieval.
"""

import os
import json
import shutil
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass, field


# =============================================================================
# Config Schema
# =============================================================================

DEFAULT_CONFIG = {
    "edition": "auto",  # auto, lite, full
    "verify_level": "schema",  # none, basic, schema, dual, human
    "log_level": "INFO",
    "log_dir": "~/.tical/logs",
    "data_dir": "~/.tical/data",
    "anchor_file": "~/.tical/anchors.json",
    "memory_file": "~/.tical/memory.json",
    
    # Worker defaults
    "ssh_timeout": 30,
    "ssh_connect_timeout": 10,
    "default_worker": None,
    
    # Execution defaults
    "execution_timeout": 300,
    "max_retries": 3,
    
    # Plugin settings
    "plugins_enabled": [],
    "plugins_dir": "~/.tical/plugins",
    
    # Security
    "strict_host_check": True,
    "auto_verify": True,
}


# =============================================================================
# Config Manager
# =============================================================================

class ConfigManager:
    """
    Manages eite-agent configuration.
    
    Supports both global (~/.tical/config.json) and local (.tical.json) configs.
    Local configs override global ones.
    """
    
    def __init__(self, config_file: Optional[str] = None, local: bool = False):
        """
        Initialize Config Manager.
        
        Args:
            config_file: Path to config file (default: ~/.tical/config.json)
            local: Use local config instead of global
        """
        if config_file:
            self.config_file = os.path.expanduser(config_file)
        elif local:
            self.config_file = os.path.join(os.getcwd(), '.tical.json')
        else:
            self.config_file = os.path.expanduser("~/.tical/config.json")
        
        self.config: Dict[str, Any] = {}
        self._load()
    
    def _load(self):
        """Load configuration from file."""
        # Start with defaults
        self.config = DEFAULT_CONFIG.copy()
        
        # Load from file
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    user_config = json.load(f)
                    self.config.update(user_config)
            except Exception as e:
                print(f"Warning: Failed to load config: {e}")
        
        # Expand ~ in paths
        for key in ['log_dir', 'data_dir', 'anchor_file', 'memory_file', 'plugins_dir']:
            if key in self.config:
                self.config[key] = os.path.expanduser(self.config[key])
    
    def _save(self):
        """Save configuration to file."""
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        with open(self.config_file, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value.
        
        Args:
            key: Configuration key (supports dot notation, e.g., "worker.defaults.timeout")
            default: Default value if key not found
            
        Returns:
            Configuration value or default
        """
        # Support dot notation
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    def set(self, key: str, value: Any, save: bool = True):
        """
        Set a configuration value.
        
        Args:
            key: Configuration key (supports dot notation)
            value: Value to set
            save: Whether to save to file immediately
        """
        # Support dot notation
        keys = key.split('.')
        target = self.config
        
        for k in keys[:-1]:
            if k not in target:
                target[k] = {}
            target = target[k]
        
        target[keys[-1]] = value
        
        if save:
            self._save()
    
    def unset(self, key: str, save: bool = True):
        """
        Unset a configuration value (revert to default).
        
        Args:
            key: Configuration key
            save: Whether to save to file immediately
        """
        keys = key.split('.')
        target = self.config
        
        for k in keys[:-1]:
            if k not in target:
                return
            target = target[k]
        
        if keys[-1] in target:
            del target[keys[-1]]
            
            if save:
                self._save()
    
    def get_all(self) -> Dict[str, Any]:
        """Get all configuration as dictionary."""
        return self.config.copy()
    
    def reset(self, save: bool = True):
        """Reset to default configuration."""
        self.config = DEFAULT_CONFIG.copy()
        if save:
            self._save()
    
    def export(self, filepath: str):
        """
        Export configuration to a file.
        
        Args:
            filepath: Path to export to
        """
        with open(os.path.expanduser(filepath), 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def import_config(self, filepath: str):
        """
        Import configuration from a file.
        
        Args:
            filepath: Path to import from
        """
        with open(os.path.expanduser(filepath), 'r') as f:
            imported = json.load(f)
            self.config.update(imported)
            self._save()
    
    def __repr__(self) -> str:
        return f"ConfigManager(file={self.config_file})"


# =============================================================================
# Global Config Manager
# =============================================================================

_global_config: Optional[ConfigManager] = None


def get_config(config_file: Optional[str] = None, local: bool = False) -> ConfigManager:
    """Get or create the global config manager."""
    global _global_config
    if _global_config is None:
        _global_config = ConfigManager(config_file, local)
    return _global_config


def reset_config():
    """Reset the global config manager."""
    global _global_config
    _global_config = None


# =============================================================================
# CLI Helpers
# =============================================================================

def print_config(key: Optional[str] = None):
    """Print configuration value(s)."""
    config = get_config()
    
    if key:
        value = config.get(key)
        print(f"{key} = {json.dumps(value, indent=2)}")
    else:
        print(json.dumps(config.get_all(), indent=2))


def set_config(key: str, value: str):
    """
    Set a configuration value from CLI.
    
    Handles type conversion for common types.
    """
    config = get_config()
    
    # Try to parse as JSON for proper types
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        # Keep as string
        parsed = value
    
    config.set(key, parsed)
    print(f"Set {key} = {json.dumps(parsed)}")


def list_config_keys():
    """List all configuration keys."""
    config = get_config()
    
    def print_keys(d: Dict, prefix: str = ""):
        for key, value in d.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                print_keys(value, full_key)
            else:
                print(full_key)
    
    print_keys(DEFAULT_CONFIG)
