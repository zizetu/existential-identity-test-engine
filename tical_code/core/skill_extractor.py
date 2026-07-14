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
Skill Extractor - auto-generates SKILL.md from completed task workflows.

When a task finishes with 5+ tool calls, the extractor analyzes the tool-call
sequence and distills it into a reusable skill document. This is the mechanism
that makes EITElite compound over time instead of starting from zero each session.

Skills are saved to ~/.EITElite/skills/<name>.md and loaded on next startup.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("EITElite.skills")


# Minimum tool calls before a workflow is considered skill-worthy
MIN_TOOL_CALLS_FOR_SKILL = 5

# Maximum skills to keep (oldest unused get pruned)
MAX_SKILLS = 50

# Skills directory
SKILLS_DIR = Path.home() / ".EITElite" / "skills"


@dataclass
class ToolCallRecord:
    """Record of a single tool call during a task."""
    name: str
    args: Dict[str, Any]
    result_summary: str  # first 200 chars of result
    timestamp: float = field(default_factory=time.time)
    is_error: bool = False

    def fingerprint(self) -> str:
        """Generate a fingerprint from the tool name + key args, ignoring specifics."""
        fp_parts = [self.name]
        for key in sorted(self.args.keys()):
            val = str(self.args[key])
            # Replace specific values with placeholders
            val = re.sub(r'/[^\s"]+', '/<path>', val)
            val = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '<ip>', val)
            val = re.sub(r'\b[0-9a-f]{8,}\b', '<hash>', val)
            fp_parts.append(f"{key}={val}")
        return "|".join(fp_parts)


@dataclass
class SkillWorkflow:
    """Extracted workflow pattern from a completed task."""
    name: str
    description: str
    tool_sequence: List[str]  # ordered list of tool names used
    steps: List[str]  # human-readable step descriptions
    modules_used: List[str]  # registry module names involved
    tool_count: int
    source_task_id: str
    fingerprint: str  # dedup hash
    created_at: float = field(default_factory=time.time)
    use_count: int = 0
    last_used_at: float = 0.0
    version: int = 1
    quality_score: float = 0.0
    _is_failure_pattern: bool = False


class SkillExtractor:
    """Watches tool calls during a task and extracts skills on completion."""

    def __init__(self, workspace: str = ".", enabled: bool = True, llm=None):
        self._workspace = Path(workspace)
        self._enabled = enabled
        self._llm = llm
        self._records: List[ToolCallRecord] = []
        self._task_id: Optional[str] = None
        self._task_goal: str = ""
        self._error_count: int = 0
        self._retry_count: int = 0
        self._task_start_time: float = 0.0
        self._last_error: str = ""
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    def set_llm(self, llm):
        """Set or replace the LLM backend for workflow summarization.

        Allows late binding when the LLM is not available at construction time.
        """
        self._llm = llm

    def start_task(self, task_id: str, goal: str):
        """Begin tracking tool calls for a new task.

        Resets all quality metrics (error count, retries, timing, last error)
        so each task is measured independently.
        """
        self._task_id = task_id
        self._task_goal = goal
        self._records = []
        self._error_count = 0
        self._retry_count = 0
        self._task_start_time = time.time()
        self._last_error = ""

    def record_tool_call(self, name: str, args: Dict[str, Any], result: str = "",
                         is_error: bool = False, is_retry: bool = False):
        """Record a tool call. Set is_error=True to track errors for quality scoring."""
        if not self._enabled:
            return
        if is_error:
            self._error_count += 1
            self._last_error = str(result)[:200]
        if is_retry:
            self._retry_count += 1
        self._records.append(ToolCallRecord(
            name=name,
            args=args,
            result_summary=str(result)[:200],
            is_error=is_error,
        ))

    def end_task(self, success: bool) -> Optional[SkillWorkflow]:
        """Called when a task completes. Extracts skill if conditions met.

        Applies a quality gate based on error rate and extracts anti-patterns
        from failed tasks to enable failure learning.
        """
        if not self._enabled or not self._task_id:
            return None

        tool_count = len(self._records)
        if tool_count < MIN_TOOL_CALLS_FOR_SKILL:
            logger.debug("SkillExtractor: %d tool calls (need %d), skipping",
                         tool_count, MIN_TOOL_CALLS_FOR_SKILL)
            self._records = []
            return None

        error_rate = self._error_count / max(tool_count, 1)

        if not success:
            # Extract anti-pattern from failed task for failure learning.
            # Anti-patterns document what went wrong so the AI can avoid
            # repeating the same mistakes. They bypass the quality gate
            # because high error rates are expected in failure scenarios.
            workflow = self._extract_anti_pattern()
            if workflow is not None:
                if not self._is_duplicate(workflow):
                    self._save_skill(workflow)
                    logger.info(
                        "SkillExtractor: saved anti-pattern '%s' "
                        "(errors=%d, retries=%d, error_rate=%.2f)",
                        workflow.name, self._error_count, self._retry_count,
                        error_rate)
                else:
                    logger.debug("SkillExtractor: duplicate anti-pattern, skipping")
            self._records = []
            return workflow

        # Quality gate: skip extraction if error rate is too high.
        if error_rate > 0.3:
            logger.debug("SkillExtractor: error rate %.2f too high for quality "
                         "gate, skipping skill extraction", error_rate)
            self._records = []
            return None

        # Generate workflow pattern
        workflow = self._extract_workflow()
        if workflow is None:
            self._records = []
            return None

        # Check if this workflow already exists (dedup)
        if self._is_duplicate(workflow):
            logger.debug("SkillExtractor: duplicate workflow, skipping")
            self._records = []
            return None

        # LLM quality reflection: ask if this workflow is genuinely reusable.
        # Lightweight - ~50 tokens in, ~3 tokens out.
        if self._llm and not self._llm_reflection_pass(workflow):
            logger.info("SkillExtractor: LLM reflection rejected '%s' (not generalizable)",
                         workflow.name)
            self._records = []
            return None

        # Save to disk
        self._save_skill(workflow)
        logger.info("SkillExtractor: saved skill '%s' v%d (%d tool calls, "
                     "quality=%.2f)",
                     workflow.name, workflow.version, workflow.tool_count,
                     workflow.quality_score)

        self._records = []
        return workflow

    def _extract_workflow(self) -> Optional[SkillWorkflow]:
        """Analyze tool-call records and extract a workflow pattern."""
        if not self._records:
            return None
        name = self._derive_skill_name()
        quality_mult = min(1.0, len(self._records) / 10.0)
        return self._build_workflow(name, self._task_goal, quality_mult)

    def _extract_anti_pattern(self) -> Optional[SkillWorkflow]:
        """Extract an anti-pattern skill from a failed task for failure learning.

        Anti-patterns capture what went wrong so the AI can avoid repeating
        the same mistakes. Saved with ``WARNING-failed-`` prefix and
        ``_is_failure_pattern`` flag.
        """
        if not self._records:
            return None
        name = f"WARNING-failed-{self._derive_skill_name()}"
        description = (
            f"FAILED TASK: {self._task_goal[:160]}. "
            f"Last error: {self._last_error}"
        )
        return self._build_workflow(name, description, 0.5, True)

    def _build_workflow(self, name: str, description: str,
                        quality_multiplier: float = 1.0,
                        is_failure_pattern: bool = False) -> SkillWorkflow:
        """Build a SkillWorkflow from current records (shared by success and anti-pattern paths)."""
        fingerprints = [r.fingerprint() for r in self._records]
        tool_names = [r.name for r in self._records]
        dedup_hash = hashlib.sha256(
            "|".join(fingerprints).encode()).hexdigest()

        steps = self._llm_summarize()
        modules_used = self._detect_modules()

        total = len(self._records)
        error_rate = self._error_count / max(total, 1)
        quality_score = round((1.0 - error_rate) * quality_multiplier, 4)

        return SkillWorkflow(
            name=name,
            description=description[:200],
            tool_sequence=list(dict.fromkeys(tool_names)),
            steps=steps,
            modules_used=modules_used,
            tool_count=total,
            source_task_id=self._task_id or "unknown",
            fingerprint=dedup_hash,
            quality_score=quality_score,
            _is_failure_pattern=is_failure_pattern,
        )

    def _derive_skill_name(self) -> str:
        """Derive a descriptive skill name from the task goal."""
        goal = self._task_goal.lower()
        # Extract key action words
        action_words = {
            "deploy": "deploy", "install": "install", "configure": "configure",
            "fix": "fix", "debug": "debug", "restart": "restart",
            "update": "update", "migrate": "migrate", "backup": "backup",
            "monitor": "monitor", "check": "check", "scan": "scan",
            "build": "build", "test": "test", "clean": "clean",
            "setup": "setup", "init": "init", "sync": "sync",
        }
        found_actions = []
        for word, action in action_words.items():
            if word in goal:
                found_actions.append(action)
        if not found_actions:
            found_actions = ["task"]

        # Extract target nouns
        nouns = re.findall(r'\b([a-z]{3,20})\b', goal)
        # Filter out common stop words
        stop = {"the", "and", "for", "all", "that", "this", "with", "from"}
        targets = [n for n in nouns if n not in stop and n not in found_actions][:2]

        if targets:
            return f"{'-'.join(found_actions[:1])}-{'-'.join(targets[:2])}"
        return f"{'-'.join(found_actions[:2])}"

    def _derive_steps(self) -> List[str]:
        """Derive human-readable steps from the tool-call trace (mechanical fallback)."""
        steps = []
        for i, r in enumerate(self._records):
            step_num = i + 1
            if r.name == "bash":
                cmd = r.args.get("command", "")[:60]
                steps.append(f"{step_num}. Run: `{cmd}`")
            elif r.name in ("file_read", "file_write", "file_patch"):
                path = r.args.get("path", "?")
                action = "Read" if r.name == "file_read" else "Write" if r.name == "file_write" else "Patch"
                steps.append(f"{step_num}. {action}: {path}")
            elif r.name == "web_fetch":
                url = r.args.get("url", "?")[:60]
                steps.append(f"{step_num}. Fetch: {url}")
            elif r.name == "chat_send":
                target = r.args.get("target", "?")
                steps.append(f"{step_num}. Send message to {target}")
            else:
                steps.append(f"{step_num}. {r.name}({', '.join(r.args.keys())[:40]})")
        return steps

    def _llm_summarize(self) -> List[str]:
        """Summarize tool-call trace into steps via LLM, falling back to mechanical summary on failure."""
        if self._llm is None:
            return self._derive_steps()

        # Build a compact tool-call listing for the prompt
        tool_lines = []
        for i, r in enumerate(self._records):
            args_summary = ", ".join(
                f"{k}={str(v)[:40]}" for k, v in list(r.args.items())[:3]
            )
            line = f"  {i + 1}. {r.name}({args_summary})"
            if r.result_summary:
                line += f" -> {r.result_summary[:80]}"
            tool_lines.append(line)

        tool_list = "\n".join(tool_lines)

        prompt = (
            f"Task: {self._task_goal[:120]}\n\n"
            f"Tool calls executed:\n{tool_list}\n\n"
            f"Write {len(self._records)} numbered step descriptions summarizing "
            f"what each tool call accomplished in plain, human-readable language. "
            f"Output format:\n"
            f"1. <step description>\n"
            f"2. <step description>\n"
            f"..."
        )

        try:
            messages = [{"role": "user", "content": prompt}]
            response = self._llm.call(messages, tools=None, max_tokens=400)
            content = response.get("content", "")

            # Parse numbered steps from the response
            steps = []
            for line in content.split("\n"):
                line = line.strip()
                match = re.match(r"^\d+[\.\)]\s+(.+)", line)
                if match:
                    steps.append(f"{len(steps) + 1}. {match.group(1).strip()}")

            if len(steps) >= len(self._records) * 0.5:
                return steps[: len(self._records)]

            logger.warning(
                "SkillExtractor: LLM returned %d steps for %d records, falling back",
                len(steps),
                len(self._records),
            )
            return self._derive_steps()

        except Exception as e:
            logger.warning(
                "SkillExtractor: LLM summarization failed: %s, falling back", e
            )
            return self._derive_steps()

    def _llm_reflection_pass(self, workflow) -> bool:
        """Ask LLM whether this workflow is generalizable enough to save.

        Lightweight call - ~50 tokens prompt, ~3 tokens response.
        Returns True if the workflow passes the reflection gate (score >= 3).
        Always returns True when LLM is unavailable (non-blocking fallback).
        """
        if not self._llm:
            return True

        steps_text = "\n".join(workflow.steps[:3])
        prompt = (
            f"Is this workflow generalizable as a reusable skill? "
            f"Task: {workflow.description[:100]}\n"
            f"Steps: {steps_text[:200]}\n"
            f"Rate 1-5 (1=one-off, 5=highly reusable). Reply with ONLY the number."
        )

        try:
            messages = [{"role": "user", "content": prompt}]
            response = self._llm.call(messages, tools=None, max_tokens=10)
            content = response.get("content", "").strip()
            import re as _re
            match = _re.search(r"[1-5]", content)
            score = int(match.group(0)) if match else 3
            return score >= 3
        except Exception:
            return True

    def _detect_modules(self) -> List[str]:
        """Detect which registry modules were relevant to this workflow."""
        modules = set()
        tool_to_module = {
            "bash": "security_baseline",
            "file_read": "security_baseline",
            "file_write": "security_baseline",
            "file_patch": "security_baseline",
            "web_fetch": "security_baseline",
            "chat_send": "sessions",
            "memory_search": "memory_store",
            "memory_save": "memory_store",
            "check_self": "identity",
        }
        for r in self._records:
            mod = tool_to_module.get(r.name)
            if mod:
                modules.add(mod)
        return sorted(modules)

    def _is_duplicate(self, workflow: SkillWorkflow) -> bool:
        """Check if a workflow with the same fingerprint already exists."""
        existing = self._load_all_skills()
        for skill in existing:
            if skill.get("fingerprint") == workflow.fingerprint:
                return True
        return False

    def _save_skill(self, workflow: SkillWorkflow):
        """Save skill as a markdown file in the skills directory.

        If a skill with the same name already exists, increments the
        version number so the skill library tracks evolution over time.
        """
        # Version bump: if name already exists, increment version
        existing = self._load_all_skills()
        for s in existing:
            if s.get("name") == workflow.name:
                workflow.version = s.get("version", 1) + 1
                break

        filename = f"{workflow.name}.md"
        filepath = SKILLS_DIR / filename

        content = self._render_skill_md(workflow)
        try:
            filepath.write_text(content)
        except Exception as e:
            logger.error("SkillExtractor: failed to write skill file: %s", e)
            return

        # Update index
        self._update_index(workflow)

        # Prune old skills if over limit
        self._prune_old_skills()

    def _render_skill_md(self, wf: SkillWorkflow) -> str:
        """Render a skill workflow as a markdown document with quality score and failure-pattern banner."""
        header = {
            "name": wf.name,
            "description": wf.description,
            "tool_count": wf.tool_count,
            "tool_sequence": wf.tool_sequence,
            "modules_used": wf.modules_used,
            "source_task": wf.source_task_id,
            "fingerprint": wf.fingerprint,
            "quality_score": wf.quality_score,
            "is_failure_pattern": wf._is_failure_pattern,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(wf.created_at)),
            "use_count": wf.use_count,
            "version": wf.version,
        }
        yaml_header = json.dumps(header, indent=2)

        steps_md = "\n".join(wf.steps)

        failure_banner = ""
        if wf._is_failure_pattern:
            failure_banner = (
                "\n> **WARNING**: This is a FAILURE PATTERN. "
                "The workflow below resulted in an error. "
                "Use this to learn what NOT to do.\n"
            )

        return f"""---
{yaml_header}
---

# {wf.name.replace('-', ' ').title()}

{failure_banner}
## Goal
{wf.description}

## Quality Score
{wf.quality_score:.2f} / 1.00

## Workflow
{steps_md}

## Tools Used
{', '.join(wf.tool_sequence)}

## Registry Modules
{', '.join(wf.modules_used) if wf.modules_used else 'none'}
"""

    def _update_index(self, workflow: SkillWorkflow):
        """Update the skills index file with quality and failure metadata."""
        index_path = SKILLS_DIR / "index.json"
        index = {}
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text())
            except Exception:
                pass

        index[workflow.name] = {
            "name": workflow.name,
            "description": workflow.description,
            "tool_count": workflow.tool_count,
            "fingerprint": workflow.fingerprint,
            "created_at": workflow.created_at,
            "use_count": workflow.use_count,
            "version": workflow.version,
            "last_used_at": workflow.last_used_at,
            "quality_score": workflow.quality_score,
            "_is_failure_pattern": workflow._is_failure_pattern,
        }
        try:
            index_path.write_text(json.dumps(index, indent=2))
        except Exception as e:
            logger.error("SkillExtractor: failed to save index: %s", e)

    def _load_all_skills(self) -> List[Dict[str, Any]]:
        """Load all skill metadata from the index.

        Returns an empty list if index doesn't exist or is corrupted.
        A backup of the corrupted file is saved for recovery.
        """
        index_path = SKILLS_DIR / "index.json"
        if not index_path.exists():
            return []
        try:
            index = json.loads(index_path.read_text())
            if not isinstance(index, dict):
                raise ValueError("index.json is not a dict")
            return list(index.values())
        except Exception:
            # Corrupted index - back it up so data isn't permanently lost
            backup = SKILLS_DIR / f"index.json.corrupted.{int(time.time())}"
            try:
                import shutil as _shutil
                _shutil.copy2(str(index_path), str(backup))
                logger.warning("Corrupted index.json backed up to %s", backup)
            except Exception:
                pass
            return []

    def _prune_old_skills(self):
        """Remove least-used skills when over MAX_SKILLS, updating index."""
        skills = self._load_all_skills()
        if len(skills) <= MAX_SKILLS:
            return

        # Sort by use_count ascending, prune the bottom
        skills.sort(key=lambda s: (s.get("use_count", 0), s.get("last_used_at", 0)))
        to_remove = skills[:len(skills) - MAX_SKILLS]

        # Load index for removal
        index_path = SKILLS_DIR / "index.json"
        index = {}
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text())
            except Exception:
                pass

        for s in to_remove:
            skill_path = SKILLS_DIR / f"{s['name']}.md"
            try:
                skill_path.unlink(missing_ok=True)
                index.pop(s['name'], None)
                logger.info("SkillExtractor: pruned unused skill '%s'", s['name'])
            except Exception:
                pass

        try:
            index_path.write_text(json.dumps(index, indent=2))
        except Exception as e:
            logger.error("SkillExtractor: failed to save pruned index: %s", e)
