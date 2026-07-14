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

"""Auto-discover LLM providers from common environment variables.

Scans well-known API key env vars and builds provider definitions on the fly,
so providers.json is optional. New users can just set DEEPSEEK_API_KEY or
OPENAI_API_KEY and start working immediately.
"""

import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger("EITElite.provider_autodiscover")

# Well-known env vars mapped to provider definitions
_KNOWN_PROVIDERS: List[Dict] = [
    {
        "name": "deepseek",
        "family": "deepseek",
        "env_key": "DEEPSEEK_API_KEY",
        "env_base_url": "DEEPSEEK_ENDPOINT",
        "default_base_url": "https://api.deepseek.com/v1/chat/completions",
        "auth_style": "bearer",
        "default_model": "deepseek-chat",
        "protocol": "openai",
        "priority": 1,
        "cost": "paid",
    },
    {
        "name": "openai",
        "family": "openai",
        "env_key": "OPENAI_API_KEY",
        "env_base_url": "OPENAI_ENDPOINT",
        "default_base_url": "https://api.openai.com/v1/chat/completions",
        "auth_style": "bearer",
        "default_model": "gpt-4o",
        "protocol": "openai",
        "priority": 2,
        "cost": "paid",
    },
    {
        "name": "openrouter",
        "family": "openrouter",
        "env_key": "OPENROUTER_API_KEY",
        "env_base_url": None,
        "default_base_url": "https://openrouter.ai/api/v1/chat/completions",
        "auth_style": "bearer",
        "default_model": "openai/gpt-oss-120b:free",
        "protocol": "openai",
        "priority": 3,
        "cost": "free",
    },
    {
        "name": "anthropic",
        "family": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "env_base_url": "ANTHROPIC_ENDPOINT",
        "default_base_url": "https://api.anthropic.com/v1/messages",
        "auth_style": "x-api-key",
        "default_model": "claude-sonnet-4-20250514",
        "protocol": "anthropic",
        "priority": 4,
        "cost": "paid",
    },
    {
        "name": "gemini",
        "family": "gemini",
        "env_key": "GEMINI_API_KEY",
        "env_base_url": "GEMINI_ENDPOINT",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "auth_style": "query-key",
        "default_model": "gemini-2.5-flash",
        "protocol": "gemini",
        "priority": 5,
        "cost": "free",
    },
    {
        "name": "custom-llm",
        "family": "openai",
        "env_key": "CUSTOM_LLM_API_KEY",
        "env_base_url": "CUSTOM_LLM_ENDPOINT",
        "default_base_url": None,
        "auth_style": "bearer",
        "default_model": "default",
        "protocol": "openai",
        "priority": 10,
        "cost": "paid",
    },
]


def auto_discover() -> List[Dict]:
    """Scan environment variables and return discovered provider definitions.

    Returns a list of provider definition dicts (same format as
    providers.json entries) for every well-known API key that is set
    in the environment. Skips providers whose env_key is not set.
    """
    discovered: List[Dict] = []
    for pdef in _KNOWN_PROVIDERS:
        key = _resolve_env(pdef.get("env_key"))
        if key:
            discovered.append(dict(pdef))
            logger.info(
                "Auto-discovered provider '%s' via %s",
                pdef["name"],
                pdef["env_key"],
            )
    return discovered


def _resolve_env(env_key: Optional[str]) -> str:
    if env_key:
        return os.environ.get(env_key, "")
    return ""
