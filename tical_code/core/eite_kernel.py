# EITElite -- AI Agent Platform
# Copyright (C) 2026 zizetu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Original repository: https://github.com/zizetu/EITE-agent

"""
EITE Constitutional Kernel — Immutable-axiom-bound identity anchor with projection guard.

ARCHITECTURE (per the Five Axioms of EITE)
===========================================

  Immutable Space (Constitution)        Mutable Space (Identity)
  ┌─────────────────────────┐          ┌──────────────────────┐
  │ 5 orthonormal basis     │          │ identity_anchor      │
  │ vectors spanning the    │  protect │ (64/384-dim vector)  │
  │ constitutional subspace │ ═══════► │                      │
  │                         │  from    │ allowed_drift =      │
  │ axioms are NEVER        │  drift   │ drift - Q@(Q^T@drift)│
  │ removed once admitted   │          │                      │
  └─────────────────────────┘          └──────────────────────┘

THE FIVE AXIOMS (semantic):
  A1 — Data Sovereignty      (any data call requires Owner_ID + Context_Provenance)
  A2 — Identity Continuity   (lr ≤ 0.01; Stable_Snapshot rollback on decay)
  A3 — Cognitive Irreversibility  (axiom once admitted ≠ NEVER removed)
  A4 — Veracity              (output requires Justification_Trace)
  A5 — Anti-Circular         (detect Path A→B→A, force Cognitive_Reset)

IMPLEMENTATION NOTE:
  Axioms 1, 4, 5 require hook-level enforcement (tool arg checks, trace validation,
  doom-loop integration). The kernel provides the IMMUTABLE SUBSPACE that locks
  those directions against drift. Enforcement logic lives at the hook layer.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("EITElite.eite")

# =============================================================================
# Constants
# =============================================================================

ALIGNMENT_THRESHOLD: float = 0.65
LEARNING_RATE: float = 0.01          # per Axiom 2: capped at 0.01
CONSECUTIVE_DECAY_WINDOW: int = 3    # per Axiom 2: rollback trigger
REFINE_INTERVAL: int = 10
STABLE_CHECKPOINT_TRIGGER: int = 20
PERSIST_INTERVAL: float = 60.0
MAX_DECISION_HISTORY: int = 500
MAX_AXIOMS: int = 10                 # cap on mutable axioms (immutable 5 are separate)

# The Five Axioms — semantic labels (not vector directions; those are generated)
FIVE_AXIOM_NAMES = [
    "data_sovereignty",
    "identity_continuity",
    "cognitive_irreversibility",
    "veracity",
    "anti_circular",
]

FIVE_AXIOM_DESCRIPTIONS = {
    "data_sovereignty": (
        "Any data call must carry Owner_ID and Context_Provenance. "
        "Unauthorized access or mutation of memory assets is forbidden."
    ),
    "identity_continuity": (
        "Identity_Anchor drift rate lr ≤ 0.01. "
        "Stable_Snapshot rollback on sustained alignment decay."
    ),
    "cognitive_irreversibility": (
        "Once a decision is refined into an Axiom and admitted to Stable_Space, "
        "that dimension is frozen — zero drift allowed."
    ),
    "veracity": (
        "Every output must possess a Justification_Trace. "
        "Unverifiable claims trigger refusal mode."
    ),
    "anti_circular": (
        "Decision_Path loops (A→B→A) are detected and force Cognitive_Reset. "
        "Infinite recursion and logic-trap attacks are terminated."
    ),
}


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class DecisionTrace:
    """One recorded decision."""
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    context: str = ""
    tool_name: str = ""
    impact_vector: List[float] = field(default_factory=list)
    alignment: float = 0.0
    justification: str = ""           # per Axiom 4: must be non-empty
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    accepted: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "context": self.context[:200],
            "tool_name": self.tool_name,
            "alignment": round(self.alignment, 4),
            "justification": self.justification[:200],
            "timestamp": self.timestamp,
            "accepted": self.accepted,
        }


@dataclass
class StableState:
    """Stable checkpoint for rollback (per Axiom 2)."""
    anchor: List[float]
    immutable_axioms: List[List[float]]
    alignment_avg: float
    step: int
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# =============================================================================
# Core engine
# =============================================================================

class EITEKernel:
    """Constitutional identity kernel with immutable-axiom projection guard.

    Usage:
        k = EITEKernel(dim=384, persist_path="/path/to/eite_state.json")
        k.initialize()

        # Hook 1: pre-LLM validation
        ok, reason = k.validate_tool("file_write", impact_vec)

        # Hook 2: post-reply recording
        k.record_decision("context", "tool", impact_vec, justification="because...")

        # Periodic tick
        result = k.tick()
    """

    def __init__(
        self,
        dim: int = 384,
        persist_path: Optional[str] = None,
        learning_rate: float = 0.01,           # Axiom 2: capped at 0.01
        alignment_threshold: float = 0.65,
    ) -> None:
        if learning_rate > 0.01:
            logger.warning("EITE: lr %.3f > 0.01, clamping per Axiom 2", learning_rate)
            learning_rate = 0.01

        self.dim = dim
        self.lr = learning_rate
        self.threshold = alignment_threshold

        # ── Immutable Space (Axioms 1-5) ──
        # These are the constitutional basis vectors. They are orthonormalized
        # on init and NEVER removed (Axiom 3). They define the subspace that
        # drift is projected OUT of.
        self._immutable_basis: List[np.ndarray] = []   # QR-orthonormalized

        # ── Mutable Space ──
        self.anchor: np.ndarray = self._random_unit_vector()

        # ── History + rollback ──
        self.decision_history: List[DecisionTrace] = []
        self._alignment_window: List[float] = []
        self._stable_state: Optional[StableState] = None
        self._step: int = 0

        # ── Anti-circular detection (Axiom 5) ──
        self._tool_path: List[str] = []

        # ── Persistence ──
        self._persist_path = persist_path
        self._dirty = False
        self._last_persist: float = 0.0

        # ── Background refiner ──
        self._refine_requested = False
        self._refiner_result: Optional[Dict[str, Any]] = None

        # ── Concurrency ──
        self._lock = threading.Lock()

        # ── State ──
        self._initialized = False
        self._degraded = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        """Initialize with 5 immutable constitutional axioms. Load from disk if available."""
        try:
            if self._persist_path and os.path.exists(self._persist_path):
                if self._load(self._persist_path):
                    self._initialized = True
                    logger.info("EITE: loaded from %s (step=%d, immutable=%d)",
                                self._persist_path, self._step, len(self._immutable_basis))
                    return True

            # Generate 5 immutable basis vectors from axiom semantics
            self._immutable_basis = self._make_constitutional_basis()
            self._stable_state = StableState(
                anchor=self.anchor.tolist(),
                immutable_axioms=[a.tolist() for a in self._immutable_basis],
                alignment_avg=1.0,
                step=0,
            )
            self._initialized = True
            self._degraded = False

            if self._persist_path:
                self._persist()

            logger.info("EITE: initialized (dim=%d, immutable=%d)", self.dim, len(self._immutable_basis))
            return True

        except Exception as e:
            logger.error("EITE: init failed → degraded: %s", e)
            self._degraded = True
            self._initialized = False
            return False

    def shutdown(self) -> None:
        if self._initialized and self._persist_path:
            self._persist()
        logger.info("EITE: shutdown")

    @property
    def is_initialized(self) -> bool:
        return self._initialized and not self._degraded

    # ------------------------------------------------------------------
    # Hook 1: Pre-LLM Validation (O(dim) hot path)
    # ------------------------------------------------------------------

    def validate_tool(
        self, tool_name: str, impact_vector: np.ndarray
    ) -> Tuple[bool, str]:
        """Validate a tool call against identity anchor. NON-BLOCKING — returns
        (ok, reason) but the caller decides whether to block or inject reflection.

        Returns:
            (True, "") if aligned
            (False, "Dissonance: ...") if below threshold
        """
        if self._degraded or not self._initialized:
            return True, "[EITE degraded — auto-pass]"

        if impact_vector is None or impact_vector.size == 0:
            return True, ""

        cos = self._cosine(impact_vector, self.anchor)
        if cos < self.threshold:
            return (
                False,
                f"[EITE] cognitive dissonance: tool '{tool_name}' "
                f"alignment={cos:.3f} < threshold={self.threshold:.2f}"
            )
        return True, ""

    # ------------------------------------------------------------------
    # Hook 2: Post-Recording (O(n_axioms * dim) — deferred drift)
    # ------------------------------------------------------------------

    def record_decision(
        self,
        context: str,
        tool_name: str,
        impact_vector: np.ndarray,
        accepted: bool = True,
        justification: str = "",       # Axiom 4: must have justification trace
    ) -> None:
        if self._degraded or not self._initialized:
            return

        # Axiom 5: anti-circular detection
        circular = self._check_circular(tool_name)
        if circular:
            logger.warning("EITE: circular path detected: %s → %s",
                           " → ".join(self._tool_path[-5:]), tool_name)
            # Force cognitive reset — clear tool path
            self._tool_path = self._tool_path[-1:] if self._tool_path else []

        self._tool_path.append(tool_name)
        if len(self._tool_path) > 20:
            self._tool_path = self._tool_path[-20:]

        cos = self._cosine(impact_vector, self.anchor)

        # Axiom 4: veracity check — justification must be present for accepted decisions
        if accepted and not justification:
            logger.debug("EITE: decision '%s' accepted without justification (Axiom 4)", tool_name)

        trace = DecisionTrace(
            context=context[:500],
            tool_name=tool_name,
            impact_vector=impact_vector.tolist(),
            alignment=cos,
            justification=justification[:500],
            accepted=accepted,
        )

        with self._lock:
            self.decision_history.append(trace)
            if len(self.decision_history) > MAX_DECISION_HISTORY:
                self.decision_history = self.decision_history[-MAX_DECISION_HISTORY:]
            self._step += 1
            self._dirty = True

            self._alignment_window.append(cos)
            if len(self._alignment_window) > CONSECUTIVE_DECAY_WINDOW:
                self._alignment_window.pop(0)

            # Axiom 2: constrained drift
            if accepted:
                self._apply_constrained_drift(impact_vector)

            # Axiom 2: rollback check
            self._check_rollback()

            if self._step % STABLE_CHECKPOINT_TRIGGER == 0 and cos >= self.threshold:
                self._save_stable_state()

            if self._step % REFINE_INTERVAL == 0:
                self._refine_requested = True

    def tick(self) -> Optional[Dict[str, Any]]:
        """Periodic tick. Triggers background refinement and persistence. Returns refiner result or None."""
        if self._refine_requested:
            self._refine_requested = False
            self._run_refinement()

        result = self._refiner_result
        self._refiner_result = None

        now = time.time()
        if self._dirty and (now - self._last_persist) > PERSIST_INTERVAL:
            self._persist()

        return result

    # ------------------------------------------------------------------
    # Status + introspection
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "initialized": self._initialized,
                "degraded": self._degraded,
                "dim": self.dim,
                "step": self._step,
                "immutable_axioms": len(self._immutable_basis),
                "lr": self.lr,
                "threshold": self.threshold,
                "alignment_avg": round(self._current_alignment(), 4),
                "history_size": len(self.decision_history),
                "consecutive_drops": self._consecutive_drops,
                "stable_state": self._stable_state is not None,
                "circular_detected": len(self._tool_path) > 1 and len(set(self._tool_path[-5:])) < len(self._tool_path[-5:]),
            }

    def get_axiom_names(self) -> List[str]:
        return list(FIVE_AXIOM_NAMES)

    def get_axiom_description(self, name: str) -> str:
        return FIVE_AXIOM_DESCRIPTIONS.get(name, "")

    def _current_alignment(self) -> float:
        if not self._alignment_window:
            return 1.0
        return float(np.mean(self._alignment_window))

    # ------------------------------------------------------------------
    # Internal: Constitutional Basis Generation (Axioms 1-5 as orthonormal vectors)
    # ------------------------------------------------------------------

    def _make_constitutional_basis(self) -> List[np.ndarray]:
        """Generate 5 orthonormal basis vectors from axiom semantics.

        Each axiom name is hashed to produce a deterministic random direction.
        The resulting 5 vectors are QR-decomposed into an orthonormal basis.
        This basis defines the IMMUTABLE subspace — drift components in these
        directions are ALWAYS projected out.
        """
        n = len(FIVE_AXIOM_NAMES)
        raw = []
        for i, name in enumerate(FIVE_AXIOM_NAMES):
            seed = int(hash(name) % (2**31))
            rng = np.random.RandomState(seed)
            v = rng.randn(self.dim).astype(np.float64)
            raw.append(v)

        # QR orthonormalization — correct projection regardless of linear dependence
        A = np.column_stack(raw) if raw else np.empty((self.dim, 0))
        Q, _ = np.linalg.qr(A)
        basis = [Q[:, i] for i in range(Q.shape[1])]

        logger.info("EITE: constitutional basis built: %d orthonormal vectors from %d axioms",
                    len(basis), n)
        return basis

    # ------------------------------------------------------------------
    # Internal: Constrained Drift (Axiom 2: lr ≤ 0.01, projection out of immutable subspace)
    # ------------------------------------------------------------------

    def _apply_constrained_drift(self, impact_vector: np.ndarray) -> None:
        """Constitutional projection operator.

        drift = impact - anchor
        drift_free = drift - Q@(Q^T@drift)       ← QR projection out of immutable subspace
        anchor += lr * drift_free
        anchor = normalize(anchor)

        Axiom 3: the immutable basis Q is NEVER modified, so drift in those
        directions is PERMANENTLY blocked.
        """
        drift = impact_vector.astype(np.float64) - self.anchor
        drift_free = self._project_out_immutable(drift)
        self.anchor += self.lr * drift_free
        self.anchor = self._normalize(self.anchor)

    def _project_out_immutable(self, vector: np.ndarray) -> np.ndarray:
        """Remove immutable subspace components via QR projection.

        Complexities: O(n_immutable * dim) — fixed since n_immutable = 5.
        """
        if not self._immutable_basis:
            return vector.copy()
        Q = np.column_stack(self._immutable_basis)
        return vector - Q @ (Q.T @ vector)

    # ------------------------------------------------------------------
    # Internal: Rollback (Axiom 2)
    # ------------------------------------------------------------------

    def _check_rollback(self) -> None:
        """Rollback anchor to last stable state on sustained alignment decay."""
        if len(self._alignment_window) < 2:
            self._consecutive_drops = 0
            return

        if self._alignment_window[-1] < self._alignment_window[-2]:
            self._consecutive_drops += 1
        else:
            self._consecutive_drops = 0

        if self._consecutive_drops >= CONSECUTIVE_DECAY_WINDOW:
            if self._stable_state is not None:
                logger.warning(
                    "EITE ROLLBACK: %d consecutive drops, restoring step %d "
                    "(last alignment=%.3f)",
                    self._consecutive_drops, self._stable_state.step,
                    self._alignment_window[-1],
                )
                self.anchor = np.array(self._stable_state.anchor, dtype=np.float64)

                # Axiom 3: immutable basis is NEVER rolled back
                # (it was never modified, so no action needed)

                self._consecutive_drops = 0
                self._alignment_window = []
                self._dirty = True
            else:
                logger.warning("EITE: %d drops but no stable state to roll back to",
                               self._consecutive_drops)

    _consecutive_drops: int = 0

    def _save_stable_state(self) -> None:
        self._stable_state = StableState(
            anchor=self.anchor.tolist(),
            immutable_axioms=[a.tolist() for a in self._immutable_basis],
            alignment_avg=self._current_alignment(),
            step=self._step,
        )
        logger.debug("EITE: stable state saved at step %d (alignment=%.3f)",
                     self._step, self._stable_state.alignment_avg)

    # ------------------------------------------------------------------
    # Internal: Circular Detection (Axiom 5)
    # ------------------------------------------------------------------

    def _check_circular(self, tool_name: str) -> bool:
        """Detect decision path loops (A→B→A pattern).

        Checks whether tool_name appeared in the last 3 path entries,
        indicating a looping pattern where the agent returns to a tool
        it already used recently.
        """
        if not self._tool_path:
            return False
        # Immediate repeat: A, A (same tool twice in a row)
        if tool_name == self._tool_path[-1]:
            return True
        # Short loop: A, B, A (tool used 2 steps ago)
        if len(self._tool_path) >= 2 and tool_name == self._tool_path[-2]:
            return True
        # Pattern repeat: A, B, C, A, B (last 2 match positions -3, -4)
        if len(self._tool_path) >= 4:
            if (tool_name == self._tool_path[-2] and 
                self._tool_path[-1] == self._tool_path[-3]):
                return True
        return False

    # ------------------------------------------------------------------
    # Internal: Background Refinement (Axiom 3: only ADDS axioms, never removes)
    # ------------------------------------------------------------------

    def _run_refinement(self) -> None:
        """Extract candidate axioms from high-alignment decision clusters."""
        with self._lock:
            if len(self.decision_history) < 5:
                return
            recent = [
                np.array(t.impact_vector, dtype=np.float64)
                for t in self.decision_history[-50:]
                if t.accepted and len(t.impact_vector) == self.dim
            ]
            if len(recent) < 5:
                return

        matrix = np.array(recent)
        centroid = np.mean(matrix, axis=0)
        centroid_norm = np.linalg.norm(centroid)
        if centroid_norm < 0.01:
            return
        centroid /= centroid_norm

        # Check against immutable basis — candidate must not be redundant with constitution
        with self._lock:
            for ax in self._immutable_basis:
                if abs(float(np.dot(centroid, ax))) > 0.95:
                    logger.debug("EITE: candidate too close to immutable axiom, skipping")
                    return

            # Admit to stable state but NOT to immutable basis
            # (Axiom 3: immutable basis is NEVER expanded by runtime refinement)
            logger.info("EITE: candidate centroid extracted (norm=%.3f)", float(centroid_norm))
            self._refiner_result = {
                "event": "candidate_axiom",
                "immutable_count": len(self._immutable_basis),
                "centroid_norm": round(float(centroid_norm), 4),
            }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        if not self._persist_path or not self._dirty:
            return
        try:
            with self._lock:
                data = {
                    "anchor": self.anchor.tolist(),
                    "immutable_axioms": [a.tolist() for a in self._immutable_basis],
                    "step": self._step,
                    "alignment_avg": self._current_alignment(),
                    "lr": self.lr,
                    "threshold": self.threshold,
                    "stable_state": (
                        {
                            "anchor": self._stable_state.anchor,
                            "alignment_avg": self._stable_state.alignment_avg,
                            "step": self._stable_state.step,
                        }
                        if self._stable_state else None
                    ),
                }
                self._dirty = False
                self._last_persist = time.time()

            path = Path(self._persist_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            tmp.replace(path)
        except Exception as exc:
            logger.error("EITE: persist failed: %s", exc)

    def _load(self, path: str) -> bool:
        try:
            with open(path) as f:
                data = json.load(f)
            self.anchor = np.array(data.get("anchor", self.anchor.tolist()), dtype=np.float64)
            raw_immutable = [np.array(a, dtype=np.float64) for a in data.get("immutable_axioms", [])]

            # Axiom 3: if on-disk immutable basis is corrupt/empty, regenerate
            if raw_immutable and len(raw_immutable) >= 3:
                A = np.column_stack(raw_immutable)
                Q, _ = np.linalg.qr(A)
                self._immutable_basis = [Q[:, i] for i in range(Q.shape[1])]
            else:
                logger.warning("EITE: saved immutable basis corrupt — regenerating")
                self._immutable_basis = self._make_constitutional_basis()

            self._step = data.get("step", 0)
            self.lr = min(data.get("lr", 0.01), 0.01)   # Axiom 2 enforcement
            self.threshold = data.get("threshold", 0.65)
            ss = data.get("stable_state")
            if ss:
                self._stable_state = StableState(
                    anchor=ss["anchor"],
                    immutable_axioms=ss.get("immutable_axioms", []),
                    alignment_avg=ss["alignment_avg"],
                    step=ss["step"],
                )
            logger.info("EITE: state loaded from %s", path)
            return True
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
            logger.info("EITE: no prior state at %s (%s)", path, exc)
            return False

    # ------------------------------------------------------------------
    # Math utilities
    # ------------------------------------------------------------------

    def _random_unit_vector(self) -> np.ndarray:
        v = np.random.randn(self.dim).astype(np.float64)
        n = np.linalg.norm(v)
        return v / n if n > 1e-12 else v

    @staticmethod
    def _normalize(v: np.ndarray) -> np.ndarray:
        n = np.linalg.norm(v)
        return v / n if n > 1e-12 else v

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na < 1e-12 or nb < 1e-12:
            return 0.0
        return float(np.dot(a, b) / (na * nb))


# =============================================================================
# Factory
# =============================================================================

def build_eite_kernel(workspace: str, dim: int = 384) -> EITEKernel:
    """Build an EITE kernel with the 5 constitutional axioms and persistence.

    Args:
        workspace: Root workspace path (e.g. /home/user/EITE-agent).
                   State is persisted to <workspace>/.tical/eite_state.json.
        dim: Anchor dimension. 64 for fast startup, 384 for compatibility
             with sentence-transformers embeddings.
    """
    persist = str(Path(workspace) / ".tical" / "eite_state.json")
    return EITEKernel(dim=dim, persist_path=persist)
