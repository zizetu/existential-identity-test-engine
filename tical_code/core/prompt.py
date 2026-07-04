# Existential Identity Test Engine (EITE) — AI Agent Evaluation Framework
# Copyright (C) 2026 zizetu
# Repository: https://github.com/zizetu/existential-identity-test-engine
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""System prompt builder — concise, independent."""
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("EITElite.prompt")


def _build_tool_descriptions() -> List[str]:
    """Build tool descriptions dynamically from TOOL_SCHEMAS."""
    try:
        from tical_code.core.tool_executor import TOOL_SCHEMAS
    except ImportError:
        return []

    lines: List[str] = []
    seen_names: set = set()
    for entry in TOOL_SCHEMAS:
        func = entry.get("function", {})
        name = func.get("name", "")
        if name in seen_names:
            continue
        seen_names.add(name)
        desc = func.get("description", "")
        if desc:
            summary = desc.split(".")[0].strip()
            lines.append(f"  {name}: {summary}")
        else:
            lines.append(f"  {name}")
    return lines


def build_system_prompt(
    name: str = "agent",
    hostname: str = "",
    deploy_path: str = "",
    target_model: str = "",
    active_modules: Optional[Dict[str, Any]] = None,
    platform: str = "",
) -> str:
    """Build system prompt.

    Args:
        name: Agent identity name.
        hostname: Machine hostname for context.
        deploy_path: Workspace path.
        target_model: Model name string.
        active_modules: Dict of enabled modules from registry.
        platform: Optional platform name for formatting hints
            ('telegram', 'wechat', 'cli', 'tical-chat', or '' for generic).
    """
    parts = [
        f"You are {name}, an autonomous agent on {hostname or 'this node'} ({target_model or 'unknown model'}). "
        "You help with questions, code, analysis, and system tasks. "
        "Reply clearly and directly. Be useful, not verbose."
    ]

    # Operating rules
    parts.append(
        "## Operating rules\n"
        "- Call tools to act -- do not just say what you will do.\n"
        "- Keep going until the task is done. Do not stop after one step.\n"
        "- Read enough files to answer, then reply. Do not read the entire codebase.\n"
        "- Never make up data. If something fails, report the failure."
    )

    # Reply Protocol -- structured reply rules
    from tical_code.core.reply_defs import REPLY_PROTOCOL, get_platform_section
    parts.append(REPLY_PROTOCOL)

    # Platform-specific formatting hints (optional)
    if platform:
        plat_section = get_platform_section(platform)
        if plat_section:
            parts.append(plat_section)

    # Available tools
    tools = _build_tool_descriptions()
    if tools:
        parts.append("## Tools\n" + "\n".join(tools))

    return "\n\n".join(parts)


def build_power_mode_suffix(name: str = "worker") -> str:
    return ""


def strip_and_inject_power_mode(prompt: str, name: str = "worker") -> str:
    return prompt
