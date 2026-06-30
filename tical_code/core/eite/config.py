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

"""EITE config manager - load eite_config.json and provide global settings"""
import json
import os
from typing import Any, Dict

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".eite", "eite_config.json")
_OVERRIDE_PATH = os.path.join(os.path.dirname(__file__), "eite_override.json")
_config: Dict[str, Any] = {}

def load_config() -> Dict[str, Any]:
    global _config
    try:
        with open(_CONFIG_PATH, "r") as f:
            _config = json.load(f)
    except FileNotFoundError:
        _config = {}
    # allowlocaloverride
    if os.path.exists(_OVERRIDE_PATH):
        with open(_OVERRIDE_PATH, "r") as f:
            override = json.load(f)
        _config.update(override)
    return _config

def get(key: str, default=None):
    return _config.get(key, default)

def set(key: str, value):
    _config[key] = value

def save():
    with open(_CONFIG_PATH, "w") as f:
        json.dump(_config, f, indent=2)

load_config()
