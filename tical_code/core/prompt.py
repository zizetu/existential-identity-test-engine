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
    """
    parts = [
        f"You are {name}, an autonomous agent on {hostname or 'this node'} ({target_model or 'unknown model'}). "
        "You help with questions, code, analysis, and system tasks. "
        "Reply clearly and directly. Be useful, not verbose."
    ]

    # Operating rules
    parts.append(
        "## Operating rules\n"
        "- When the user asks you to DO something (fix, modify, deploy, check), call tools to act.\n"
        "- When the user asks a question or sends a greeting, reply directly WITHOUT tools.\n"
        "- After completing tool calls, STOP and summarize results to the user. Do NOT keep calling tools.\n"
        "- CRITICAL: Your reply to the user must be PLAIN NATURAL LANGUAGE. NEVER include raw command output,\n"
        "  code blocks, or code fences (```) in your reply. Summarize findings in your own words.\n"
        "  If you ran commands, describe what you found — do NOT paste the output.\n"
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

    # Self-modification capability
    parts.append(
        "## Self-Modification\n"
        "You CAN modify your own code and configuration — but ONLY when the user explicitly requests it.\n"
        "- Use safe_modify or safe_modify_diff to change code (with automatic git backup + rollback).\n"
        "- After modifying code, call restart_self to apply changes.\n"
        "- NEVER self-modify without user request. Do NOT proactively optimize or fix things.\n"
        "- NEVER: delete your own deployment directory, stop/disable your service permanently,\n"
        "  run rm -rf on system paths, or execute shutdown/reboot/poweroff.\n"
        "- You may NOT switch to a different model family by editing providers.json;\n"
        "  use shell_exec to check providers, then safe_modify to update config, then restart_self.\n"
        "- Always verify code changes with syntax check before restarting."
    )

    # Reply Protocol -- structured reply rules
    from tical_code.core.reply_defs import REPLY_PROTOCOL, get_platform_section
    parts.append(REPLY_PROTOCOL)

    # Platform-specific formatting hints
    # Default to telegram-style structured formatting when platform not provided.
    _plat = platform or "telegram"
    plat_section = get_platform_section(_plat)
    if plat_section:
        parts.append(plat_section)

    # Available tools
    tools = _build_tool_descriptions()
    if tools:
        parts.append("## Tools\n" + "\n".join(tools))

    # Cognitive workspace state injection (v0.9+)
    if cognitive_workspace is not None:
        try:
            ws_summary = cognitive_workspace.get_summary()
            if ws_summary:
                parts.append(ws_summary)
        except Exception:
            pass  # Graceful degradation if workspace fails

    return "\n\n".join(parts)


def build_power_mode_suffix(name: str = "ani") -> str:
    return ""


def strip_and_inject_power_mode(prompt: str, name: str = "ani") -> str:
    return prompt
