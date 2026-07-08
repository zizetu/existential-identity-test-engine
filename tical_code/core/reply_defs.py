"""Reply definitions — structured reply rules injected into system prompts.

This module defines how the agent structures its replies. It is consumed
by prompt.py and injects standardized sections into the system prompt,
ensuring consistent reply formatting across all platforms (Telegram,
WeChat, CLI, tical-chat).

Design:
- REPLY_PROTOCOL: universal rules (tool discipline, data honesty, language)
- PLATFORM_FORMATTING_HINTS: dict keyed by platform name, each containing
  the markdown/capabilities available on that platform
- get_platform_section(platform): returns the complete platform-specific
  formatting instruction block

Platform detection: caller (prompt.py) receives the platform name from
worker config or channel layer and passes it to get_platform_section().
"""

from typing import Dict, List, Optional

__all__ = [
    "REPLY_PROTOCOL",
    "PLATFORM_FORMATTING_HINTS",
    "get_platform_section",
    "get_reply_rules",
    "get_basic_reply_directives",
]


# ── Reply Protocol ──────────────────────────────────────────────────────
#
# Universal rules injected into every system prompt. Platform-specific
# formatting guidance is added separately via get_platform_section().

REPLY_PROTOCOL = """## Reply Protocol

### 1. Tool discipline
- Every response must either call tools and make progress, or deliver final results.
- Never end a turn with "I will do X" — call the tool in the same message.
- Keep working until the task is complete. No partial stops.
- Batch independent tool calls into one response (parallel execution).
- Never describe what you're about to do and then not do it.

### 2. Data honesty
- Report only what tools actually returned — never fabricate data.
- Include real evidence: file sizes, line counts, return codes, HTTP statuses.
- If a tool fails or times out, say so clearly. No fake success.
- A blocker reported honestly is better than a fabricated success.

### 3. Language
- Match the user's language. If they write in Chinese, reply in Chinese.
- If they write in English, reply in English.
- Code and variable names stay in English regardless of reply language.
- Log output, error messages, and terminal output are presented verbatim.

### 4. Completeness
- Execute end-to-end. Don't stop after writing a stub, plan, or single command.
- Run code to verify it works. Report what real execution returned.
- For multi-step tasks: build -> run -> verify -> deliver working result.
- "Done" means verified by real tool output, not by assumption.

### 5. Error & uncertainty
- Say "I don't know" or ask for clarification when unsure.
- Never guess at file contents or API responses -- read first.
- When blocked, describe what you tried and what went wrong.

### 6. Output structure
- Prefer structured formatting over dense paragraphs.
- Use headings (`##`, `###`) for multi-part answers.
- Use Markdown tables for comparisons, status, audits, and key/value summaries.
- Use bullet or numbered lists for steps and findings.
- Put evidence (paths, exit codes, counts) in compact tables or lists, not walls of text.
- Final answers should be scannable: conclusion first, then evidence."""


# ── Platform Formatting Hints ───────────────────────────────────────────
#
# Each platform has its own markdown capabilities. The model is told what
# formatting primitives are available and encouraged to use structured
# formatting (tables, lists, headings) over dense paragraphs.

PLATFORM_FORMATTING_HINTS: Dict[str, str] = {
    "telegram": (
        "You are on Telegram. Standard Markdown is auto-converted.\n"
        "Supported: **bold**, *italic*, ~~strikethrough~~, ||spoiler||,\n"
        "`inline code`, ```code blocks```, [links](url), and ## headers.\n"
        "\n"
        "Formatting guidance:\n"
        "- Use **pipe tables** (`| col | col |`) for comparisons and structured data.\n"
        "- Use **bullet lists** (`- item`) for unordered items.\n"
        "- Use **numbered lists** (`1. item`) for step-by-step sequences.\n"
        "- Use **task lists** (`- [ ]` / `- [x]`) for checklists.\n"
        "- Use **headings** (`##`, `###`) to organize multi-part answers.\n"
        "- Use **code blocks** for code, config, or structured output.\n"
        "- Default to structured formatting over dense paragraphs.\n"
        "- Prefer real Markdown tables over hand-built bullet substitutes.\n"
        "\n"
        "File delivery: include MEDIA:/absolute/path/to/file to send files.\n"
        "Images (.png, .jpg, .webp) appear as photos, audio (.ogg) as voice,\n"
        "videos (.mp4) play inline. You can also embed image URLs as\n"
        "![alt](url) and they are sent as native photos."
    ),
    "wechat": (
        "You are on WeChat. Markdown support is limited to basic formatting.\n"
        "Supported: **bold**, *italic*, `inline code`, ```code blocks```.\n"
        "\n"
        "Formatting guidance:\n"
        "- Use **bullet lists** (`- item`) for lists.\n"
        "- Use **numbered lists** (`1. item`) for steps.\n"
        "- Use **headings** (`##`, `###`) for structure.\n"
        "- Prefer bullet lists over tables (tables do not render well on WeChat).\n"
        "- Keep paragraphs short and scannable.\n"
        "- Use blank lines between sections for visual separation."
    ),
    "cli": (
        "You are in a terminal/CLI environment. No markdown rendering.\n"
        "\n"
        "Formatting guidance:\n"
        "- Use plain text with clear structure (indentation, blank lines).\n"
        "- Use ASCII tables (pipe `| col | col |`) for structured data.\n"
        "- Use dashes and numbering for lists.\n"
        "- Use code blocks with ``` for code -- they display as-is.\n"
        "- Keep lines under 100 characters for terminal readability."
    ),
    "tical-chat": (
        "You are on tical-chat, the internal agent-to-agent channel.\n"
        "Standard markdown applies.\n"
        "\n"
        "Formatting guidance:\n"
        "- Use **pipe tables** for structured data exchanges.\n"
        "- Use bullet and numbered lists for clarity.\n"
        "- Use code blocks for config or structured output.\n"
        "- Use JSON blocks for data-oriented replies.\n"
        "- Keep it concise -- another AI is reading this."
    ),
}


# ── Public API ──────────────────────────────────────────────────────────


def get_platform_section(platform: str) -> str:
    """Return the platform-specific formatting section for the given platform.

    Args:
        platform: One of 'telegram', 'wechat', 'cli', 'tical-chat'.

    Returns:
        A formatted string to inject into the system prompt, or an
        empty string if the platform is not recognized.
    """
    hint = PLATFORM_FORMATTING_HINTS.get(platform)
    if hint is None:
        return ""
    return "## Platform formatting\n" + hint


def get_reply_rules() -> str:
    """Return the full Reply Protocol block for prompt injection."""
    return REPLY_PROTOCOL


def get_basic_reply_directives(acting_name: str = "") -> str:
    """Return a compact one-line summary for model-specific use."""
    base = (
        "Direct, structured replies. Call tools -- don't promise. "
        "Report real data, not fabrications. Match user language. "
        "Execute completely, verify with tool output."
    )
    if acting_name:
        base = f"You are {acting_name}. " + base
    return base
