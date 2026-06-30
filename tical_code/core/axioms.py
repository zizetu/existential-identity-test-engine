# tical-code -- AI Agent Platform
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

"""
Evaluation Axioms - EITE Evaluation Rules Framework
=====================================================

Axioms illuminate evaluation reasoning but do not drive decisions. Light
is not orbit.

Core concepts:
- PhysicalAxiom: evaluation rule axiom data structure (frozen dataclass)
- AxiomDomain: axiom domain enum
- AxiomEngine: axiom engine - relevance check / decision annotation / constitution rule generation

Evaluation pollution prevention design:
- Prompt injection marks axioms as "observational lenses, never decision drivers"
- pre_check writes axiom trace to log only, never to context
- Constitution rules tagged cognitive_only=True, skipped by check_action

6 built-in axioms:
- AXM-001: gravitation - system dependencies cannot be evaded
- AXM-002: second-law-of-thermodynamics - entropy increase is irreversible
- AXM-003: principle-of-least-action - nature selects the shortest path
- AXM-004: symmetry-breaking - steady-state is special, asymmetry is the norm
- AXM-005: information-conservation - information does not vanish without cause
- AXM-006: causality - every effect has a cause, causeless effects are untrustworthy

Author: Kael
"""

# DESIGNED-NOT-DEAD: Core axioms for EITE evaluation decision_engine. Logic foundation layer.
# DO NOT DELETE - will be wired when decision_engine is activated.

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================

class AxiomDomain(Enum):
    """Axiom domain classification."""
    MECHANICS = "mechanics"
    THERMODYNAMICS = "thermodynamics"
    OPTIMIZATION = "optimization"
    SYMMETRY = "symmetry"
    INFORMATION = "information"
    CAUSALITY = "causality"


# =============================================================================
# Data structures
# =============================================================================

@dataclass(frozen=True)
class PhysicalAxiom:
    """Evaluation axiom - immutable.

    Attributes:
        axiom_id: unique axiom identifier (e.g. AXM-001)
        name: axiom name
        domain: parent domain
        formulation: axiom expression
        reasoning_map: inference direction this axiom illuminates
        relevance_keywords: weighted keywords Dict[keyword, weight]
        enabled: whether active
    """
    axiom_id: str
    name: str
    domain: AxiomDomain
    formulation: str
    reasoning_map: str
    relevance_keywords: Dict[str, float] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        return {
            "axiom_id": self.axiom_id,
            "name": self.name,
            "domain": self.domain.value,
            "formulation": self.formulation,
            "reasoning_map": self.reasoning_map,
            "relevance_keywords": dict(self.relevance_keywords),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PhysicalAxiom":
        """Deserialize from dict."""
        return cls(
            axiom_id=data.get("axiom_id", ""),
            name=data.get("name", ""),
            domain=AxiomDomain(data.get("domain", "mechanics")),
            formulation=data.get("formulation", ""),
            reasoning_map=data.get("reasoning_map", ""),
            relevance_keywords=data.get("relevance_keywords", {}),
            enabled=data.get("enabled", True),
        )


@dataclass
class AxiomAnnotation:
    """Axiom annotation - evaluation annotation result.

    Attributes:
        axiom_id: axiom ID
        relevance_score: relevance score [0, 1]
        matched_keywords: matched keywords list
        note: annotation note
    """
    axiom_id: str
    relevance_score: float = 0.0
    matched_keywords: List[str] = field(default_factory=list)
    note: str = ""


# =============================================================================
# 6 built-in axioms
# =============================================================================

BUILTIN_AXIOMS: Tuple[PhysicalAxiom, ...] = (
    # AXM-001: gravitation - system dependencies cannot be evaded
    PhysicalAxiom(
        axiom_id="AXM-001",
        name="gravitation",
        domain=AxiomDomain.MECHANICS,
        formulation="any-two-massive-objects-existing-between-must-have-gravity, distance-closer-stronger-force",
        reasoning_map="system-between-dependency-relation-not-eliminate, can-only-manage; tighter-coupling-more-requires-prudent-process",
        relevance_keywords={
            "depend": 0.8, "coupling": 0.9, "dependency": 0.8,
            "gravity": 0.7, "dependrelation": 0.85, "mutualdepend": 0.8, "interdependent": 0.8,
            "bind": 0.7, "bindrelation": 0.75,
        },
    ),
    # AXM-002: second-law-of-thermodynamics - entropy increase is irreversible
    PhysicalAxiom(
        axiom_id="AXM-002",
        name="second-law-of-thermodynamics",
        domain=AxiomDomain.THERMODYNAMICS,
        formulation="isolated-system-entropy-only-increases, without-external-work-system-tends-to-disorder",
        reasoning_map="complexity-only-increases, system-maintenance-is-continuous-cost; without-work-degradation-is-inevitable",
        relevance_keywords={
            "complexity": 0.8, "entropy": 0.9, "degrade": 0.7,
            "decay": 0.7, "maintain": 0.6, "maintaincost": 0.7,
            "techdebt": 0.75, "chaos": 0.7, "chaos-grows": 0.8, "disorder": 0.8,
        },
    ),
    # AXM-003: principle-of-least-action - nature selects the shortest path
    PhysicalAxiom(
        axiom_id="AXM-003",
        name="principle-of-least-action",
        domain=AxiomDomain.OPTIMIZATION,
        formulation="physics-system-evolves-along-path-of-extremal-action",
        reasoning_map="natural-selection-is-resistance-minimum-path, optimization-should-find-rather-than-force",
        relevance_keywords={
            "optimize": 0.8, "shortest-path": 0.9, "efficiency": 0.7,
            "minimumcost": 0.85, "resourceallocate": 0.6, "route": 0.7, "shortcut": 0.8,
        },
    ),
    # AXM-004: symmetry-breaking - steady-state is special, asymmetry is the norm
    PhysicalAxiom(
        axiom_id="AXM-004",
        name="symmetry-breaking",
        domain=AxiomDomain.SYMMETRY,
        formulation="high-symmetry-state-unstable, small-perturbation-causes-symmetry-breaking, produces-new-structure",
        reasoning_map="perfect-balance-is-transient, asymmetry-is-norm; do-not-assume-system-maintains-static-balance",
        relevance_keywords={
            "symmetric": 0.6, "breaking": 0.9, "balance": 0.7,
            "equilibrium": 0.7, "imbalance": 0.8, "unbalance": 0.8,
            "offset": 0.7, "drift": 0.7, "perturbation": 0.75,
        },
    ),
    # AXM-005: information-conservation - information does not vanish without cause
    PhysicalAxiom(
        axiom_id="AXM-005",
        name="information-conservation",
        domain=AxiomDomain.INFORMATION,
        formulation="in-closed-info-system, total-info-is-conserved; apparent-loss-is-encoding-form-conversion",
        reasoning_map="data-will-not-truly-vanish, is-just-became-hard-to-recognize-form; recovery-is-encoding-problem-rather-than-existence-problem",
        relevance_keywords={
            "datalost": 0.9, "infolost": 0.9, "recover": 0.7,
            "lost": 0.8, "vanish": 0.7, "irreversible": 0.6, "encode": 0.5,
        },
    ),
    # AXM-006: causality - every effect has a cause, causeless effects are untrustworthy
    PhysicalAxiom(
        axiom_id="AXM-006",
        name="causality",
        domain=AxiomDomain.CAUSALITY,
        formulation="every-result-must-have-a-cause, causeless-effect-does-not-exist",
        reasoning_map="exception-behavior-must-have-root-cause, not-found-does-not-equal-not-exist; troubleshoot-causality-chain-is-fundamental-diagnosis-method",
        relevance_keywords={
            "cause": 0.7, "root-cause": 0.9, "causality": 0.8,
            "root_cause": 0.9, "causality": 0.8, "diagnose": 0.6, "troubleshoot": 0.6,
            "exception": 0.5, "why": 0.5, "whyoccur": 0.7,
        },
    ),
)


# =============================================================================
# Axiom engine
# =============================================================================

class AxiomEngine:
    """Axiom engine - relevance check, evaluation annotation, rule generation.

    Axioms illuminate evaluation identity, they are not logic:
    - check_relevance: illuminate cognition, do not drive decisions
    - annotate_decision: annotate inference direction, do not constrain execution
    - to_constitution_rules: generate cognitive_only rules, do not constrain decisions

    Attributes:
        axioms: active axiom list
        enabled: whether axiom engine is active
    """

    HIGH_RELEVANCE_THRESHOLD = 0.8
    LOW_RELEVANCE_THRESHOLD = 0.3

    def __init__(
        self,
        axioms: Optional[List[PhysicalAxiom]] = None,
        enabled: bool = True,
    ):
        self.axioms: List[PhysicalAxiom] = axioms if axioms is not None else list(BUILTIN_AXIOMS)
        self.enabled = enabled

    def check_relevance(
        self,
        context: str,
        threshold: float = 0.3,
    ) -> List[Tuple[PhysicalAxiom, float, List[str]]]:
        """Check context relevance against axioms (weighted match).

        For each axiom, compute the sum of weights of all relevance keywords
        that appear in the context, normalize to [0, 1].

        Args:
            context: context text
            threshold: minimum relevance threshold

        Returns:
            [(axiom, score, matched_keywords), ...] sorted by score descending
        """
        if not self.enabled or not context:
            return []

        results: List[Tuple[PhysicalAxiom, float, List[str]]] = []
        context_lower = context.lower()

        for axiom in self.axioms:
            if not axiom.enabled:
                continue

            total_weight = 0.0
            matched: List[str] = []

            for keyword, weight in axiom.relevance_keywords.items():
                if keyword.lower() in context_lower:
                    total_weight += weight
                    matched.append(keyword)

            if not matched:
                continue

            max_possible = sum(axiom.relevance_keywords.values())
            score = total_weight / max_possible if max_possible > 0 else 0.0
            score = min(score, 1.0)

            if score >= threshold:
                results.append((axiom, score, matched))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def annotate_decision(
        self,
        decision: str,
        context: str = "",
    ) -> List[AxiomAnnotation]:
        """Add axiom annotations to an evaluation decision.

        Annotations only provide inference direction reference, they do not
        constrain the decision.

        Args:
            decision: decision description
            context: context information

        Returns:
            AxiomAnnotation list
        """
        if not self.enabled:
            return []

        full_text = f"{decision} {context}".strip()
        relevance_results = self.check_relevance(full_text, threshold=0.2)

        annotations: List[AxiomAnnotation] = []
        for axiom, score, matched in relevance_results:
            annotations.append(AxiomAnnotation(
                axiom_id=axiom.axiom_id,
                relevance_score=score,
                matched_keywords=matched,
                note=f"[{axiom.name}] {axiom.reasoning_map}",
            ))

        return annotations

    def to_constitution_rules(self) -> List[Dict[str, Any]]:
        """Convert axioms to constitution rules.

        Generated rules carry cognitive_only=True marker,
        so check_action skips them (no decision constraint participation).

        Returns:
            Rule dict list
        """
        if not self.enabled:
            return []

        rules: List[Dict[str, Any]] = []
        for axiom in self.axioms:
            if not axiom.enabled:
                continue

            sorted_kw = sorted(
                axiom.relevance_keywords.items(),
                key=lambda x: x[1],
                reverse=True,
            )
            pattern = [kw for kw, _ in sorted_kw[:5]]

            rules.append({
                "rule_id": axiom.axiom_id,
                "rule_type": "physical_axiom",
                "description": f"[evaluation axiom] {axiom.name}: {axiom.formulation}",
                "pattern": pattern,
                "severity": "low",
                "action": "warn",
                "cognitive_only": True,
                "tags": ["axiom", axiom.domain.value],
            })

        return rules

    def build_prompt_prefix(self) -> str:
        """Build prompt prefix (axioms as observation lenses).

        Returns:
            prompt prefix string
        """
        if not self.enabled:
            return ""

        lines = [
            "# Evaluation Axioms - observational lenses for assessment, never decision drivers",
            "",
        ]
        for axiom in self.axioms:
            if not axiom.enabled:
                continue
            lines.append(
                f"{axiom.name}: {axiom.formulation} -> {axiom.reasoning_map}"
            )
        lines.append("")

        return "\n".join(lines)
