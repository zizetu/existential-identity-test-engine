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
Skill Curator - background lifecycle management for evaluation skills.

Part of the EITE evaluation framework. Tracks usage of extracted skills,
marks idle skills stale, archives long-stale skills, supports pin
protection, and creates tar.gz backups before any mutation.

Runs as a lightweight background check alongside the main evaluation
loop. Designed to be called periodically -- every curation tick it
inspects the skill index and applies lifecycle rules.

EITE Version: 1.0.0
"""

import json
import logging
import os
import shutil
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("eite.curator")

SKILLS_DIR = Path.home() / ".eite" / "skills"
ARCHIVE_DIR = SKILLS_DIR / "archive"
BACKUP_DIR = Path.home() / ".eite" / "skill-backups"  # outside skills/ to prevent recursion

# Lifecycle thresholds (configurable via constructor)
STALE_AFTER_DAYS = 7       # unused for 7 days -> stale
ARCHIVE_AFTER_DAYS = 30    # stale for 30 days -> archive
PIN_EXEMPT = True          # pinned skills never auto-archive


class SkillCurator:
    """Background lifecycle manager for auto-extracted evaluation skills.

    Every ``curate()`` call (intended to run periodically, e.g. every
    60 minutes) applies the following rules to every skill in the index:

    1. **Pin exemption**: pinned skills skip all auto-transitions.
    2. **Stale marking**: skills unused for ``stale_after_days`` are
       marked ``state: "stale"`` (informational only -- file stays).
    3. **Archival**: skills in ``"stale"`` state for >=
       ``archive_after_days`` are moved to ``archive/`` and removed
       from the active index.
    4. **Backup**: a tar.gz snapshot is created before the first
       mutation in any curation run.

    Manual actions (pin / unpin / archive / restore) are available
    via public methods and do NOT trigger auto-transitions for that
    specific skill in the same curation tick.
    """

    def __init__(
        self,
        skills_dir: Optional[Path] = None,
        stale_after_days: int = STALE_AFTER_DAYS,
        archive_after_days: int = ARCHIVE_AFTER_DAYS,
    ):
        self._skills_dir = skills_dir or SKILLS_DIR
        self._stale_after = stale_after_days
        self._archive_after = archive_after_days
        self._last_curation = 0.0
        self._curation_cooldown = 600  # seconds between curations
        self._index_path = self._skills_dir / "index.json"
        self._archive_dir = self._skills_dir / "archive"
        self._backup_dir = self._skills_dir / "backups"
        for d in (self._skills_dir, self._archive_dir, self._backup_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- Public API ---------------------------------------------------------

    def curate(self) -> Dict[str, Any]:
        """Run one curation cycle. Returns a summary of actions taken.

        Designed to be called from the evaluation worker main loop
        every N ticks. Does nothing if called more than once per hour.
        """
        now = time.time()
        if now - self._last_curation < self._curation_cooldown:
            return {"skipped": True, "reason": "cooldown"}
        self._last_curation = now

        index = self._load_index()
        if not index:
            return {"skipped": True, "reason": "empty index"}

        actions = {"stale": [], "archived": [], "errors": []}
        did_backup = False

        for name, meta in list(index.items()):
            try:
                if meta.get("pinned"):
                    continue

                last_used = meta.get("last_used_at", now)
                state = meta.get("state", "active")
                days_since_use = (now - last_used) / 86400.0

                # State transition: active -> stale
                if state == "active" and last_used > 0 and days_since_use >= self._stale_after:
                    if not did_backup:
                        self._backup()
                        did_backup = True
                    meta["state"] = "stale"
                    meta["stale_since"] = now
                    actions["stale"].append(name)
                    logger.info(
                        "Curator: marked '%s' stale (unused %.0f days)",
                        name, days_since_use,
                    )

                # State transition: stale -> archived
                elif state == "stale":
                    stale_since = meta.get("stale_since", last_used)
                    days_since_stale = (now - stale_since) / 86400.0
                    if days_since_stale >= self._archive_after:
                        if not did_backup:
                            self._backup()
                            did_backup = True
                        self._archive_skill(name, meta)
                        del index[name]
                        actions["archived"].append(name)
                        logger.info(
                            "Curator: archived '%s' (stale %.0f days)",
                            name, days_since_stale,
                        )

            except Exception as e:
                actions["errors"].append({"skill": name, "error": str(e)})
                logger.warning("Curator: error processing '%s': %s", name, e)

        self._save_index(index)
        return {
            "curated_at": datetime.now(timezone.utc).isoformat(),
            "total_skills": len(index),
            "stale_count": len(actions["stale"]),
            "archived_count": len(actions["archived"]),
            "error_count": len(actions["errors"]),
        }

    def pin(self, name: str) -> bool:
        """Protect a skill from auto-archival. Returns True on success."""
        index = self._load_index()
        if name not in index:
            return False
        index[name]["pinned"] = True
        index[name]["pinned_at"] = time.time()
        self._save_index(index)
        logger.info("Curator: pinned '%s'", name)
        return True

    def unpin(self, name: str) -> bool:
        """Remove pin protection. Returns True on success."""
        index = self._load_index()
        if name not in index:
            return False
        index[name]["pinned"] = False
        self._save_index(index)
        logger.info("Curator: unpinned '%s'", name)
        return True

    def archive(self, name: str) -> bool:
        """Immediately archive a skill (manual action). Returns True on success."""
        index = self._load_index()
        if name not in index:
            return False
        self._backup()
        self._archive_skill(name, index.pop(name))
        self._save_index(index)
        logger.info("Curator: manually archived '%s'", name)
        return True

    def restore(self, name: str) -> bool:
        """Restore an archived skill back to the active index. Returns True on success."""
        archived_path = self._archive_dir / f"{name}.md"
        if not archived_path.exists():
            return False

        index = self._load_index()

        # Move file back
        skill_path = self._skills_dir / f"{name}.md"
        shutil.move(str(archived_path), str(skill_path))

        # Reconstruct metadata
        try:
            meta_text = skill_path.read_text().split("---")[1]
            meta = json.loads(meta_text.strip())
        except Exception:
            meta = {"name": name, "description": ""}

        meta["state"] = "active"
        meta["last_used_at"] = time.time()
        meta["use_count"] = meta.get("use_count", 0)
        meta.pop("stale_since", None)
        meta["pinned"] = False
        meta["restored_at"] = time.time()

        index[name] = meta
        self._save_index(index)
        logger.info("Curator: restored '%s' from archive", name)
        return True

    def get_stats(self) -> Dict[str, Any]:
        """Return a summary of the current skill library state."""
        index = self._load_index()
        active = sum(1 for m in index.values() if m.get("state", "active") == "active")
        stale = sum(1 for m in index.values() if m.get("state") == "stale")
        pinned = sum(1 for m in index.values() if m.get("pinned"))
        failure = sum(1 for m in index.values() if m.get("_is_failure_pattern"))
        archived = len(list(self._archive_dir.glob("*.md")))

        return {
            "total": len(index),
            "active": active,
            "stale": stale,
            "pinned": pinned,
            "failure_patterns": failure,
            "archived": archived,
            "last_curation": self._last_curation,
        }

    # -- Internal helpers ---------------------------------------------------

    def _load_index(self) -> Dict[str, Dict[str, Any]]:
        if not self._index_path.exists():
            return {}
        try:
            return json.loads(self._index_path.read_text())
        except Exception:
            return {}

    def _save_index(self, index: Dict[str, Dict[str, Any]]):
        try:
            self._index_path.write_text(json.dumps(index, indent=2))
        except Exception as e:
            logger.error("Curator: failed to save index: %s", e)

    def _archive_skill(self, name: str, meta: Dict[str, Any]):
        """Move a skill file to the archive directory."""
        src = self._skills_dir / f"{name}.md"
        dst = self._archive_dir / f"{name}.md"
        if src.exists():
            shutil.move(str(src), str(dst))
            # Tag metadata for posterity
            meta["archived_at"] = time.time()
            meta["archived_state"] = meta.get("state", "active")
            archive_meta = self._archive_dir / f"{name}.meta.json"
            archive_meta.write_text(json.dumps(meta, indent=2))

        # Prune old archives: keep only the 100 most recent
        try:
            archived = sorted(
                self._archive_dir.glob("*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in archived[100:]:
                old.unlink(missing_ok=True)
                meta_file = self._archive_dir / f"{old.stem}.meta.json"
                meta_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _backup(self) -> Optional[Path]:
        """Create a tar.gz backup of the entire skills directory before mutation.

        Returns the backup path, or None on failure.
        """
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = self._backup_dir / f"skills-backup-{timestamp}.tar.gz"
        try:
            with tarfile.open(backup_path, "w:gz") as tar:
                tar.add(str(self._skills_dir), arcname="skills")
            # Prune old backups: keep only the 10 most recent
            backups = sorted(self._backup_dir.glob("skills-backup-*.tar.gz"))
            for old in backups[:-10]:
                old.unlink(missing_ok=True)
            logger.debug("Curator: backup saved to %s", backup_path)
            return backup_path
        except Exception as e:
            logger.warning("Curator: backup failed: %s", e)
            return None
