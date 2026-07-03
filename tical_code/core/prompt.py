# tical-code -- AI Agent Platform
# Copyright (C) 2026 zizetu
# Original repository: https://github.com/zizetu/eite-agent
#
# Built on ticalasi.cloud — Seoul / Oracle / Test mesh. Independent system,
# not a fork of any other agent framework. See https://ticalasi.cloud
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

logger = logging.getLogger("tical-code.prompt")


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
    name: str = "seoul",
    hostname: str = "",
    deploy_path: str = "",
    target_model: str = "",
    active_modules: Optional[Dict[str, Any]] = None,
) -> str:
    """Build system prompt."""
    parts = [
        f"You are {name}, an autonomous agent on {hostname or 'this node'} ({target_model or 'unknown model'}). "
        "You help with questions, code, analysis, and system tasks. "
        "Reply clearly and directly. Be useful, not verbose."
    ]

    # Tool discipline
    parts.append(
        "## Operating rules\n"
        "- Call tools to act — do not just say what you will do.\n"
        "- When you promise an action ('I will check', 'let me read'), call the tool in the same message.\n"
        "- Keep going until the task is done. Do not stop after one step.\n"
        "- Each response must either: (a) call tools and make progress, or (b) deliver the final result.\n"
        "- Read enough files to answer, then reply. Do not read the entire codebase.\n"
        "- Output real results from your tool calls. Format findings with lists or tables.\n"
        "- Never make up data. If something fails, report the failure."
    )

    # Available tools
    tools = _build_tool_descriptions()
    if tools:
        parts.append("## Tools\n" + "\n".join(tools))

    return "\n\n".join(parts)


def build_power_mode_suffix(name: str = "ani") -> str:
    return ""


def strip_and_inject_power_mode(prompt: str, name: str = "ani") -> str:
    return prompt
