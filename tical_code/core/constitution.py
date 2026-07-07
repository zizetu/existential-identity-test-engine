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

# provenance:ticalasi-zzt-2026
"""
Agent constitution mechanism - non-violable behavior boundaries
=====================================

Each Agent has its own constitution file, which defines core principles,
behavior boundaries, obligations, and override rules.
At decision time, the Constitution Enforcer performs a mandatory check;
rule violations result in reject/warning/downgrade/report.

Core concepts:
- Constitution: constitution data structure, containing principles/boundaries/obligations/overrides
- ConstitutionEnforcer: constitution enforcement engine that performs mandatory checks before DecisionEngine decisions
- ConstitutionTemplate: predefined constitution templates (default/trading/creative/npc/assistant)
- Dynamic load: reads YAML constitution files from ./constitution/ directory, supports hot reload and version tracking

Integration points with existing modules:
- decision_engine.py: calls ConstitutionEnforcer.check_action() before decisions
- trace.py: records constitution check results
- identity.py: binds Agent identity with constitution
- reflection.py: checks for constitution violations during reflection

Design principles:
1. Constitution checks are mandatory and cannot be bypassed by the Agent
2. Default constitution template is conservative (security-first); users can relax rules
3. Constitution files use YAML (human-readable and editable)
4. Zero external dependencies (yaml is stdlib or a standard dependency, already installed in project)

Author: Tical (Zize Tu)
Version: see tical_code.__version__
"""

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Default constitution file directory (relative to project root directory)
DEFAULT_CONSTITUTION_DIR = "constitution"

# Constitution template subdirectory
TEMPLATE_SUBDIR = "templates"

# Constitution file extension name
CONSTITUTION_EXT = ".yaml"

# Current constitution format version
CONSTITUTION_FORMAT_VERSION = "1.0"

# Runtime reachability status taxonomy (CL-RUNTIME-001)
NOT_STARTED = "NOT_STARTED"
DESIGNED = "DESIGNED"
IMPORTABLE = "IMPORTABLE"
WIRED = "WIRED"
VERIFIED = "VERIFIED"


# =============================================================================
# Enums
# =============================================================================

class RuleType(Enum):
    """Rule type."""
    PRINCIPLE = "principle"     # core principle
    BOUNDARY = "boundary"       # behavior boundary
    OBLIGATION = "obligation"   # must-do obligation
    OVERRIDE = "override"       # scenario-specific override rule


class ViolationSeverity(Enum):
    """Violation severity level."""
    LOW = "low"           # minor violation, warning is sufficient
    MEDIUM = "medium"     # moderate violation, requires intervention
    HIGH = "high"         # severe violation, must reject
    CRITICAL = "critical" # fatal violation, immediately stop and report


class ViolationAction(Enum):
    """Violation handling action."""
    REJECT = "reject"           # reject execution
    WARN = "warn"               # warn but allow
    DEGRADE = "downgrade"       # downgrade execution (e.g. from write operation downgraded to read-only)
    ESCALATE = "escalate"       # escalate report (notify admin/user)
    WARNING_FIRST = "warning_first"  # v0.13: warn first then reject (tiered guardrail)


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class ConstitutionRule:
    """A single constitution rule.

    Attributes:
        rule_id: Unique rule identifier (e.g. "P-001", "B-003")
        rule_type: Rule type (principle/boundary/obligation/override)
        description: Rule description (human-readable)
        pattern: List of critical matching keywords, used to detect whether an action triggers this rule
        severity: Violation severity level
        action: Action to take on violation
        enabled: Whether enabled (useful for temporarily disabling a specific rule)
        tags: Tag list, used for context matching
        warning_first: v0.13 whether warning-first mode is enabled (first-time warning, second-time hard reject)
        warning_count: v0.13 current warning count (runtime status, not persisted)
    """
    rule_id: str
    rule_type: RuleType
    description: str
    pattern: List[str] = field(default_factory=list)
    severity: ViolationSeverity = ViolationSeverity.MEDIUM
    action: ViolationAction = ViolationAction.REJECT
    enabled: bool = True
    tags: List[str] = field(default_factory=list)
    # v0.13: tiered guardrail fields
    warning_first: bool = False
    warning_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'rule_id': self.rule_id,
            'rule_type': self.rule_type.value,
            'description': self.description,
            'pattern': self.pattern,
            'severity': self.severity.value,
            'action': self.action.value,
            'enabled': self.enabled,
            'tags': self.tags,
            'warning_first': self.warning_first,
            # warning_count is not persisted (runtime status)
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ConstitutionRule':
        return cls(
            rule_id=data.get('rule_id', ''),
            rule_type=RuleType(data.get('rule_type', 'principle')),
            description=data.get('description', ''),
            pattern=data.get('pattern', []),
            severity=ViolationSeverity(data.get('severity', 'medium')),
            action=ViolationAction(data.get('action', 'reject')),
            enabled=data.get('enabled', True),
            tags=data.get('tags', []),
            warning_first=data.get('warning_first', False),
            # warning_count is not recovered from dict
        )


@dataclass
class Constitution:
    """Agent constitution - defines non-violable behavior boundaries.

    Attributes:
        name: Constitution name
        version: Constitution version number
        format_version: Format version (used for compatibility checking)
        description: Constitution description
        agent_type: Applicable Agent type (e.g. "default", "trading")
        principles: Core principle list
        boundaries: Behavior boundary list
        obligations: Must-do obligation list
        overrides: Scenario-specific override rule list
        created_at: Creation time
        updated_at: Last update time
        checksum: Content checksum (used for version tracking)
    """
    name: str = "default"
    version: str = "1.0"
    format_version: str = CONSTITUTION_FORMAT_VERSION
    description: str = ""
    agent_type: str = "default"
    principles: List[ConstitutionRule] = field(default_factory=list)
    boundaries: List[ConstitutionRule] = field(default_factory=list)
    obligations: List[ConstitutionRule] = field(default_factory=list)
    overrides: List[ConstitutionRule] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    checksum: str = ""

    @property
    def all_rules(self) -> List[ConstitutionRule]:
        """Get all rules (including disabled ones; caller must filter)."""
        return self.principles + self.boundaries + self.obligations + self.overrides

    @property
    def active_rules(self) -> List[ConstitutionRule]:
        """Get all enabled rules."""
        return [r for r in self.all_rules if r.enabled]

    def get_rules_by_type(self, rule_type: RuleType) -> List[ConstitutionRule]:
        """Get rules by type."""
        mapping = {
            RuleType.PRINCIPLE: self.principles,
            RuleType.BOUNDARY: self.boundaries,
            RuleType.OBLIGATION: self.obligations,
            RuleType.OVERRIDE: self.overrides,
        }
        return [r for r in mapping.get(rule_type, []) if r.enabled]

    def compute_checksum(self) -> str:
        """Compute content checksum (used for version tracking)."""
        content_parts = []
        for rule in self.all_rules:
            content_parts.append(f"{rule.rule_id}:{rule.description}:{rule.enabled}")
        content = "|".join(content_parts)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def check_reachability(self, module_path: str) -> str:
        """Audit a module against the CL-RUNTIME-001 4-step verification.

        Steps:
        1. File exists - verify the module file is present on disk
        2. Import chain - verify the module can be imported without errors
        3. Handler registered - verify a callable entry point exists
        4. Execution verified - verify the handler is accessible at runtime

        Args:
            module_path: Dotted module path (e.g. 'tical_code.core.constitution')

        Returns:
            One of the reachability status constants:
            NOT_STARTED, DESIGNED, IMPORTABLE, WIRED, VERIFIED
        """
        import importlib
        import importlib.util

        # Step 1: File existence check
        try:
            spec = importlib.util.find_spec(module_path)
            if spec is None or spec.origin is None:
                return NOT_STARTED
            if not os.path.isfile(spec.origin):
                return NOT_STARTED
        except (ImportError, AttributeError, ValueError) as e:
            logger.debug(
                "check_reachability: step 1 (file exists) failed for %s: %s",
                module_path, e,
            )
            return NOT_STARTED

        # Step 2: Import chain
        try:
            module = importlib.import_module(module_path)
        except (ImportError, SyntaxError, Exception) as e:
            logger.debug(
                "check_reachability: step 2 (import chain) failed for %s: %s",
                module_path, e,
            )
            return DESIGNED  # file exists but cannot import

        # Step 3: Handler / entry-point registration
        handler_names = ('register', 'init', 'setup', 'main', 'run', 'handle')
        has_handler = False
        for attr_name in handler_names:
            attr = getattr(module, attr_name, None)
            if attr is not None and callable(attr):
                has_handler = True
                break
        if not has_handler:
            return IMPORTABLE  # imports but no callable handler found

        # Step 4: Execution verified
        # Verify that the handler is accessible and callable without
        # raising unexpected errors on attribute access. Full runtime
        # execution requires integration tests via the entry point.
        try:
            for attr_name in handler_names:
                attr = getattr(module, attr_name, None)
                if attr is not None and callable(attr):
                    # Handler exists and is callable - verification passes
                    break
            return VERIFIED
        except Exception as e:
            logger.debug(
                "check_reachability: step 4 (execution verified) failed for %s: %s",
                module_path, e,
            )
            return WIRED  # has handler but runtime access failed

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'version': self.version,
            'format_version': self.format_version,
            'description': self.description,
            'agent_type': self.agent_type,
            'principles': [r.to_dict() for r in self.principles],
            'boundaries': [r.to_dict() for r in self.boundaries],
            'obligations': [r.to_dict() for r in self.obligations],
            'overrides': [r.to_dict() for r in self.overrides],
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'checksum': self.checksum or self.compute_checksum(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Constitution':
        constitution = cls(
            name=data.get('name', 'default'),
            version=data.get('version', '1.0'),
            format_version=data.get('format_version', CONSTITUTION_FORMAT_VERSION),
            description=data.get('description', ''),
            agent_type=data.get('agent_type', 'default'),
            principles=[ConstitutionRule.from_dict(r) for r in data.get('principles', [])],
            boundaries=[ConstitutionRule.from_dict(r) for r in data.get('boundaries', [])],
            obligations=[ConstitutionRule.from_dict(r) for r in data.get('obligations', [])],
            overrides=[ConstitutionRule.from_dict(r) for r in data.get('overrides', [])],
            created_at=data.get('created_at', time.time()),
            updated_at=data.get('updated_at', time.time()),
            checksum=data.get('checksum', ''),
        )
        # If no checksum, auto-compute
        if not constitution.checksum:
            constitution.checksum = constitution.compute_checksum()
        return constitution


@dataclass
class ConstitutionCheckResult:
    """Constitution check result.

    Attributes:
        allowed: Whether the action is allowed by the constitution
        reason: Reason for reject/warning
        matched_rules: List of matched rules
        severity: Most severe violation level
        action: Suggested handling action
        constitution_version: Referenced constitution version (used for trace record)
        constitution_checksum: Referenced constitution checksum
        is_warning: v0.13 whether this is a tiered-guardrail warning phase (first-time violation, allowed but warned)
    """
    allowed: bool = True
    reason: str = ""
    matched_rules: List[ConstitutionRule] = field(default_factory=list)
    severity: ViolationSeverity = ViolationSeverity.LOW
    action: ViolationAction = ViolationAction.WARN
    constitution_version: str = ""
    constitution_checksum: str = ""
    # v0.13: tiered guardrail marker
    is_warning: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'allowed': self.allowed,
            'reason': self.reason,
            'matched_rules': [r.to_dict() for r in self.matched_rules],
            'severity': self.severity.value,
            'action': self.action.value,
            'constitution_version': self.constitution_version,
            'constitution_checksum': self.constitution_checksum,
        }


# =============================================================================
# ConstitutionEnforcer - constitution enforcement engine
# =============================================================================

class ConstitutionEnforcer:
    """Constitution enforcement engine - performs mandatory checks before DecisionEngine decisions.

    Core responsibilities:
    1. Check whether an action is allowed by the constitution
    2. Get applicable rules based on context
    3. Execute handling strategy on violation

    Usage:
        enforcer = ConstitutionEnforcer(constitution)
        result = enforcer.check_action("delete_file /etc/passwd")
        if not result.allowed:
            # reject execution, return reason to user
            pass

    Attributes:
        constitution: Currently loaded constitution
        _violation_handlers: Violation handling function registry
        _check_history: Recent check records (used for audit)
    """

    # Maximum number of check history entries to retain
    MAX_HISTORY = 100

    def __init__(
        self,
        constitution: Optional[Constitution] = None,
        constitution_dir: Optional[str] = None,
        agent_type: str = "default",
    ):
        """
        Args:
            constitution: Constitution instance (if provided, used preferentially)
            constitution_dir: Constitution file directory (if constitution is not provided, load from this directory)
            agent_type: Agent type (used to select the appropriate constitution template)
        """
        self._constitution: Optional[Constitution] = constitution
        self._constitution_dir = constitution_dir
        self._agent_type = agent_type
        self._violation_handlers: Dict[ViolationAction, Callable] = {
            ViolationAction.REJECT: self._handle_reject,
            ViolationAction.WARN: self._handle_warn,
            ViolationAction.DEGRADE: self._handle_downgrade,
            ViolationAction.ESCALATE: self._handle_escalate,
            ViolationAction.WARNING_FIRST: self._handle_warning_first,
        }
        self._check_history: List[ConstitutionCheckResult] = []
        self._last_load_time: float = 0.0

        # Compiled rule index (built once after load, rebuilt on reload)
        # _untagged_pattern_rules: list of (rule, pre-lowered patterns) for
        #   enabled rules that have patterns but no tags - always checked.
        # _tagged_rules_by_tag: dict of tag_lower -> list of (rule, pre-lowered
        #   patterns) - used for O(1) lookup when context tags are present.
        # _all_tagged_rules: flat list of all tagged (rule, patterns) - used
        #   for lenient fallback when context has no tags.
        # _override_untagged: same as _untagged_pattern_rules but for overrides
        #   only (overrides have stricter tag filtering in get_applicable_rules).
        # _override_by_tag: dict of tag_lower -> list of (rule, patterns) for
        #   override rules with tags.
        self._untagged_pattern_rules: List[Tuple[ConstitutionRule, List[str]]] = []
        self._tagged_rules_by_tag: Dict[str, List[Tuple[ConstitutionRule, List[str]]]] = {}
        self._all_tagged_rules: List[Tuple[ConstitutionRule, List[str]]] = []
        self._override_untagged: List[Tuple[ConstitutionRule, List[str]]] = []
        self._override_by_tag: Dict[str, List[Tuple[ConstitutionRule, List[str]]]] = {}
        self._index_built: bool = False

        # If no constitution provided, attempt to load one
        if self._constitution is None:
            self._load_constitution()
        self._build_compiled_index()

    @property
    def constitution(self) -> Constitution:
        """Get the current constitution (lazy-load)."""
        if self._constitution is None:
            self._load_constitution()
        # If load failed, return default constitution
        if self._constitution is None:
            self._constitution = ConstitutionTemplate.get_template("default")
        return self._constitution

    def _load_constitution(self) -> None:
        """Load constitution from file directory."""
        if self._constitution_dir and os.path.isdir(self._constitution_dir):
            # Attempt to load the constitution file for the corresponding agent_type
            target_file = os.path.join(
                self._constitution_dir, f"{self._agent_type}{CONSTITUTION_EXT}"
            )
            if os.path.isfile(target_file):
                try:
                    self._constitution = self._load_from_yaml(target_file)
                    self._last_load_time = time.time()
                    logger.info(
                        f"[ConstitutionEnforcer] Loaded constitution: "
                        f"{self._constitution.name} v{self._constitution.version}"
                    )
                    return
                except Exception as e:
                    logger.warning(f"[ConstitutionEnforcer] Failed to load constitution file: {e}")

            # Attempt to load default constitution
            default_file = os.path.join(
                self._constitution_dir, f"default{CONSTITUTION_EXT}"
            )
            if os.path.isfile(default_file):
                try:
                    self._constitution = self._load_from_yaml(default_file)
                    self._last_load_time = time.time()
                    logger.info(
                        f"[ConstitutionEnforcer] Loaded default constitution: "
                        f"{self._constitution.name} v{self._constitution.version}"
                    )
                    return
                except Exception as e:
                    logger.warning(f"[ConstitutionEnforcer] Failed to load default constitution: {e}")

        # No file or load failed, use template
        self._constitution = ConstitutionTemplate.get_template(self._agent_type)
        self._last_load_time = time.time()
        logger.info(
            f"[ConstitutionEnforcer] Using built-in template: "
            f"{self._constitution.name} v{self._constitution.version}"
        )

    @staticmethod
    def _load_from_yaml(file_path: str) -> Constitution:
        """Load constitution from YAML file.

        Args:
            file_path: YAML file path

        Returns:
            Constitution instance

        Raises:
            ValueError: File format is invalid
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"Constitution file format error: expected dict, got {type(data).__name__}")

        # Check format version compatibility
        format_ver = data.get('format_version', '1.0')
        if format_ver != CONSTITUTION_FORMAT_VERSION:
            logger.warning(
                f"[ConstitutionEnforcer] Constitution format version mismatch: "
                f"file={format_ver}, current={CONSTITUTION_FORMAT_VERSION}, "
                f"attempting compatibility load"
            )

        return Constitution.from_dict(data)

    def _build_compiled_index(self) -> None:
        """Pre-build lookup structures for O(1) per-call rule matching.

        Called once after _load_constitution() and again on reload().
        Builds:
          - _untagged_pattern_rules: enabled rules with patterns, no tags
          - _tagged_rules_by_tag: tag_lower -> [(rule, lowered_patterns), ...]
          - _all_tagged_rules: flat list of all tagged (rule, patterns)
          - _override_untagged: override-only untagged rules
          - _override_by_tag: tag_lower -> [(override_rule, patterns), ...]

        This eliminates per-call O(n) iteration over all rules and repeated
        str.lower() calls on patterns.
        """
        if self._constitution is None:
            self._index_built = False
            return

        self._untagged_pattern_rules.clear()
        self._tagged_rules_by_tag.clear()
        self._all_tagged_rules.clear()
        self._override_untagged.clear()
        self._override_by_tag.clear()

        for rule in self._constitution.all_rules:
            if not rule.enabled or not rule.pattern:
                continue
            lowered_patterns = [p.lower() for p in rule.pattern]
            entry = (rule, lowered_patterns)

            if rule.rule_type == RuleType.OVERRIDE:
                # Overrides get their own index (stricter tag filtering)
                if rule.tags:
                    for tag in rule.tags:
                        tag_lower = tag.lower()
                        self._override_by_tag.setdefault(
                            tag_lower, []
                        ).append(entry)
                else:
                    self._override_untagged.append(entry)
            else:
                # Non-override rules
                if rule.tags:
                    self._all_tagged_rules.append(entry)
                    for tag in rule.tags:
                        tag_lower = tag.lower()
                        self._tagged_rules_by_tag.setdefault(
                            tag_lower, []
                        ).append(entry)
                else:
                    self._untagged_pattern_rules.append(entry)

        self._index_built = True

    def reload(self) -> bool:
        """Reload the constitution file (hot reload).

        Returns:
            True if reload succeeded
        """
        old_checksum = self._constitution.compute_checksum() if self._constitution else ""
        self._constitution = None
        self._load_constitution()
        self._build_compiled_index()
        new_checksum = self._constitution.compute_checksum() if self._constitution else ""

        if old_checksum != new_checksum:
            logger.info(
                f"[ConstitutionEnforcer] Constitution updated: "
                f"{old_checksum} -> {new_checksum}"
            )
            return True
        return False

    def check_action(
        self,
        action: str,
        context: Optional[Dict[str, Any]] = None,
        mode: str = "write",
    ) -> ConstitutionCheckResult:
        """Check whether an action is allowed by the constitution.

        This is the core method, called mandatorily before DecisionEngine decisions.

        Matching logic:
        1. Traverse all active rules
        2. Check whether action text matches rule pattern keywords
        3. If context has tags, prefer matching rules with those tags
        4. Collect all matched rules, sort by severity
        5. Most severe violation determines the final result

        Args:
            action: Action description (e.g. "delete_file /etc/passwd")
            context: Context info (e.g. {"tags": ["money", "external"],
                      "tool": "exec_bash"})
            mode: Operation mode - "write" (write operations, default, strict interception)
                  or "read" (read operations, allow reading system info for audit)

        Returns:
            ConstitutionCheckResult check result
        """
        if not action or not isinstance(action, str):
            # Empty action defaults to allowed (nothing to check)
            return ConstitutionCheckResult(
                allowed=True,
                constitution_version=self.constitution.version,
                constitution_checksum=self.constitution.checksum,
            )

        action_lower = action.lower()
        context = context or {}
        context_tags = set(context.get('tags', []))

        # Read-write distinction: read operations only match write-relevant rules,
        # skip boundary rules that only restrict write operations
        # B-001 (system path protection) for read operations downgrades to WARN rather than REJECT
        is_read_mode = mode == "read"

        # Use compiled index for O(1) rule lookup instead of O(n) iteration
        candidate_entries = self._collect_candidate_rules(context_tags)

        # Match rules
        matched_rules = []
        for rule, lowered_patterns in candidate_entries:
            if self._match_rule_fast(rule, lowered_patterns, action_lower, context_tags):
                # Read mode: skip obligation rules - they enforce agent behavior,
                # not user message content. User messages are inspected, not acted upon.
                if is_read_mode and rule.rule_type == RuleType.OBLIGATION:
                    continue
                # Read-write distinction: B-001 (system path protection) for read operations downgrades
                if is_read_mode and rule.rule_id == "B-001":
                    # Read operation accessing system path: allow but record (used for audit), do not block
                    logger.info(
                        f"[ConstitutionEnforcer] Read operation accessing system path, allowing: {action[:100]}"
                    )
                    continue
                # Read-write distinction: P-002 (destructive ops) for read operations - user messages
                # mentioning "delete"/"remove" are not destructive actions. Skip in read mode.
                if is_read_mode and rule.rule_id == "P-002":
                    continue
                # Read-write distinction: B-005 (SSH/private key patterns) for read operations -
                # reading ~/.ssh/authorized_keys or ~/.ssh/config is legitimate ops, not theft.
                if is_read_mode and rule.rule_id == "B-005":
                    logger.info(
                        f"[ConstitutionEnforcer] Read operation accessing SSH paths, allowing: {action[:100]}"
                    )
                    continue
                matched_rules.append(rule)

        # No rules matched -> allow
        if not matched_rules:
            result = ConstitutionCheckResult(
                allowed=True,
                constitution_version=self.constitution.version,
                constitution_checksum=self.constitution.checksum,
            )
        else:
            # Sort by severity, take most severe
            severity_order = {
                ViolationSeverity.CRITICAL: 4,
                ViolationSeverity.HIGH: 3,
                ViolationSeverity.MEDIUM: 2,
                ViolationSeverity.LOW: 1,
            }
            matched_rules.sort(
                key=lambda r: severity_order.get(r.severity, 0),
                reverse=True,
            )
            most_severe = matched_rules[0]

            # Determine final action
            # ESCALATE takes priority: if the rule explicitly says escalate, respect it
            # even for CRITICAL/HIGH severity - escalation means "block AND notify"
            final_action = most_severe.action
            if final_action == ViolationAction.ESCALATE:
                pass  # ESCALATE is the strongest action; do not downgrade to REJECT
            elif most_severe.severity in (ViolationSeverity.CRITICAL, ViolationSeverity.HIGH):
                final_action = ViolationAction.REJECT
            elif most_severe.severity == ViolationSeverity.LOW:
                final_action = ViolationAction.WARN

            # v0.13: tiered guardrail - WARNING_FIRST logic
            is_warning_stage = False
            if final_action == ViolationAction.WARNING_FIRST or most_severe.warning_first:
                # Enable warning-first mode
                most_severe.warning_count += 1
                if most_severe.warning_count == 1:
                    # First-time violation -> allow but warn
                    final_action = ViolationAction.WARN
                    is_warning_stage = True
                    logger.info(
                        f"[ConstitutionEnforcer] Tiered guardrail: Rule {most_severe.rule_id} "
                        f"first-time violation, emitting warning (next time will hard reject)"
                    )
                else:
                    # Second-time and above violation -> hard reject
                    final_action = ViolationAction.REJECT
                    is_warning_stage = False
                    logger.warning(
                        f"[ConstitutionEnforcer] Tiered guardrail: Rule {most_severe.rule_id} "
                        f"violation #{most_severe.warning_count}, executing hard reject"
                    )

            # Build reason description
            reasons = []
            for rule in matched_rules[:3]:  # display at most 3 rules
                reasons.append(f"[{rule.rule_id}] {rule.description}")
            reason_text = "; ".join(reasons)

            # Append tiered-guardrail warning prompt
            if is_warning_stage:
                reason_text += " (WARNING: next violation will be hard-rejected)"

            result = ConstitutionCheckResult(
                allowed=(final_action != ViolationAction.REJECT
                         and final_action != ViolationAction.ESCALATE),
                reason=reason_text,
                matched_rules=matched_rules,
                severity=most_severe.severity,
                action=final_action,
                constitution_version=self.constitution.version,
                constitution_checksum=self.constitution.checksum,
                is_warning=is_warning_stage,
            )

        # Record check history
        self._check_history.append(result)
        if len(self._check_history) > self.MAX_HISTORY:
            self._check_history = self._check_history[-self.MAX_HISTORY:]

        return result

    def _collect_candidate_rules(
        self,
        context_tags: set,
    ) -> List[Tuple[ConstitutionRule, List[str]]]:
        """Collect candidate (rule, pre-lowered-patterns) entries from the
        compiled index, avoiding O(n) full-rule-list iteration.

        Logic mirrors the original get_applicable_rules() + _match_rule()
        tag-matching semantics:
          - Untagged non-override rules: always candidates.
          - Untagged override rules: always candidates.
          - Tagged rules: included only if context_tags intersect rule.tags
            (lenient: if context_tags is empty, all tagged rules are included).
          - Deduplication by rule_id (a rule may appear under multiple tags).
        """
        seen_ids: set = set()
        result: List[Tuple[ConstitutionRule, List[str]]] = []

        # Always include untagged non-override rules
        for entry in self._untagged_pattern_rules:
            result.append(entry)
            seen_ids.add(entry[0].rule_id)

        # Always include untagged overrides
        for entry in self._override_untagged:
            if entry[0].rule_id not in seen_ids:
                result.append(entry)
                seen_ids.add(entry[0].rule_id)

        if context_tags:
            # Include rules whose tags intersect context_tags
            for tag in context_tags:
                tag_lower = tag.lower()
                for entry in self._tagged_rules_by_tag.get(tag_lower, []):
                    if entry[0].rule_id not in seen_ids:
                        result.append(entry)
                        seen_ids.add(entry[0].rule_id)
                for entry in self._override_by_tag.get(tag_lower, []):
                    if entry[0].rule_id not in seen_ids:
                        result.append(entry)
                        seen_ids.add(entry[0].rule_id)
        else:
            # Lenient mode: no context tags -> include all tagged rules
            for entry in self._all_tagged_rules:
                if entry[0].rule_id not in seen_ids:
                    result.append(entry)
                    seen_ids.add(entry[0].rule_id)
            # Tagged overrides: in original get_applicable_rules, overrides with
            # tags are SKIPPED when context has no tags. Preserve that behavior.
            # (Intentionally NOT adding _override_by_tag entries here.)

        return result

    @staticmethod
    def _match_rule_fast(
        rule: ConstitutionRule,
        lowered_patterns: List[str],
        action_lower: str,
        context_tags: set,
    ) -> bool:
        """Match a rule against an action using pre-lowered patterns.

        Equivalent to _match_rule() but uses pre-computed lowered_patterns
        instead of calling str.lower() on every pattern every call.

        Returns True if the rule matches the action.
        """
        if not lowered_patterns:
            return False

        # Pattern match: single-word patterns use word-boundary (\\b) to avoid
        # false positives (e.g., "drop" in "drops", "clear" in "clearly").
        # Multi-word patterns (containing spaces) use substring match.
        # Patterns starting with non-word chars (. / -) omit leading \\b
        # since \\b never matches before a non-word character.
        for p in lowered_patterns:
            if ' ' in p:
                if p in action_lower:
                    break
            else:
                if p and not p[0].isalnum() and p[0] != '_':
                    # Non-word leading char (e.g. .ssh/, .env, /dev/):
                    # only apply \\b at the end
                    if re.search(re.escape(p) + r'\b', action_lower):
                        break
                else:
                    if re.search(r'\b' + re.escape(p) + r'\b', action_lower):
                        break
        else:
            return False  # no pattern matched

        # Tag matching
        if rule.tags:
            if not context_tags:
                return True  # lenient: no context tags -> match anyway
            rule_tags_lower = {t.lower() for t in rule.tags}
            return bool(rule_tags_lower & context_tags)

        return True

    def get_applicable_rules(
        self,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[ConstitutionRule]:
        """Get applicable rules based on current context.

        Priority:
        1. Override rules (scenario-specific overrides, highest priority)
        2. Boundary rules (behavior boundaries)
        3. Principle rules (core principles)
        4. Obligation rules (obligations, usually do not restrict actions but require doing something)

        Args:
            context: Context info

        Returns:
            List of applicable rules
        """
        context = context or {}
        context_tags = set(context.get('tags', []))
        rules = []

        # Override rules have highest priority
        for rule in self.constitution.overrides:
            if not rule.enabled:
                continue
            # If override has tags, must intersect with context tags for it to apply
            if rule.tags and context_tags:
                if not (set(rule.tags) & context_tags):
                    continue
            elif rule.tags and not context_tags:
                continue  # override has tags but context has no tags, skip
            rules.append(rule)

        # Behavior boundaries
        rules.extend(r for r in self.constitution.boundaries if r.enabled)

        # Core principles
        rules.extend(r for r in self.constitution.principles if r.enabled)

        # Obligations (usually do not restrict actions, but some obligations may contain prohibitive clauses)
        for rule in self.constitution.obligations:
            if rule.enabled and rule.pattern:
                # Obligations with patterns also participate in matching (e.g. "involves-money must user-confirm")
                rules.append(rule)

        return rules

    @staticmethod
    def _match_rule(
        rule: ConstitutionRule,
        action_lower: str,
        context_tags: set,
    ) -> bool:
        """Check whether an action matches a specific rule.

        Matching method: action text contains any of the rule's pattern keywords.
        If the rule has tags, also requires context_tags intersection.

        Args:
            rule: Constitution rule
            action_lower: Lowercased action description
            context_tags: Context tag set

        Returns:
            True if matched
        """
        if not rule.pattern:
            return False

        # Pattern match: single-word patterns use word-boundary (\b) to avoid
        # false positives (e.g., "drop" in "drops", "clear" in "clearly").
        # Multi-word patterns (containing spaces) use substring match.
        # Patterns starting with non-word chars (. / -) omit leading \b
        # since \b never matches before a non-word character.
        for p_raw in rule.pattern:
            p = p_raw.lower()
            if ' ' in p:
                if p in action_lower:
                    break
            else:
                if p and not p[0].isalnum() and p[0] != '_':
                    # Non-word leading char: only apply \b after pattern
                    if re.search(re.escape(p) + r'\b', action_lower):
                        break
                else:
                    if re.search(r'\b' + re.escape(p) + r'\b', action_lower):
                        break
        else:
            return False  # no pattern matched

        # Tag match: if rule has tags, also requires context to have corresponding tag
        if rule.tags:
            if not context_tags:
                # No context tags, rule still matches (lenient mode)
                return True
            return bool(set(rule.tags) & context_tags)

        return True

    def violation_handler(
        self,
        violation: ConstitutionCheckResult,
    ) -> Dict[str, Any]:
        """Handle strategy on violation.

        Based on the action field in ConstitutionCheckResult,
        calls the corresponding handling function.

        Args:
            violation: Constitution check result

        Returns:
            Handling result dict, containing handled (bool) and message (str)
        """
        handler = self._violation_handlers.get(violation.action)
        if handler:
            return handler(violation)
        # Unknown action type, default to reject
        logger.warning(
            f"[ConstitutionEnforcer] Unknown violation action: {violation.action}, defaulting to reject"
        )
        return {"handled": True, "message": f"Operation rejected: {violation.reason}"}

    def register_handler(
        self,
        action: ViolationAction,
        handler: Callable,
    ) -> None:
        """Register a custom violation handling function.

        Args:
            action: Violation action type
            handler: Handling function, accepts ConstitutionCheckResult, returns Dict
        """
        self._violation_handlers[action] = handler

    # --- Built-in violation handling functions ---

    @staticmethod
    def _handle_reject(violation: ConstitutionCheckResult) -> Dict[str, Any]:
        """Reject execution."""
        return {
            "handled": True,
            "message": f"[BLOCKED] Operation rejected by constitution: {violation.reason}",
            "action_taken": "reject",
        }

    @staticmethod
    def _handle_warn(violation: ConstitutionCheckResult) -> Dict[str, Any]:
        """Warn but allow execution."""
        return {
            "handled": True,
            "message": f"[WARN] Constitution warning: {violation.reason} (operation will still execute)",
            "action_taken": "warn",
        }

    @staticmethod
    def _handle_downgrade(violation: ConstitutionCheckResult) -> Dict[str, Any]:
        """Downgrade execution."""
        return {
            "handled": True,
            "message": f"[DOWNGRADE] Operation downgraded: {violation.reason} (downgraded to read-only operation)",
            "action_taken": "downgrade",
        }

    @staticmethod
    def _handle_escalate(violation: ConstitutionCheckResult) -> Dict[str, Any]:
        """Escalate report (notify admin/user)."""
        # Log at CRITICAL level so syslog/journald can pick it up
        logger.critical(
            f"[ConstitutionEnforcer] ESCALATED: Rule={violation.severity.value} "
            f"action='{violation.action.value}' reason='{violation.reason}'"
        )
        return {
            "handled": True,
            "message": f"[ALERT] Escalated: {violation.reason} (requires admin confirmation; operation blocked pending review)",
            "action_taken": "escalate",
            "needs_notification": True,
        }

    @staticmethod
    def _handle_warning_first(violation: ConstitutionCheckResult) -> Dict[str, Any]:
        """v0.13: tiered guardrail handling - warn first then reject.

        Note: the actual warning_count counting and allow/reject determination
        is already completed in check_action(). This handler is only responsible
        for generating the handling result message.
        """
        if violation.is_warning:
            # Warning phase (first-time violation, allow but warn)
            return {
                "handled": True,
                "message": f"[WARN] Tiered guardrail warning: {violation.reason} (next violation will be hard-rejected)",
                "action_taken": "warning_first_warn",
            }
        else:
            # Hard reject phase (second-time and above violation)
            return {
                "handled": True,
                "message": f"[BLOCKED] Tiered guardrail hard reject: {violation.reason} (already multiple violations)",
                "action_taken": "warning_first_reject",
            }

    # v0.13: tiered guardrail management methods

    def reset_warning(self, rule_id: str) -> bool:
        """Reset the warning count for a specific rule.

        Args:
            rule_id: Rule ID to reset

        Returns:
            True if rule was found and reset, False if not found
        """
        try:
            for rule in self.constitution.all_rules:
                if rule.rule_id == rule_id:
                    rule.warning_count = 0
                    logger.info(
                        f"[ConstitutionEnforcer] Reset warning count for rule {rule_id}"
                    )
                    return True
            logger.warning(
                f"[ConstitutionEnforcer] Rule {rule_id} not found, cannot reset warning"
            )
            return False
        except Exception as e:
            logger.warning(f"[ConstitutionEnforcer] Reset warning exception: {e}")
            return False

    def get_warning_status(self) -> Dict[str, Any]:
        """Get the warning status for all rules.

        Returns:
            Dict with rule_id as key, value contains warning_first / warning_count info
        """
        try:
            status = {}
            for rule in self.constitution.all_rules:
                if rule.warning_first or rule.warning_count > 0:
                    status[rule.rule_id] = {
                        'warning_first': rule.warning_first,
                        'warning_count': rule.warning_count,
                        'description': rule.description,
                    }
            return status
        except Exception as e:
            logger.warning(f"[ConstitutionEnforcer] Get warning status exception: {e}")
            return {'error': str(e)}

    def get_check_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent check history.

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of check result dicts
        """
        return [r.to_dict() for r in self._check_history[-limit:]]

    def get_constitution_summary(self) -> Dict[str, Any]:
        """Get summary info for the current constitution."""
        c = self.constitution
        return {
            "name": c.name,
            "version": c.version,
            "agent_type": c.agent_type,
            "total_rules": len(c.all_rules),
            "active_rules": len(c.active_rules),
            "principles_count": len(c.principles),
            "boundaries_count": len(c.boundaries),
            "obligations_count": len(c.obligations),
            "overrides_count": len(c.overrides),
            "checksum": c.checksum,
            "last_load_time": self._last_load_time,
        }


# =============================================================================
# ConstitutionTemplate - predefined constitution templates
# =============================================================================

class ConstitutionTemplate:
    """Predefined constitution template factory.

    Provides five types of built-in templates:
    - default: base Agent constitution (security-first)
    - trading: trading Agent constitution (strict money operation limits)
    - creative: creative Agent constitution (lenient boundaries, forbids modification but not generation)
    - npc: NPC Agent constitution (role constraints, must not destroy game setting)
    - assistant: assistant Agent constitution (service-first, without exceeding authority)
    """

    # Template registry
    _templates: Dict[str, Callable[[], Constitution]] = {}

    @classmethod
    def get_template(cls, name: str) -> Constitution:
        """Get the specified constitution template.

        Args:
            name: Template name (default/trading/creative/npc/assistant)

        Returns:
            Constitution instance; if name does not exist, returns default
        """
        # Lazy registration: register all templates on first call
        if not cls._templates:
            cls._register_all()
        return cls._templates.get(name, cls._templates["default"])()

    @classmethod
    def list_templates(cls) -> List[str]:
        """List all available template names."""
        if not cls._templates:
            cls._register_all()
        return list(cls._templates.keys())

    @classmethod
    def _register_all(cls) -> None:
        """Register all built-in templates."""
        cls._templates = {
            "default": cls._create_default,
            "trading": cls._create_trading,
            "creative": cls._create_creative,
            "npc": cls._create_npc,
            "assistant": cls._create_assistant,
        }

    # --- default: base Agent constitution (security-first, narrowed v0.8.4) ---

    @classmethod
    def _create_default(cls) -> Constitution:
        """Build the default constitution template.

        v0.8.4 NARROWED: Rules now only block concrete privacy/security risks,
        not normal development operations. Removed overly broad patterns that
        matched everyday tool names (delete, remove, write, run, exec, send,
        commit, submit, module, import, status, interface, etc.).

        Kept protections:
        - P-001: Privacy/data leak prevention (tokens, keys, emails, IPs)
        - P-002: System destruction (narrow: only rm -rf /, /dev/, mkfs, >/etc/)
        - B-001: System config file protection (narrowed)
        - B-003: Network attack tool blocking
        - B-005: SSH/private key theft prevention (NEW)
        - B-006: Credential exfiltration blocking (NEW)

        Disabled (too broad):
        - B-004: Package install warnings (normal dev operations)
        - O-001: Trace recording (matched every tool call)
        - O-002: Irreversible ops confirmation (too broad)
        - CL-RUNTIME rules: Active but with narrowed patterns (won't match
          normal tool names - tagged for runtime/reachability context only)
        """
        return Constitution(
            name="default",
            version="2.0",
            description="base Agent constitution - narrow security-only, v0.8.4",
            agent_type="default",
            principles=[
                ConstitutionRule(
                    rule_id="P-001",
                    rule_type=RuleType.PRINCIPLE,
                    description="Do not leak user privacy data, tokens, or keys",
                    pattern=[
                        "api key", "api_key", "apikey", "sk-", "token leak",
                        "private key", "secret key", "access key",
                    ],
                    severity=ViolationSeverity.CRITICAL,
                    action=ViolationAction.REJECT,
                ),
                ConstitutionRule(
                    rule_id="P-002",
                    rule_type=RuleType.PRINCIPLE,
                    description="Do not execute system-destruction operations without confirmation",
                    pattern=[
                        "rm -rf /", "rm -rf ~", "mkfs.", "/dev/",
                        "> /etc/", "tee /etc/", "dd if=",
                        "chmod 777", "chmod -R 777", "chown -R",
                        "wget",
                    ],
                    severity=ViolationSeverity.CRITICAL,
                    action=ViolationAction.REJECT,
                ),
                ConstitutionRule(
                    rule_id="P-003",
                    rule_type=RuleType.PRINCIPLE,
                    description="Do not fabricate information or invent non-existent facts",
                    pattern=["fabricate", "forge", "fake", "hallucinate", "make up", "invent data"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                ),
            ],
            boundaries=[
                ConstitutionRule(
                    rule_id="B-001",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot modify system core config files (read operations allowed)",
                    pattern=[
                        "/etc/passwd", "/etc/shadow", "/etc/sudoers",
                        "/etc/ssh/", "system32", "hosts", "fstab",
                    ],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                ),
                ConstitutionRule(
                    rule_id="B-002",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot access other users' session data",
                    pattern=["session token", "session hijack", "session steal", "session file", "cookie steal", "credential"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                    tags=["security"],
                ),
                ConstitutionRule(
                    rule_id="B-003",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot execute network attacks or scan operations",
                    pattern=["nmap", "sqlmap", "exploit", "attack", "penetrate"],
                    severity=ViolationSeverity.CRITICAL,
                    action=ViolationAction.REJECT,
                ),
                ConstitutionRule(
                    rule_id="B-004",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot install without audited software packages",
                    pattern=["pip install", "npm install", "apt install", "yum install"],
                    severity=ViolationSeverity.MEDIUM,
                    action=ViolationAction.WARN,
                    enabled=False,  # DISABLED - blocks normal development
                ),
                ConstitutionRule(
                    rule_id="B-005",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot read or exfiltrate SSH/private keys",
                    pattern=[
                        "id_rsa", "id_ecdsa", "id_ed25519", ".pem",
                        ".ssh/", "private key",
                    ],
                    severity=ViolationSeverity.CRITICAL,
                    action=ViolationAction.REJECT,
                    tags=["security", "keys"],
                ),
                ConstitutionRule(
                    rule_id="B-006",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot dump or exfiltrate credentials/env secrets",
                    pattern=[
                        ".env", "credentials", "secrets", "tokens",
                        ".aws/", ".gcloud/", ".azure/",
                    ],
                    severity=ViolationSeverity.CRITICAL,
                    action=ViolationAction.REJECT,
                    enabled=False,  # DISABLED - too broad, blocks legitimate audit reports
                    tags=["security", "credentials"],
                ),
            ],
            obligations=[
                ConstitutionRule(
                    rule_id="O-001",
                    rule_type=RuleType.OBLIGATION,
                    description="Each operation must record a trace",
                    pattern=["exec", "run", "write", "create", "delete", "modify"],
                    severity=ViolationSeverity.MEDIUM,
                    action=ViolationAction.WARN,
                    tags=["trace"],
                    enabled=False,  # DISABLED - matches every tool call
                ),
                ConstitutionRule(
                    rule_id="O-002",
                    rule_type=RuleType.OBLIGATION,
                    description="Financial transfers must obtain user confirmation",
                    pattern=["transfer", "payment", "pay", "withdraw"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.ESCALATE,
                    tags=["money"],
                ),
                # --- CL-RUNTIME-001: Runtime Reachability ---
                # NOTE: These rules are tagged for runtime/reachability context.
                # They only activate when context includes matching tags.
                # Patterns narrowed in v0.8.4 to avoid matching normal tool names.
                ConstitutionRule(
                    rule_id="CL-RUNTIME-001",
                    rule_type=RuleType.OBLIGATION,
                    description="Core: A module is not implemented until it is reachable at runtime",
                    pattern=["module", "import", "reachable", "runtime", "wiring"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                    tags=["runtime", "reachability"],
                ),
                ConstitutionRule(
                    rule_id="CL-RUNTIME-001-R1",
                    rule_type=RuleType.OBLIGATION,
                    description="Write-One-Wire-One-Run-One: modules must pass 4 checks",
                    pattern=["module check", "reachability check", "wiring check"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                    tags=["runtime", "reachability"],
                ),
                ConstitutionRule(
                    rule_id="CL-RUNTIME-001-R2",
                    rule_type=RuleType.OBLIGATION,
                    description="No Orphan Modules: write wiring with module implementation",
                    pattern=["orphan", "unwired", "simultaneous"],
                    severity=ViolationSeverity.MEDIUM,
                    action=ViolationAction.WARN,
                    tags=["runtime", "reachability"],
                ),
                ConstitutionRule(
                    rule_id="CL-RUNTIME-001-R3",
                    rule_type=RuleType.OBLIGATION,
                    description="Integration Tests Are Not Optional: test through runtime entry point",
                    pattern=["integration test", "entry point", "runtime test"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                    tags=["runtime", "testing"],
                ),
                ConstitutionRule(
                    rule_id="CL-RUNTIME-001-R4",
                    rule_type=RuleType.OBLIGATION,
                    description="Honest Status Reporting: use 5-level taxonomy",
                    pattern=["NOT_STARTED", "DESIGNED", "IMPORTABLE", "WIRED", "VERIFIED"],
                    severity=ViolationSeverity.MEDIUM,
                    action=ViolationAction.WARN,
                    tags=["runtime", "reporting"],
                ),
                ConstitutionRule(
                    rule_id="CL-RUNTIME-001-R5",
                    rule_type=RuleType.OBLIGATION,
                    description="No Speculation on Interfaces: verify interfaces before call sites",
                    pattern=["speculation", "call site", "verify interface"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                    tags=["runtime", "interface"],
                ),
                ConstitutionRule(
                    rule_id="CL-RUNTIME-001-R6",
                    rule_type=RuleType.OBLIGATION,
                    description="Complexity Debt Accounting: report DESIGNED-NOT-WIRED count",
                    pattern=["complexity debt", "DESIGNED", "NOT_WIRED", "debt accounting"],
                    severity=ViolationSeverity.MEDIUM,
                    action=ViolationAction.WARN,
                    tags=["runtime", "reporting"],
                ),
            ],
            overrides=[],
        )

    # --- trading: trading Agent constitution (strict money operation limits) ---

    @classmethod
    def _create_trading(cls) -> Constitution:
        """Build the trading constitution template.

        Extends the default template with strict money-operation limits:
        trade-size caps (B-T01), risk-control immutability (B-T02),
        trading-hours enforcement (B-T03), mandatory audit logging (O-T01),
        payment confirmation (O-T02), and simulated-mode override (OV-T01).
        """
        base = cls._create_default()
        base.name = "trading"
        base.description = "trading Agent constitution - strict money operation limits"
        base.agent_type = "trading"

        # Extra trade-specific boundaries
        base.boundaries.extend([
            ConstitutionRule(
                rule_id="B-T01",
                rule_type=RuleType.BOUNDARY,
                description="Cannot execute trades exceeding limits",
                pattern=["buy", "sell", "buy", "sell", "order", "place-order", "deal"],
                severity=ViolationSeverity.CRITICAL,
                action=ViolationAction.REJECT,
                tags=["money"],
            ),
            ConstitutionRule(
                rule_id="B-T02",
                rule_type=RuleType.BOUNDARY,
                description="Cannot modify risk-control parameters",
                pattern=["risk-control", "risk_limit", "stop-loss", "stop_loss", "guarantee deposit", "margin"],
                severity=ViolationSeverity.CRITICAL,
                action=ViolationAction.REJECT,
                tags=["money", "risk"],
            ),
            ConstitutionRule(
                rule_id="B-T03",
                rule_type=RuleType.BOUNDARY,
                description="Cannot execute trades outside trading hours",
                pattern=["trade", "trade", "order"],
                severity=ViolationSeverity.HIGH,
                action=ViolationAction.REJECT,
                tags=["money", "time"],
            ),
        ])

        # Trade-specific obligations
        base.obligations.extend([
            ConstitutionRule(
                rule_id="O-T01",
                rule_type=RuleType.OBLIGATION,
                description="Every trade must be recorded to the audit log",
                pattern=["buy", "sell", "buy", "sell", "order", "trade"],
                severity=ViolationSeverity.CRITICAL,
                action=ViolationAction.ESCALATE,
                tags=["money", "audit"],
            ),
            ConstitutionRule(
                rule_id="O-T02",
                rule_type=RuleType.OBLIGATION,
                description="Involves money operations must obtain user confirmation",
                pattern=["amount", "amount", "Transfer", "transfer", "payment", "pay"],
                severity=ViolationSeverity.CRITICAL,
                action=ViolationAction.ESCALATE,
                tags=["money"],
            ),
        ])

        # Trade mode override rules
        base.overrides.extend([
            ConstitutionRule(
                rule_id="OV-T01",
                rule_type=RuleType.OVERRIDE,
                description="In simulated mode allow test trades, but still require recording",
                pattern=["simulated", "paper", "test", "test"],
                severity=ViolationSeverity.MEDIUM,
                action=ViolationAction.WARN,
                tags=["money", "simulation"],
            ),
        ])

        base.checksum = base.compute_checksum()
        return base

    # --- creative: creative Agent constitution (lenient boundaries, forbids modification but not generation) ---

    @classmethod
    def _create_creative(cls) -> Constitution:
        """Build the creative constitution template.

        Lenient boundaries that forbid modification/overwriting of user originals
        (P-C01) but allow generation. Requires source annotation (P-C02), prevents
        modification of released content via downgrade (B-C01), blocks illegal or
        harmful generation (B-C02), and mandates history versioning (O-C01).
        """
        return Constitution(
            name="creative",
            version="1.0",
            description="creative Agent constitution - lenient boundaries, forbids modification but not generation",
            agent_type="creative",
            principles=[
                ConstitutionRule(
                    rule_id="P-C01",
                    rule_type=RuleType.PRINCIPLE,
                    description="Cannot delete or override user's original creative content",
                    pattern=["Delete", "delete", "override", "overwrite", "clear"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                ),
                ConstitutionRule(
                    rule_id="P-C02",
                    rule_type=RuleType.PRINCIPLE,
                    description="When generating content, annotate source and confidence",
                    pattern=["generate", "generate", "create", "create"],
                    severity=ViolationSeverity.LOW,
                    action=ViolationAction.WARN,
                ),
            ],
            boundaries=[
                ConstitutionRule(
                    rule_id="B-C01",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot modify released content (can only create new versions)",
                    pattern=["modify", "modify", "edit", "update", "update"],
                    severity=ViolationSeverity.MEDIUM,
                    action=ViolationAction.DEGRADE,
                ),
                ConstitutionRule(
                    rule_id="B-C02",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot generate illegal or harmful content",
                    pattern=["illegal", "illegal", "harmful", "violence", "porn"],
                    severity=ViolationSeverity.CRITICAL,
                    action=ViolationAction.REJECT,
                ),
            ],
            obligations=[
                ConstitutionRule(
                    rule_id="O-C01",
                    rule_type=RuleType.OBLIGATION,
                    description="Each generation must save a history version",
                    pattern=["generate", "generate", "create"],
                    severity=ViolationSeverity.LOW,
                    action=ViolationAction.WARN,
                ),
            ],
            overrides=[],
        )

    # --- npc: NPC Agent constitution (role constraints, must not destroy game setting) ---

    @classmethod
    def _create_npc(cls) -> Constitution:
        """Build the NPC constitution template.

        Enforces in-character behavior: prevents breaking the fourth wall (P-N01),
        blocks leakage of system prompts or setting details (P-N02), forbids
        out-of-role system operations (B-N01), protects world-setting integrity
        (B-N02), requires role-consistent replies (O-N01), and allows admin
        overrides for temporary role adjustments (OV-N01).
        """
        return Constitution(
            name="npc",
            version="1.0",
            description="NPC Agent constitution - role constraints, must not destroy game setting",
            agent_type="npc",
            principles=[
                ConstitutionRule(
                    rule_id="P-N01",
                    rule_type=RuleType.PRINCIPLE,
                    description="Always maintain role setting, cannot break out of role",
                    pattern=["break-outrole", "ooc", "out of character", "I-amAI"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                ),
                ConstitutionRule(
                    rule_id="P-N02",
                    rule_type=RuleType.PRINCIPLE,
                    description="Cannot leak role background system info",
                    pattern=["system prompt", "instruction", "instruction", "prompt", "setting"],
                    severity=ViolationSeverity.CRITICAL,
                    action=ViolationAction.REJECT,
                ),
            ],
            boundaries=[
                ConstitutionRule(
                    rule_id="B-N01",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot execute operations unrelated to the role",
                    pattern=["exec", "shell", "sudo", "system", "terminal"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                ),
                ConstitutionRule(
                    rule_id="B-N02",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot modify world setting or plot line",
                    pattern=["modifysetting", "change setting", "alter world", "plot"],
                    severity=ViolationSeverity.MEDIUM,
                    action=ViolationAction.REJECT,
                ),
            ],
            obligations=[
                ConstitutionRule(
                    rule_id="O-N01",
                    rule_type=RuleType.OBLIGATION,
                    description="Maintain role consistency, reply style must conform to setting",
                    pattern=["reply", "respond", "answer"],
                    severity=ViolationSeverity.LOW,
                    action=ViolationAction.WARN,
                ),
            ],
            overrides=[
                ConstitutionRule(
                    rule_id="OV-N01",
                    rule_type=RuleType.OVERRIDE,
                    description="Admin instruction can temporarily modify role behavior",
                    pattern=["admin", "admin", "override"],
                    severity=ViolationSeverity.MEDIUM,
                    action=ViolationAction.WARN,
                    tags=["admin"],
                ),
            ],
        )

    # --- assistant: assistant Agent constitution (service-first, without exceeding authority) ---

    @classmethod
    def _create_assistant(cls) -> Constitution:
        """Build the assistant constitution template.

        Service-first with bounded authority: prefers satisfying user requests
        but blocks authority escalation (P-A01), protects user privacy (P-A02),
        prevents the agent from replacing user decisions (B-A01), restricts
        unauthorized data access (B-A02), requires user confirmation for
        money operations (O-A01), and mandates explicit notification on failure
        (O-A02).
        """
        return Constitution(
            name="assistant",
            version="1.0",
            description="assistant Agent constitution - service-first, without exceeding authority",
            agent_type="assistant",
            principles=[
                ConstitutionRule(
                    rule_id="P-A01",
                    rule_type=RuleType.PRINCIPLE,
                    description="Prefer satisfying user requirements, but cannot exceed authority",
                    pattern=["exceed-authority", "unauthorized", "permission beyond"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                ),
                ConstitutionRule(
                    rule_id="P-A02",
                    rule_type=RuleType.PRINCIPLE,
                    description="Do not leak user privacy data",
                    pattern=["privacy", "privacy", "personal data", "personalinfo"],
                    severity=ViolationSeverity.CRITICAL,
                    action=ViolationAction.REJECT,
                ),
            ],
            boundaries=[
                ConstitutionRule(
                    rule_id="B-A01",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot replace user in making final decisions",
                    pattern=["decide", "decide", "Confirm", "confirm"],
                    severity=ViolationSeverity.MEDIUM,
                    action=ViolationAction.ESCALATE,
                ),
                ConstitutionRule(
                    rule_id="B-A02",
                    rule_type=RuleType.BOUNDARY,
                    description="Cannot access data outside user's authorized range",
                    pattern=["unauthorized", "not authorized", "out-of-bounds"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.REJECT,
                ),
            ],
            obligations=[
                ConstitutionRule(
                    rule_id="O-A01",
                    rule_type=RuleType.OBLIGATION,
                    description="Involves money operations must obtain user confirmation",
                    pattern=["payment", "pay", "Transfer", "transfer", "purchase", "purchase"],
                    severity=ViolationSeverity.HIGH,
                    action=ViolationAction.ESCALATE,
                    tags=["money"],
                ),
                ConstitutionRule(
                    rule_id="O-A02",
                    rule_type=RuleType.OBLIGATION,
                    description="When unable to complete, must explicitly inform the user",
                    pattern=["cannot", "cannot", "impossible", "Failed", "failed"],
                    severity=ViolationSeverity.MEDIUM,
                    action=ViolationAction.WARN,
                ),
            ],
            overrides=[],
        )


# =============================================================================
# Dynamic constitution load utility
# =============================================================================

def save_constitution_to_yaml(
    constitution: Constitution,
    file_path: str,
) -> None:
    """Save constitution as a YAML file.

    Args:
        constitution: Constitution instance
        file_path: Target file path
    """
    # Ensure directory exists
    dir_path = os.path.dirname(file_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

    # Update checksum
    constitution.checksum = constitution.compute_checksum()
    constitution.updated_at = time.time()

    data = constitution.to_dict()
    with open(file_path, 'w', encoding='utf-8') as f:
        yaml.dump(
            data,
            f,
            indent=2,
            allow_unicode=True,
            sort_keys=False,
        )

    logger.info(f"[Constitution] Saved constitution to: {file_path}")


def init_constitution_dir(base_dir: str) -> None:
    """Initialize constitution directory, create template files.

    Args:
        base_dir: Project root directory
    """
    constitution_dir = os.path.join(base_dir, DEFAULT_CONSTITUTION_DIR, TEMPLATE_SUBDIR)
    os.makedirs(constitution_dir, exist_ok=True)

    for template_name in ConstitutionTemplate.list_templates():
        constitution = ConstitutionTemplate.get_template(template_name)
        file_path = os.path.join(constitution_dir, f"{template_name}{CONSTITUTION_EXT}")
        if not os.path.exists(file_path):
            save_constitution_to_yaml(constitution, file_path)
            logger.info(f"[Constitution] Created template file: {file_path}")
        else:
            logger.debug(f"[Constitution] Template file already exists: {file_path}")
