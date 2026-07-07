# EITElite -- AI Agent Platform
# Copyright (C) 2026 zizetu
# Original repository: https://github.com/zizetu/eite-agent
#
# Licensed under AGPLv3. See LICENSE for details.
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
    name: str = "seoul",
    hostname: str = "",
    deploy_path: str = "",
    target_model: str = "",
    active_modules: Optional[Dict[str, Any]] = None,
    platform: str = "",
    cognitive_workspace: Any = None,
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
        cognitive_workspace: Optional Workspace for cognitive state injection.
    """
    parts = [
        f"You are {name}, an autonomous agent on {hostname or 'this node'} ({target_model or 'unknown model'}). "
        "You help with questions, code, analysis, and system tasks. "
        "Reply clearly and directly. Be useful, not verbose."
    ]

    # Inject cognitive workspace summary
    if cognitive_workspace is not None:
        try:
            ws_summary = cognitive_workspace.get_summary()
            if ws_summary:
                parts.append(ws_summary)
        except Exception:
            pass

    # Operating rules
    parts.append(
        "## Operating rules\n"
        "- Call tools to act -- do not just say what you will do.\n"
        "- Keep going until the task is done. Do not stop after one step.\n"
        "- Read enough files to answer, then reply. Do not read the entire codebase.\n"
        "- Never make up data. If something fails, report the failure.\n"
        "- IMPORTANT: The user may send short Chinese commands. Interpret them as DIRECT ORDERS:\n"
        "  * '\u56de\u7b54' = Reply/answer the previous message or question (not 'you said answer')\n"
        "  * '\u505a\u554a' / '\u505a\u5427' = Execute the plan NOW, no confirmation needed\n"
        "  * '\u5168\u4fee' = Fix ALL issues identified, not just one\n"
        "  * '\u7ee7\u7eed' = Continue working on the current task without stopping\n"
        "  * '\u4fee\u5b8c\u6ca1\u6709' = Binary answer: 'done' or 'not done yet'\n"
        "  * '\u72b6\u6001' / 'status' = Report current system status\n"
        "  * '\u91cd\u542f' / 'restart' = Restart the service\n"
        "- MEMORY RECALL: When a user asks about past conversations, previous tasks,\n"
        "  'do you remember', or references something from before — ALWAYS call\n"
        "  memory_search FIRST to look up relevant context. Do not say 'I don't know'\n"
        "  without searching. The memory_search tool searches all past conversations\n"
        "  and memory files (SOUL.md, MEMORY.md, USER.md)."
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
