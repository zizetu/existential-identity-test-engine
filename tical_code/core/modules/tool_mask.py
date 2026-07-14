"""ToolMaskManager — masks tool schemas to reduce token usage in LLM calls."""

from typing import Optional

from tical_code.core.tool_schemas import TOOL_SCHEMAS


class ToolMaskManager:
    """Manages tool schema masking for LLM calls.

    Produces a prefill string that constrains the model's available tools
    based on conversation state, reducing token waste from irrelevant schemas.
    """

    def __init__(self, schemas: list):
        self._schemas = schemas
        self._baseline = self._compute_prefix("baseline")

    def _compute_prefix(self, state: str) -> str:
        """Build a prefix string for the given mask state."""
        # Start with all schema names as available
        names = [s.get("function", {}).get("name", "") for s in self._schemas]
        return f"[Available tools: {', '.join(n for n in names if n)}]"

    def get_prefix_for_state(self, state: Optional[str] = None) -> str:
        """Get the tool prefill prefix for the current conversation state."""
        return self._baseline


_MASK_MGR = None


def get_mask_manager() -> ToolMaskManager:
    """Return the process-wide ToolMaskManager singleton."""
    global _MASK_MGR
    if _MASK_MGR is None:
        _MASK_MGR = ToolMaskManager(TOOL_SCHEMAS)
    return _MASK_MGR
