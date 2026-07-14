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
# Original repository: https://github.com/zizetu/existential-identity-test-engine
#

"""
Skill Loader - loads extracted skills into the evaluation system prompt.

Part of the EITE evaluation framework. On worker initialization, scans
~/.eite/skills/ for saved skills and injects summaries into the system
prompt. Full skill content is loaded on-demand when a task matches a
skill's tool pattern.

This is the loading half of the auto-evolving skill system.
The extraction half is in skill_extractor.py.

EITE Version: 1.0.0
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("eite.skills")

SKILLS_DIR = Path.home() / ".eite" / "skills"


class SkillLoader:
    """Loads and manages auto-extracted skills for the EITE evaluation framework."""

    def __init__(self, max_in_prompt: int = 5, llm=None):
        self._max_in_prompt = max_in_prompt
        self._llm = llm
        self._index: Dict[str, Dict[str, Any]] = {}
        self._loaded: List[Dict[str, Any]] = []
        self._refresh_index()

    def set_llm(self, llm):
        """Set or update the LLM backend for semantic skill matching."""
        self._llm = llm

    def _refresh_index(self):
        """Reload the skill index from disk."""
        index_path = SKILLS_DIR / "index.json"
        if not index_path.exists():
            self._index = {}
            return
        try:
            self._index = json.loads(index_path.read_text())
        except Exception:
            self._index = {}

    def get_prompt_injection(self) -> str:
        """Build a compact skill summary for the system prompt.

        Only includes the most recently used and most frequently used skills.
        Keeps the injection small to avoid prompt bloat.
        """
        if not self._index:
            return ""

        # Sort by (use_count desc, last_used_at desc)
        skills = sorted(
            self._index.values(),
            key=lambda s: (s.get("use_count", 0), s.get("last_used_at", 0)),
            reverse=True,
        )

        top = skills[:self._max_in_prompt]
        if not top:
            return ""

        lines = [
            "",
            "## Learned Skills (auto-extracted from past tasks)",
            "",
            "These workflows were saved from completed evaluation tasks. When a new task",
            "matches a skill's pattern, use it as a starting point -- do not rediscover",
            "from scratch.",
            "",
        ]

        for s in top:
            name = s.get("name", "unknown")
            desc = s.get("description", "")[:120]
            tools = s.get("tool_count", 0)
            use = s.get("use_count", 0)
            lines.append(
                f"- **{name}**: {desc} ({tools} tool calls, used {use}x)"
            )

        lines.append("")
        return "\n".join(lines)

    def _llm_match(self, task_goal: str,
                   candidates: List[Tuple[str, str]]) -> Optional[str]:
        """Use LLM to find the most semantically relevant skill for a task.

        Args:
            task_goal: The user's task description.
            candidates: List of (name, description) tuples from the skill index.

        Returns:
            Matched skill name, or None if no match found or LLM unavailable.
        """
        if not self._llm or not candidates:
            return None

        # Build a compact candidate list for the prompt
        candidate_lines = []
        for name, desc in candidates:
            short_desc = (desc or "")[:150]
            candidate_lines.append(f"- {name}: {short_desc}")
        candidate_text = "\n".join(candidate_lines)

        prompt = (
            f"Task: {task_goal}\n\n"
            f"Available skills:\n{candidate_text}\n\n"
            "Which skill is most relevant to this task? "
            "Reply with ONLY the skill name, or 'none' if no skill "
            "matches semantically."
        )

        messages = [{"role": "user", "content": prompt}]

        try:
            result = self._llm.call(messages, tools=None, max_tokens=50)
        except Exception:
            return None

        content = result.get("content", "").strip().lower()

        if not content or content == "none" or "none" in content:
            return None

        # Find which candidate name appears in the LLM response
        for name, _ in candidates:
            if name.lower() in content:
                return name

        # LLM response didn't map to any known skill name
        return None

    def find_matching_skill(self, task_goal: str) -> Optional[Dict[str, Any]]:
        """Find the most relevant skill for a task goal.

        Uses LLM-driven semantic matching when an LLM backend is available,
        falling back to keyword matching when no LLM is configured.

        Returns the full skill content if a match is found.
        """
        # Try LLM-driven semantic matching first
        if self._llm:
            candidates = [
                (name, meta.get("description", ""))
                for name, meta in self._index.items()
            ]
            matched_name = self._llm_match(task_goal, candidates)
            if matched_name and matched_name in self._index:
                return self._load_full_skill(matched_name)

        # Fallback: keyword matching (original logic)
        goal_lower = task_goal.lower()
        best_score = 0
        best_skill = None

        for name, meta in self._index.items():
            score = 0
            # Check name keywords
            name_words = set(name.replace("-", " ").split())
            for word in name_words:
                if word in goal_lower and len(word) > 2:
                    score += 3
            # Check description keywords
            desc = meta.get("description", "").lower()
            desc_words = set(desc.split())
            for word in desc_words:
                if len(word) > 3 and word in goal_lower:
                    score += 1

            if score > best_score:
                best_score = score
                best_skill = meta

        if best_skill and best_score >= 3:
            return self._load_full_skill(best_skill["name"])
        return None

    def _load_full_skill(self, name: str) -> Optional[Dict[str, Any]]:
        """Load the full markdown content of a skill."""
        skill_path = SKILLS_DIR / f"{name}.md"
        if not skill_path.exists():
            return None
        try:
            content = skill_path.read_text()
            # Update usage stats
            self._record_use(name)
            return {"name": name, "content": content}
        except Exception:
            return None

    def _record_use(self, name: str):
        """Record that a skill was used, incrementing its use count.

        Re-reads the index from disk before writing to avoid overwriting
        skills added by other components (extractor, curator) since boot.
        """
        # Re-read index from disk to pick up changes from extractor/curator
        self._refresh_index()

        if name not in self._index:
            return
        self._index[name]["use_count"] = self._index[name].get("use_count", 0) + 1
        self._index[name]["last_used_at"] = time.time()

        # Save updated index
        index_path = SKILLS_DIR / "index.json"
        try:
            index_path.write_text(json.dumps(self._index, indent=2))
        except Exception as e:
            logger.debug("SkillLoader: failed to persist use_count: %s", e)

    def get_skill_count(self) -> int:
        """Return the number of saved skills."""
        return len(self._index)

    def get_recent_skills(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return recently used skills."""
        skills = sorted(
            self._index.values(),
            key=lambda s: s.get("last_used_at", 0),
            reverse=True,
        )
        return skills[:limit]
