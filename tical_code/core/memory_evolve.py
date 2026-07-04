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

"""
Memory Evolve System -- EITE Evaluation State Recording
=========================================================

Records evaluation session state transitions, tracks memory integrity
changes across evaluation runs. Implements a protection-level system
to prevent accidental modification of evaluation configuration while
allowing state to evolve during a session.

Architecture:
    MemoryEvolver manages a set of Markdown memory files mapped by
    MEMORY_FILES. Each file has a protection level:

        frozen  -- Cannot be modified by evaluation (SOUL, SECRET).
                   The _write_file method enforces this at the bottom layer.
        guarded -- Evaluation can propose edits via propose_edit(), but
                   they require confirmation (USER).
        open    -- Evaluation can modify autonomously (MEMORY, TOOLS).

    Key safety mechanisms:
    - Automatic backup before every modification
    - Rate limiting: evolve() once per hour, consolidate() once per day
    - Size guard: single edit cannot exceed MAX_EDIT_RATIO (10%) of file size
    - Audit trail: every edit carries a reason string
    - Self-integrity verification against file replacement attacks
    - Confidence tracking for evaluation state quality
    - Memory decay: unvisited evaluation records are aged out

Features:
- Protection levels: frozen / guarded / open
- Automatic backup before each modification
- Rate limiting (evolve: 1/hour, consolidate: 1/day)
- Size guard: single edit <= 10% of file size
- Audit trail: every edit carries a reason
- Confidence-based decay tracking
- Runtime self-integrity verification

Author: Tical (Zize Tu)
Version: see tical_code.__version__
"""

# DESIGNED-NOT-DEAD: EITE evaluation state recorder. DO NOT DELETE -- core for tracking evaluation session state evolution.

import copy
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Memory file map: maps logical keys to file paths relative to memory_dir.
MEMORY_FILES = {
    'SOUL': 'Base config/SOUL.md',
    'TOOLS': 'Base config/TOOLS.md',
    'MEMORY': 'MEMORY.md',
    'USER': 'USER.md',
    'SECRET': 'SECRET.md',
}

# Memory protection hierarchy for evaluation state files.
# frozen   -- Cannot be modified by evaluation system under any circumstances.
# guarded  -- Evaluation can propose modifications, require confirmation.
# open     -- Evaluation can modify autonomously.
PROTECTION_LEVELS = {
    'SOUL': 'frozen',
    'USER': 'guarded',
    'MEMORY': 'open',
    'TOOLS': 'open',
    'SECRET': 'frozen',
}

# Rate limits -- prevent evaluation state from changing too aggressively.
EVOLVE_INTERVAL = 3600
CONSOLIDATE_INTERVAL = 86400

# Modification limit -- single edit cannot exceed this fraction of file size.
MAX_EDIT_RATIO = 0.10

# Memory decay parameters for evaluation records.
DECAY_THRESHOLD_DAYS = 30
DECAY_REMOVE_DAYS = 90

# Confidence scoring constants for evaluation state quality tracking.
DEFAULT_CONFIDENCE = 0.5
KEYWORD_CONFIDENCE = 0.3
LLM_CONFIDENCE = 0.7
CONFIRMED_CONFIDENCE = 1.0
DECAY_CONFIDENCE_FACTOR = 3.0
HIGH_CONFIDENCE_IMMUNE = 1.0

# External validation guard.
VALIDATION_THRESHOLD = 10


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class EditProposal:
    """A proposed edit to an evaluation memory file.

    Attributes:
        file_key: Which memory file to edit (SOUL/USER/MEMORY/TOOLS/SECRET)
        old_text: Text to replace
        new_text: Replacement text
        reason: Why this edit is needed (audit)
        timestamp: When the proposal was created
        status: pending / approved / rejected / applied
    """
    file_key: str
    old_text: str
    new_text: str
    reason: str
    timestamp: float = field(default_factory=time.time)
    status: str = 'pending'
    confidence: float = 0.5


@dataclass
class EditRecord:
    """Record of an applied edit for audit trail.

    Attributes:
        file_key: Which file was edited
        old_text: Original text
        new_text: New text
        reason: Why the edit was made
        timestamp: When the edit was applied
        backup_path: Path to the backup file
        editor: Who made the edit ('ai' / 'human')
    """
    file_key: str
    old_text: str
    new_text: str
    reason: str
    timestamp: float = field(default_factory=time.time)
    backup_path: str = ''
    editor: str = 'ai'
    confidence: float = 0.5


@dataclass
class MemoryStats:
    """Statistics about an evaluation memory file.

    Attributes:
        file_key: Which memory file
        size_bytes: File size in bytes
        line_count: Number of lines
        last_modified: Last modification timestamp
        protection_level: frozen/guarded/open
        entry_count: Number of distinct entries (sections)
    """
    file_key: str
    size_bytes: int = 0
    line_count: int = 0
    last_modified: float = 0.0
    protection_level: str = 'open'
    entry_count: int = 0


# =============================================================================
# MemoryEvolver
# =============================================================================

class MemoryEvolver:
    """Evaluation State Recorder -- records evaluation session state evolution.

    Tracks changes to evaluation memory files with safety protections:
    - Frozen files (SOUL/SECRET) cannot be modified
    - Guarded files (USER) require confirmation
    - Open files (MEMORY/TOOLS) can be modified autonomously
    - Auto-backup on each modification
    - Rate limit to prevent misuse

    Usage:
        evolver = MemoryEvolver(memory_dir="/path/to/memory", base_dir="/path/to/project")
        evolver.apply_edit('MEMORY', 'old content', 'new content', 'learned evaluation state')
        proposals = evolver.propose_edit('USER', 'old content', 'new content', 'preference update')
    """

    def __init__(
        self,
        memory_dir: str,
        base_dir: Optional[str] = None,
    ):
        """
        Args:
            memory_dir: Memory file root directory
            base_dir: Project root directory (used for backup path)
        """
        self.memory_dir = os.path.expanduser(memory_dir)
        self.base_dir = os.path.expanduser(base_dir) if base_dir else os.path.dirname(self.memory_dir)

        self.backup_dir = os.path.join(self.base_dir, 'backups', 'memory')
        self._edit_history: List[EditRecord] = []
        self._pending_proposals: List[EditProposal] = []
        self._last_evolve_time: float = 0.0
        self._last_consolidate_time: float = 0.0
        self._unvalidated_count: int = 0
        self._memory_confidence: Dict[str, float] = {}
        self._cache: Dict[str, str] = {}
        self._self_hash = self._compute_self_hash()

        os.makedirs(self.backup_dir, exist_ok=True)

    # =========================================================================
    # Evolve -- record evaluation state from session experience
    # =========================================================================

    def evolve(self, experience: str, llm_interface=None) -> Dict[str, Any]:
        """Extract evaluation state changes from session experience and record them.

        If llm_interface is provided, uses LLM to analyze experience;
        otherwise uses simple rules.

        Args:
            experience: Evaluation session experience description
            llm_interface: LLM interface (optional)

        Returns:
            {"stored": bool, "file_key": str, "content": str, "reason": str}
        """
        now = time.time()
        if now - self._last_evolve_time < EVOLVE_INTERVAL:
            remaining = int(EVOLVE_INTERVAL - (now - self._last_evolve_time))
            logger.warning(
                f"[EITE MemoryEvolve] evolve rate limited: need to wait {remaining} seconds"
            )
            return {
                'stored': False,
                'file_key': '',
                'content': '',
                'reason': f'Rate limited: need to wait {remaining} seconds',
            }

        result = self._extract_memory(experience)

        if not result['content']:
            return {
                'stored': False,
                'file_key': '',
                'content': '',
                'reason': 'No worth-recording evaluation state extracted',
            }

        if llm_interface is not None:
            confidence = LLM_CONFIDENCE
        elif any(kw in experience.lower() for kw in ['user', 'master', 'preference']):
            confidence = KEYWORD_CONFIDENCE
        else:
            confidence = DEFAULT_CONFIDENCE

        file_key = result['file_key']
        content = result['content']
        reason = result['reason']

        protection = PROTECTION_LEVELS.get(file_key, 'open')
        if protection == 'frozen':
            logger.warning(f"[EITE MemoryEvolve] File {file_key} is frozen, cannot modify")
            return {
                'stored': False,
                'file_key': file_key,
                'content': content,
                'reason': f'File {file_key} is frozen protected, cannot modify',
            }
        elif protection == 'guarded':
            proposal = self.propose_edit(
                file_key,
                '',
                f"\n{content}",
                reason,
            )
            self._last_evolve_time = now
            return {
                'stored': False,
                'file_key': file_key,
                'content': content,
                'reason': f'File {file_key} is guarded protected, proposal submitted',
                'proposal': proposal,
            }
        else:
            success = self._append_to_file(file_key, content, reason)
            if confidence < CONFIRMED_CONFIDENCE:
                self._unvalidated_count += 1
                self._memory_confidence[content[:80]] = confidence
                if self._unvalidated_count >= VALIDATION_THRESHOLD:
                    logger.warning(
                        "[EITE MemoryEvolve] %d unvalidated states exceed threshold %d -- "
                        "external validation recommended",
                        self._unvalidated_count, VALIDATION_THRESHOLD)
            self._last_evolve_time = now
            return {
                'stored': success,
                'file_key': file_key,
                'content': content,
                'reason': reason if success else 'Write failed',
            }

    # =========================================================================
    # Decay -- age out stale evaluation records
    # =========================================================================

    def decay(self) -> Dict[str, Any]:
        """Apply decay to evaluation state records -- reduce weight of unused records.

        Returns:
            {"decayed": int, "removed": int, "details": list}
        """
        decayed = 0
        removed = 0
        details = []
        now = time.time()

        for file_key in ['MEMORY', 'TOOLS']:
            file_path = self._get_file_path(file_key)
            if not os.path.exists(file_path):
                continue

            content = self._read_file(file_key)
            if not content:
                continue

            sections = self._split_sections(content)
            kept_sections = []

            for section in sections:
                section_time = self._extract_timestamp(section)
                if section_time is None:
                    kept_sections.append(section)
                    continue

                age_days = (now - section_time) / 86400

                if age_days > DECAY_REMOVE_DAYS:
                    removed += 1
                    details.append({
                        'file_key': file_key,
                        'action': 'removed',
                        'age_days': round(age_days, 1),
                        'section_preview': section[:80],
                    })
                elif age_days > DECAY_THRESHOLD_DAYS:
                    decayed += 1
                    if '[decay' not in section:
                        decay_marker = f" [decay{round(age_days)}days]"
                        section = re.sub(
                            r'(^#{1,3}\s+.+)',
                            rf'\1{decay_marker}',
                            section,
                            count=1,
                        )
                    kept_sections.append(section)
                    details.append({
                        'file_key': file_key,
                        'action': 'decayed',
                        'age_days': round(age_days, 1),
                    })
                else:
                    kept_sections.append(section)

            if len(kept_sections) != len(sections):
                new_content = '\n'.join(kept_sections)
                if new_content != content:
                    self._backup_file(file_key)
                    self._write_file(file_key, new_content)

        return {
            'decayed': decayed,
            'removed': removed,
            'details': details,
        }

    # =========================================================================
    # Consolidate -- compress fragmented evaluation state records
    # =========================================================================

    def consolidate(self) -> Dict[str, Any]:
        """Consolidate fragmented evaluation records -- compress and merge.

        Returns:
            {"consolidated": int, "space_saved": int, "details": list}
        """
        now = time.time()
        if now - self._last_consolidate_time < CONSOLIDATE_INTERVAL:
            remaining = int(CONSOLIDATE_INTERVAL - (now - self._last_consolidate_time))
            logger.warning(
                f"[EITE MemoryEvolve] consolidate rate limited: need to wait {remaining} seconds"
            )
            return {
                'consolidated': 0,
                'space_saved': 0,
                'reason': f'Rate limited: need to wait {remaining} seconds',
            }

        consolidated = 0
        space_saved = 0
        details = []

        for file_key in ['MEMORY', 'TOOLS']:
            file_path = self._get_file_path(file_key)
            if not os.path.exists(file_path):
                continue

            content = self._read_file(file_key)
            if not content:
                continue

            original_size = len(content)

            new_content = re.sub(r'\n{4,}', '\n\n\n', content)

            sections = self._split_sections(new_content)
            seen_titles = set()
            unique_sections = []
            for section in sections:
                title = self._extract_section_title(section)
                if title and title in seen_titles:
                    for i, existing in enumerate(unique_sections):
                        existing_title = self._extract_section_title(existing)
                        if existing_title == title:
                            if len(section) > len(existing):
                                unique_sections[i] = section
                            consolidated += 1
                            break
                else:
                    if title:
                        seen_titles.add(title)
                    unique_sections.append(section)

            new_content = '\n'.join(unique_sections)
            new_content = '\n'.join(line.rstrip() for line in new_content.split('\n'))

            new_size = len(new_content)
            saved = original_size - new_size

            if saved > 0:
                self._backup_file(file_key)
                self._write_file(file_key, new_content)
                space_saved += saved
                details.append({
                    'file_key': file_key,
                    'original_size': original_size,
                    'new_size': new_size,
                    'space_saved': saved,
                })

        self._last_consolidate_time = now

        return {
            'consolidated': consolidated,
            'space_saved': space_saved,
            'details': details,
        }

    # =========================================================================
    # Edit Operations
    # =========================================================================

    def propose_edit(
        self,
        file_key: str,
        old_text: str,
        new_text: str,
        reason: str,
    ) -> EditProposal:
        """Propose an edit to a guarded/frozen evaluation memory file.

        Args:
            file_key: Memory file key name (SOUL/USER/MEMORY/TOOLS/SECRET)
            old_text: Text to replace
            new_text: Replacement text
            reason: Reason for modification (for audit)

        Returns:
            EditProposal object
        """
        if not reason.strip():
            raise ValueError("Modification reason cannot be empty (audit required)")

        if file_key not in MEMORY_FILES:
            raise ValueError(f"Unknown memory file: {file_key}")

        protection = PROTECTION_LEVELS.get(file_key, 'open')

        if protection == 'frozen':
            logger.warning(f"[EITE MemoryEvolve] File {file_key} is frozen, proposal rejected")
            proposal = EditProposal(
                file_key=file_key,
                old_text=old_text,
                new_text=new_text,
                reason=f"[FROZEN-reject] {reason}",
                status='rejected',
            )
        else:
            proposal = EditProposal(
                file_key=file_key,
                old_text=old_text,
                new_text=new_text,
                reason=reason,
                status='pending',
            )

        self._pending_proposals.append(proposal)
        logger.info(f"[EITE MemoryEvolve] New proposal: {file_key} -- {reason[:50]}")
        return proposal

    def approve_proposal(self, proposal_index: int) -> Dict[str, Any]:
        """Approve and apply a pending edit proposal.

        Args:
            proposal_index: Index of proposal in list

        Returns:
            {"applied": bool, "reason": str}
        """
        if proposal_index < 0 or proposal_index >= len(self._pending_proposals):
            return {'applied': False, 'reason': 'Invalid proposal index'}

        proposal = self._pending_proposals[proposal_index]

        if proposal.status != 'pending':
            return {'applied': False, 'reason': f'Proposal status is {proposal.status}'}

        if proposal.reason.startswith('[FROZEN-reject]'):
            return {'applied': False, 'reason': 'Frozen file cannot be modified'}

        result = self.apply_edit(
            proposal.file_key,
            proposal.old_text,
            proposal.new_text,
            proposal.reason,
            editor='human',
        )

        if result:
            proposal.status = 'approved'
            return {'applied': True, 'reason': 'Proposal executed'}
        else:
            return {'applied': False, 'reason': 'Modification execution failed'}

    def reject_proposal(self, proposal_index: int) -> bool:
        """Reject a pending edit proposal.

        Args:
            proposal_index: Index of proposal in list

        Returns:
            True if rejected successfully
        """
        if proposal_index < 0 or proposal_index >= len(self._pending_proposals):
            return False

        self._pending_proposals[proposal_index].status = 'rejected'
        return True

    def apply_edit(
        self,
        file_key: str,
        old_text: str,
        new_text: str,
        reason: str,
        editor: str = 'ai',
    ) -> bool:
        """Apply an edit to an open evaluation memory file (with backup).

        Args:
            file_key: Memory file key name
            old_text: Text to replace (empty string indicates append)
            new_text: Replacement text
            reason: Reason for modification
            editor: Editor type ('ai' / 'human')

        Returns:
            True if edit applied successfully

        Raises:
            ValueError: If file is frozen or parameters are invalid
        """
        if not reason.strip():
            raise ValueError("Modification reason cannot be empty (audit required)")

        if file_key not in MEMORY_FILES:
            raise ValueError(f"Unknown memory file: {file_key}")

        protection = PROTECTION_LEVELS.get(file_key, 'open')

        if protection == 'frozen' and editor == 'ai':
            raise PermissionError(f"Evaluation system cannot modify frozen file: {file_key}")

        if protection == 'guarded' and editor == 'ai':
            raise PermissionError(f"Guarded file requires propose_edit: {file_key}")

        file_path = self._get_file_path(file_key)

        if not os.path.exists(file_path):
            self._write_file(file_key, '')

        content = self._read_file(file_key)

        if old_text and content:
            edit_size = abs(len(new_text) - len(old_text))
            max_edit = max(int(len(content) * MAX_EDIT_RATIO), 100)
            if edit_size > max_edit:
                logger.error(
                    f"[EITE MemoryEvolve] Modification size {edit_size} exceeds limit {max_edit} "
                    f"({MAX_EDIT_RATIO*100:.0f}% of file size)"
                )
                return False

        backup_path = self._backup_file(file_key)

        if old_text == '':
            new_content = content + new_text
        else:
            if old_text not in content:
                logger.error(f"[EITE MemoryEvolve] Text not found: {old_text[:50]}...")
                return False
            new_content = content.replace(old_text, new_text, 1)

        success = self._write_file(file_key, new_content)

        if success:
            record = EditRecord(
                file_key=file_key,
                old_text=old_text[:200],
                new_text=new_text[:200],
                reason=reason,
                backup_path=backup_path or '',
                editor=editor,
            )
            self._edit_history.append(record)
            logger.info(f"[EITE MemoryEvolve] Modified {file_key}: {reason[:50]}")

        return success

    # =========================================================================
    # Confidence & Validation
    # =========================================================================

    def needs_validation(self) -> bool:
        """Check if unvalidated evaluation state records exceed threshold."""
        return self._unvalidated_count >= VALIDATION_THRESHOLD

    def validate_memory(self, file_key: str, section_title: str, approved: bool = True) -> bool:
        """Mark an evaluation state record as externally validated.

        Args:
            file_key: Memory file key name
            section_title: Title or first 80 chars of the record
            approved: True if confirmed, False if rejected

        Returns:
            True if a matching record was found and updated
        """
        if approved:
            if section_title in self._memory_confidence:
                self._memory_confidence[section_title] = CONFIRMED_CONFIDENCE
                self._unvalidated_count = max(0, self._unvalidated_count - 1)
                logger.info("[EITE MemoryEvolve] Validated state in %s: %s", file_key, section_title[:50])
                return True
        else:
            if section_title in self._memory_confidence:
                del self._memory_confidence[section_title]
                self._unvalidated_count = max(0, self._unvalidated_count - 1)
                logger.info("[EITE MemoryEvolve] Rejected state in %s: %s", file_key, section_title[:50])
                return True
        return False

    def get_validation_stats(self) -> Dict[str, int]:
        """Return validation statistics for the evaluation state system."""
        return {
            'unvalidated_count': self._unvalidated_count,
            'tracked_confidence_entries': len(self._memory_confidence),
            'threshold': VALIDATION_THRESHOLD,
            'needs_validation': self.needs_validation(),
        }

    # =========================================================================
    # Memory Statistics
    # =========================================================================

    def get_memory_stats(self) -> Dict[str, Any]:
        """Get evaluation memory file statistics.

        Returns:
            Dict mapping file_key -> MemoryStats dict
        """
        stats = {}
        for file_key, rel_path in MEMORY_FILES.items():
            file_path = self._get_file_path(file_key)
            stat = MemoryStats(
                file_key=file_key,
                protection_level=PROTECTION_LEVELS.get(file_key, 'open'),
            )

            if os.path.exists(file_path):
                try:
                    file_stat = os.stat(file_path)
                    stat.size_bytes = file_stat.st_size
                    stat.last_modified = file_stat.st_mtime

                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    stat.line_count = content.count('\n') + 1
                    stat.entry_count = len(re.findall(r'^#{1,3}\s+', content, re.MULTILINE))
                except (OSError, UnicodeDecodeError):
                    pass

            stats[file_key] = {
                'file_key': stat.file_key,
                'size_bytes': stat.size_bytes,
                'line_count': stat.line_count,
                'last_modified': stat.last_modified,
                'protection_level': stat.protection_level,
                'entry_count': stat.entry_count,
            }

        return stats

    def get_pending_proposals(self) -> List[Dict[str, Any]]:
        """Get all pending edit proposals."""
        return [
            {
                'index': i,
                'file_key': p.file_key,
                'old_text': p.old_text[:100],
                'new_text': p.new_text[:100],
                'reason': p.reason,
                'status': p.status,
                'timestamp': p.timestamp,
            }
            for i, p in enumerate(self._pending_proposals)
            if p.status == 'pending'
        ]

    def get_edit_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent edit history.

        Args:
            limit: Maximum number of records to return

        Returns:
            List of edit record dicts
        """
        return [
            {
                'file_key': r.file_key,
                'old_text': r.old_text[:100],
                'new_text': r.new_text[:100],
                'reason': r.reason,
                'timestamp': r.timestamp,
                'backup_path': r.backup_path,
                'editor': r.editor,
            }
            for r in self._edit_history[-limit:]
        ]

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _get_file_path(self, file_key: str) -> str:
        rel_path = MEMORY_FILES[file_key]
        return os.path.join(self.memory_dir, rel_path)

    def _read_file(self, file_key: str) -> str:
        if file_key in self._cache:
            return self._cache[file_key]

        file_path = self._get_file_path(file_key)
        if not os.path.exists(file_path):
            return ''

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self._cache[file_key] = content
            return content
        except (OSError, UnicodeDecodeError) as e:
            logger.error(f"[EITE MemoryEvolve] Read file failed {file_key}: {e}")
            return ''

    def _write_file(self, file_key: str, content: str) -> bool:
        """Write content to an evaluation memory file.

        Security: bottom-layer write protection. Even if code bypasses
        upper-layer protection checks, _write_file rejects writing frozen
        files. This is the last line of defense.
        """
        protection = PROTECTION_LEVELS.get(file_key, 'open')
        if protection == 'frozen':
            logger.error(
                f"[EITE MemoryEvolve] BLOCKED: attempt to write frozen file {file_key}. "
                f"This may be a security violation."
            )
            return False

        file_path = self._get_file_path(file_key)
        if file_path.endswith('.py'):
            logger.error(
                f"[EITE MemoryEvolve] BLOCKED: attempt to write Python file {file_path}. "
                f"Memory system can only manage .md files."
            )
            return False

        if not self._verify_integrity():
            logger.error(
                "[EITE MemoryEvolve] BLOCKED: self integrity check failed! "
                "memory_evolve.py may have been tampered, all write operations locked."
            )
            return False

        file_path = self._get_file_path(file_key)
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            self._cache[file_key] = content
            return True
        except OSError as e:
            logger.error(f"[EITE MemoryEvolve] Write file failed {file_key}: {e}")
            return False

    def _append_to_file(self, file_key: str, content: str, reason: str) -> bool:
        existing = self._read_file(file_key)
        separator = '\n' if existing and not existing.endswith('\n') else ''
        new_content = existing + separator + content
        return self._write_file(file_key, new_content)

    def _backup_file(self, file_key: str) -> Optional[str]:
        file_path = self._get_file_path(file_key)
        if not os.path.exists(file_path):
            return None

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f"{file_key}_{timestamp}.md"
        backup_path = os.path.join(self.backup_dir, backup_name)

        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            shutil.copy2(file_path, backup_path)
            logger.info(f"[EITE MemoryEvolve] Backed up {file_key} -> {backup_path}")
            return backup_path
        except OSError as e:
            logger.error(f"[EITE MemoryEvolve] Backup failed {file_key}: {e}")
            return None

    def _extract_memory(self, experience: str) -> Dict[str, str]:
        """Extract evaluation state content from an experience string.

        Args:
            experience: Experience description text

        Returns:
            {"file_key": str, "content": str, "reason": str}
        """
        if not experience.strip():
            return {'file_key': 'MEMORY', 'content': '', 'reason': ''}

        experience_lower = experience.lower()

        user_keywords = ['userlike', 'userpreference', 'userdislike', 'user says', 'masterlike',
                         'masterpreference', 'my master', 'userhabit']
        if any(kw in experience_lower or kw in experience for kw in user_keywords):
            return {
                'file_key': 'USER',
                'content': f"- {experience.strip()}",
                'reason': 'Detected user-relevant info',
            }

        tool_keywords = ['Tool', 'plugin', 'Search', 'API', 'plugin', 'tool', 'usage']
        if any(kw in experience for kw in tool_keywords):
            return {
                'file_key': 'TOOLS',
                'content': f"### experience\n- {experience.strip()}",
                'reason': 'Detected tool usage experience',
            }

        return {
            'file_key': 'MEMORY',
            'content': f"### experience record\n{experience.strip()}\n",
            'reason': 'Generic experience record',
        }

    def _split_sections(self, content: str) -> List[str]:
        """Split content into sections by markdown headers."""
        if not content.strip():
            return []

        sections = re.split(r'(?=^###\s)', content, flags=re.MULTILINE)
        result = []
        for section in sections:
            section = section.strip()
            if section:
                result.append(section)

        if not result:
            result = [content.strip()]

        return result

    def _extract_section_title(self, section: str) -> Optional[str]:
        match = re.match(r'^#{1,3}\s+(.+)', section)
        if match:
            return match.group(1).strip()
        return None

    def _extract_timestamp(self, section: str) -> Optional[float]:
        iso_match = re.search(r'(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})', section)
        if iso_match:
            try:
                from datetime import datetime as dt
                dt_obj = dt.strptime(
                    f"{iso_match.group(1)} {iso_match.group(2)}",
                    '%Y-%m-%d %H:%M:%S',
                )
                return dt_obj.timestamp()
            except ValueError:
                logger.debug("[EITE MemoryEvolve] ISO datetime parse failed")

        date_match = re.search(r'\[(\d{4}-\d{2}-\d{2})\]', section)
        if date_match:
            try:
                from datetime import datetime as dt
                dt_obj = dt.strptime(date_match.group(1), '%Y-%m-%d')
                return dt_obj.timestamp()
            except ValueError:
                logger.debug("[EITE MemoryEvolve] date format parse failed")

        return None

    def clear_cache(self) -> None:
        """Clear the internal file content cache."""
        self._cache.clear()

    # =========================================================================
    # Integrity Verification
    # =========================================================================

    def _compute_self_hash(self) -> str:
        """Compute SHA-256 hash of memory_evolve.py source code."""
        import hashlib
        try:
            self_path = os.path.realpath(__file__)
            with open(self_path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            logger.debug(f"[EITE MemoryEvolve] clear_cache exception (non-blocking): {e}")
            return 'INTEGRITY_CHECK_UNAVAILABLE'

    def _verify_integrity(self) -> bool:
        """Verify that memory_evolve.py has not been tampered with."""
        current_hash = self._compute_self_hash()
        if current_hash == 'INTEGRITY_CHECK_UNAVAILABLE':
            logger.warning("[EITE MemoryEvolve] Cannot compute self hash, skipping integrity check")
            return True
        return current_hash == self._self_hash
