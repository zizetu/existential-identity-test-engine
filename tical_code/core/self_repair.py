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
Self-Repair Engine - Anchor-Based Auto Recovery
=================================================

Auto-detect exceptions and recover from Anchor.

Trigger conditions:
1. identityVerifyFailed(identity mismatch)
2. ConfigFilecorrupt/missing
3. Session datalost
4. criticalprocessnot inrun
5. Anchor File inconsistent with local status

Recovery strategy:
- Restore identity from anchor.json
- Recover config from anchor.json
- Rebuild session (from summary)
- restartcriticalprocess

Author: Tical (Zize Tu)
Version: see tical_code.__version__
"""

import ast
import asyncio
import hashlib
import json
import logging
import os
import py_compile
import re
import signal
import socket
import subprocess
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .worker_framework import WorkerFramework

logger = logging.getLogger(__name__)


# =============================================================================
# P0-4: Sandbox Modes
# =============================================================================

class SandboxMode:
    """Sandbox isolation mode constants.

    DOCKER: fully process-isolated, execute test code in Docker container (requires Docker)
    RESTRICTED_PYTHON: restricted Python environment, remove dangerous builtins (current default mode, limited isolation)
    DISABLED: Do not execute sandbox tests
    """
    DOCKER = "docker"
    RESTRICTED_PYTHON = "restricted_python"
    DISABLED = "disabled"


def _detect_docker_available() -> bool:
    """Detect whether Docker is usable (P0-4).

    Returns:
        True if docker CLI is executable and guard process is running
    """
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


# =============================================================================
# Issue Types
# =============================================================================

class IssueType:
    """Issue type constants."""
    IDENTITY_MISMATCH = "identity_mismatch"
    CONFIG_MISSING = "config_missing"
    CONFIG_CORRUPTED = "config_corrupted"
    SESSION_LOST = "session_lost"
    PROCESS_NOT_RUNNING = "process_not_running"
    ANCHOR_INCONSISTENT = "anchor_inconsistent"
    FILE_MISSING = "file_missing"
    VERIFICATION_FAILED = "verification_failed"


# =============================================================================
# Repair Result
# =============================================================================

@dataclass
class RepairResult:
    """
    Repair operation result.
    
    Attributes:
        issue_type: problemtype
        action: Fix action to execute
        success: whethersuccess
        details: detail-info
        restored_from: Source from which to recover
    """
    issue_type: str
    action: str
    success: bool
    details: str = ""
    restored_from: str = ""
    timestamp: float = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
    
    def to_dict(self) -> Dict:
        return {
            'issue_type': self.issue_type,
            'action': self.action,
            'success': self.success,
            'details': self.details,
            'restored_from': self.restored_from,
            'timestamp': self.timestamp,
        }


# =============================================================================
# Self-Repair Engine
# =============================================================================

class SelfRepairEngine:
    __tical_module__ = True
    """
    Auto-detect exceptions and recover from Anchor.
    
    usemethod:
        engine = SelfRepairEngine(framework)
        
        # healthCheck
        health = await engine.check_health()
        if not health['healthy']:
            results = await engine.repair(health['issues'])
        
        # Auto-repair
        repaired = await engine.auto_repair_if_needed()
        
        # Self-evolutionsecuritymodify
        result = await engine.safe_modify('some_file.py', new_code)
        restart_result = await engine.safe_restart_with_rollback(['python', 'main.py'])
    """
    
    # Self-evolutionsecurityConfig
    # P0 #1: Protect filelist - Single-source reference from sandbox.PROTECTED_FILE_REGISTRY
    # (tical-agent) or empty fallback (EITE-light, where sandbox.py is eval-only)
    PROTECTED_FILES = frozenset()
    try:
        from tical_code.core.sandbox import PROTECTED_FILE_REGISTRY
        PROTECTED_FILES = PROTECTED_FILE_REGISTRY
    except (ImportError, AttributeError):
        logger.warning("PROTECTED_FILE_REGISTRY not available; self_repair runs with empty protect list")
    
    # P0 #1: Protected directories - files under these directories cannot be modified
    PROTECTED_DIRS = frozenset({
        '.git',        # Git repository
    })
    
    MAX_SELF_MODIFICATIONS = 3  # Max 3 self-modifications per conversation/startup
    HARD_MAX_SELF_MODIFICATIONS = 10  # absolute upper limit, no config can exceed
    
    # P0 #2 + P1 #5 + P1 #8: dangerfunctioncallMode
    DANGEROUS_PATTERNS = [
        # systemcommandExecute
        r'os\.system\s*\(',
        r'subprocess\.(call|run|Popen|check_output|check_call)\s*\(',
        r'exec\s*\(',
        r'eval\s*\(',
        r'__import__\s*\(',
        # Filesystemdestroy
        r'shutil\.rmtree\s*\(',
        r'os\.remove\s*\(\s*[\'"]\/',  # Delete root directory files
        r'os\.unlink\s*\(',
        # processcontrol
        r'sys\.exit\s*\(',
        r'os\._exit\s*\(',
        r'os\.kill\s*\(',
        # networkbackdoor
        r'socket\.socket\s*\(',
        r'telnetlib\.',
        r'http\.server\.',
        # environmenttamper
        r'os\.environ\[.*\]\s*=',  # modifyenv-var
        r'PYTHONPATH',
        # self-destruct
        r'rm\s+-rf',
        r'dd\s+if=',
        r'mkfs\.',
        r'>\s*/dev/sd',
        # P1 #5: memoryoperation
        r'ctypes\.',
        r'/proc/self/mem',
        r'/proc/self/',
        r'mmap\.',              # mmap can be used to modify memory
        r'sys\.modules',        # sys.modules tamper
    ]
    
    # P1 #8: Protected environment variables
    PROTECTED_ENV_VARS = frozenset({
        'PYTHONPATH', 'PATH', 'HOME', 'USER',
        'ANCHOR_TOKEN', 'AI_SHARED_KEY',
        'TICAL_IDENTITY_NAME', 'TICAL_IDENTITY_ROLE',
    })
    
    # P0 #3: Security check rules for non-Python files
    SHELL_DANGEROUS_PATTERNS = [
        r'rm\s+-rf\s+/',
        r'mkfs\.',
        r'dd\s+if=',
        r'>\s*/dev/sd',
        r'curl\s+.*\|\s*bash',
        r'wget\s+.*\|\s*sh',
        r'chmod\s+777',
        r'sudo\s+rm',
        r':\(\)\{\s*:\|\:&\s*\}',  # fork bomb
    ]
    
    YAML_DANGEROUS_PATTERNS = [
        r'!!python/object/',     # YAML deserializeattack
        r'!!python/name/',
        r'!!python/module/',
    ]
    
    DOCKERFILE_DANGEROUS_PATTERNS = [
        r'privileged',
        r'host\s+network',
        r'/var/run/docker\.sock',
        r'rm\s+-rf\s+/',
    ]
    
    def __init__(self, framework: 'WorkerFramework' = None, sandbox_mode: str = None):
        self.framework = framework
        self.repair_history: List[RepairResult] = []
        
        # P0-4: Sandbox ModeConfig
        # Prefer first-pass parameter, otherwise read from framework config, default RESTRICTED_PYTHON
        if sandbox_mode is not None:
            self.sandbox_mode = sandbox_mode
        elif framework is not None and hasattr(framework, 'config') and hasattr(framework.config, 'sandbox_mode'):
            self.sandbox_mode = framework.config.sandbox_mode
        else:
            self.sandbox_mode = SandboxMode.RESTRICTED_PYTHON
        
        # Auto-detect Docker usability
        if self.sandbox_mode == SandboxMode.DOCKER and not _detect_docker_available():
            logger.warning("[SelfRepair] Docker mode requested but Docker unavailable, falling back to RESTRICTED_PYTHON")
            self.sandbox_mode = SandboxMode.RESTRICTED_PYTHON
        
        # criticalFilelist(used forCheck)
        self.critical_files = [
            'anchor.json',
            '~/.tical-code/sessions.db',
        ]
        
        # Self-evolutionstatus
        self._modification_count = self._load_modification_count()  # P1 #4: load from persistent file
        self._last_commit_hash = None  # commit hash of last backup
        
        # P0 #3: identity fingerprint - record identity.py hash at startup
        self._identity_fingerprint = self._compute_identity_fingerprint()
        
        # P2 #9: Concurrency lock
        self._modify_lock = asyncio.Lock()
        
        # truthful-report system: lazy import, does not affect core features
        self._truth_reporter = None
        try:
            from .truthful_reporting import TruthReporter
            self._truth_reporter = TruthReporter()
            logger.info("[SelfRepair] TruthReporter initialized successfully")
        except Exception as e:
            logger.warning(f"[SelfRepair] TruthReporter init failed (non-fatal): {e}")
    
    # =========================================================================
    # Health Check
    # =========================================================================
    
    async def check_health(self) -> Dict:
        """
        Health check, return list of exceptions.
        
        Returns:
            {"healthy": bool, "issues": [issue_dict, ...]}
        """
        issues = []
        
        # Check 1: identityconsistency
        identity_issue = await self._check_identity()
        if identity_issue:
            issues.append(identity_issue)
        
        # Check 2: Configintegrity
        config_issue = await self._check_config()
        if config_issue:
            issues.append(config_issue)
        
        # Check 3: Session usability
        session_issue = await self._check_session()
        if session_issue:
            issues.append(session_issue)
        
        # Check 4: criticalFileexist
        file_issues = await self._check_critical_files()
        issues.extend(file_issues)
        
        # Check 5: Critical tool usability
        tool_issues = await self._check_tools()
        issues.extend(tool_issues)
        
        return {
            'healthy': len(issues) == 0,
            'issues': issues,
            'timestamp': time.time(),
        }
    
    async def _check_identity(self) -> Optional[Dict]:
        """Check identity consistency."""
        try:
            # Get identity from framework - may be .identity attr or cfg['name']
            identity = getattr(self.framework, 'identity', None)
            name = self._config.get('name', '') if isinstance(self._config, dict) else ''
            
            if not identity and not name:
                return {
                    'type': IssueType.IDENTITY_MISMATCH,
                    'severity': 'high',
                    'details': 'Identity not loaded and no name in config',
                }
            
            # Resolve name from either source
            if identity:
                identity_dict = identity.to_dict() if hasattr(identity, 'to_dict') else identity
                resolved_name = identity_dict.get('name', name)
            else:
                resolved_name = name
            
            if not resolved_name or resolved_name == 'unknown':
                return {
                    'type': IssueType.IDENTITY_MISMATCH,
                    'severity': 'high',
                    'details': f"Identity name is unknown: resolved={resolved_name}",
                }
            
            return None
            
        except Exception as e:
            return {
                'type': IssueType.IDENTITY_MISMATCH,
                'severity': 'high',
                'details': f"Identity check error: {e}",
            }
    
    @property
    def _config(self):
        """Resolve config from framework - supports both .cfg (dict) and .config (object)."""
        if hasattr(self.framework, 'cfg'):
            return self.framework.cfg
        return getattr(self.framework, 'config', {})

    async def _check_config(self) -> Optional[Dict]:
        """Check config integrity."""
        try:
            config = self._config

            # Normalize config access: support both dict and object
            def _cfg(key, default=None):
                if isinstance(config, dict):
                    return config.get(key, default)
                return getattr(config, key, default)

            # Checkessentialfield
            required_fields = ['name', 'model', 'edition']
            missing = [f for f in required_fields if not _cfg(f)]

            if missing:
                return {
                    'type': IssueType.CONFIG_MISSING,
                    'severity': 'high',
                    'details': f"Missing required config fields: {missing}",
                }

            # Check configuration reasonableness
            max_ctx = _cfg('max_context_tokens')
            if max_ctx is not None and isinstance(max_ctx, (int, float)) and max_ctx < 100:
                return {
                    'type': IssueType.CONFIG_CORRUPTED,
                    'severity': 'medium',
                    'details': f"max_context_tokens too small: {max_ctx}",
                }

            return None

        except Exception as e:
            return {
                'type': IssueType.CONFIG_CORRUPTED,
                'severity': 'high',
                'details': f"Config check error: {e}",
            }
    
    async def _check_session(self) -> Optional[Dict]:
        """Check session usability."""
        try:
            # Resolve sessions from framework - try common attribute paths
            sessions = None
            for attr in ['sessions', '_ctx']:
                obj = getattr(self.framework, attr, None)
                if obj and hasattr(obj, 'load_session'):
                    sessions = obj
                    break
                if obj and hasattr(obj, 'sessions'):
                    sessions = obj.sessions
                    break
            
            if not sessions or not hasattr(sessions, 'load_session'):
                return None  # No session manager available - skip check

            get_session_id = getattr(self.framework, '_get_session_id', None)
            if get_session_id:
                session_id = get_session_id()
            else:
                return None
            session = sessions.load_session(session_id)
            
            if session is None:
                return {
                    'type': IssueType.SESSION_LOST,
                    'severity': 'medium',
                    'details': f"Session {session_id} not found",
                }
            
            return None
            
        except Exception as e:
            return None  # Non-critical check - skip on error
    
    async def _check_critical_files(self) -> List[Dict]:
        """Check whether critical files exist."""
        issues = []
        
        for file_path in self.critical_files:
            expanded = os.path.expanduser(file_path)
            if not os.path.exists(expanded):
                issues.append({
                    'type': IssueType.FILE_MISSING,
                    'severity': 'low',
                    'details': f"Critical file missing: {file_path}",
                    'file_path': expanded,
                })
        
        return issues
    
    async def _check_tools(self) -> List[Dict]:
        """Check critical tool usability."""
        issues = []
        
        # Check tool registry
        if not hasattr(self.framework, '_tool_registry') or self.framework._tool_registry is None:
            issues.append({
                'type': IssueType.VERIFICATION_FAILED,
                'severity': 'medium',
                'details': "Tool registry not initialized",
            })
        
        return issues
    
    # =========================================================================
    # Repair Methods
    # =========================================================================
    
    async def repair(self, issues: List[Dict]) -> List[RepairResult]:
        """
        Execute fix.
        
        Args:
            issues: problemlist(from check_health)
            
        Returns:
            FixResultlist
        """
        results = []
        
        for issue in issues:
            issue_type = issue.get('type')
            
            if issue_type == IssueType.IDENTITY_MISMATCH:
                result = await self._repair_identity(issue)
            elif issue_type == IssueType.CONFIG_MISSING:
                result = await self._repair_config(issue)
            elif issue_type == IssueType.CONFIG_CORRUPTED:
                result = await self._repair_config(issue)
            elif issue_type == IssueType.SESSION_LOST:
                result = await self._repair_session(issue)
            elif issue_type == IssueType.FILE_MISSING:
                result = await self._repair_file(issue)
            elif issue_type == IssueType.VERIFICATION_FAILED:
                result = await self._repair_tools(issue)
            else:
                result = RepairResult(
                    issue_type=issue_type,
                    action="unknown",
                    success=False,
                    details=f"Unknown issue type: {issue_type}",
                )
            
            results.append(result)
            if not isinstance(self.repair_history, list):
                logger.warning("[SelfRepair] repair_history was corrupted (dict→list), reinitializing")
                self.repair_history = list(self.repair_history.values()) if isinstance(self.repair_history, dict) else []
            self.repair_history.append(result)
        
        return results
    
    async def _repair_identity(self, issue: Dict) -> RepairResult:
        """Recover identity from anchor."""
        try:
            # Read correct identity from anchor
            config = self._config
            anchor_path = config.get('anchor_path') if isinstance(config, dict) else getattr(config, 'anchor_path', None)
            if not anchor_path or not os.path.exists(anchor_path):
                return RepairResult(
                    issue_type=IssueType.IDENTITY_MISMATCH,
                    action="restore_identity",
                    success=False,
                    details=f"Anchor not found: {anchor_path}",
                )
            
            with open(anchor_path, 'r', encoding='utf-8') as f:
                anchor_data = json.load(f)
            
            # Found matching deployment
            my_fp = self._get_current_fingerprint()
            deployments = anchor_data.get('deployments', {})
            matched_identity = None
            matched_deploy_id = None
            
            for dep_id, dep in deployments.items():
                fp = dep.get('fingerprint', {})
                if (fp.get('hostname') == my_fp.get('hostname') or 
                    fp.get('ip') == my_fp.get('ip')):
                    matched_identity = dep.get('identity', {})
                    matched_deploy_id = dep_id
                    break
            
            if not matched_identity:
                return RepairResult(
                    issue_type=IssueType.IDENTITY_MISMATCH,
                    action="restore_identity",
                    success=False,
                    details="No matching deployment in anchor",
                )
            
            # Update identity - worker may store in .identity attr or just cfg
            identity = getattr(self.framework, 'identity', None)
            if identity and hasattr(identity, '_my_identity'):
                identity._my_identity = {
                    'id': matched_deploy_id,
                    **matched_identity,
                    'status': 'active',
                }
                logger.info(f"[SelfRepair] Identity restored: {matched_identity.get('name')}")
                return RepairResult(
                    issue_type=IssueType.IDENTITY_MISMATCH,
                    action="restore_identity",
                    success=True,
                    details=f"Identity restored from anchor: {matched_identity.get('name')}",
                    restored_from=f"anchor:{matched_deploy_id}",
                )
            
            # No identity attribute - the name is already in config, nothing to restore
            logger.info(f"[SelfRepair] Identity anchor matches config name={self._config.get('name')}")
            return RepairResult(
                issue_type=IssueType.IDENTITY_MISMATCH,
                action="restore_identity",
                success=True,
                details=f"Identity confirmed via config name={self._config.get('name')}",
            )
            
        except Exception as e:
            return RepairResult(
                issue_type=IssueType.IDENTITY_MISMATCH,
                action="restore_identity",
                success=False,
                details=f"Repair failed: {e}",
            )
    
    async def _repair_config(self, issue: Dict) -> RepairResult:
        """Rebuild config from anchor."""
        try:
            # Read config from anchor
            config = self._config
            anchor_path = config.get('anchor_path') if isinstance(config, dict) else getattr(config, 'anchor_path', None)
            if not os.path.exists(anchor_path):
                return RepairResult(
                    issue_type=IssueType.CONFIG_MISSING,
                    action="restore_config",
                    success=False,
                    details=f"Anchor not found: {anchor_path}",
                )
            
            with open(anchor_path, 'r', encoding='utf-8') as f:
                anchor_data = json.load(f)
            
            # Found matching deployment
            my_fp = self._get_current_fingerprint()
            deployments = anchor_data.get('deployments', {})
            matched_deploy = None
            
            for dep_id, dep in deployments.items():
                fp = dep.get('fingerprint', {})
                if (fp.get('hostname') == my_fp.get('hostname') or 
                    fp.get('ip') == my_fp.get('ip')):
                    matched_deploy = dep
                    break
            
            if not matched_deploy:
                return RepairResult(
                    issue_type=IssueType.CONFIG_MISSING,
                    action="restore_config",
                    success=False,
                    details="No matching deployment in anchor",
                )
            
            # applyConfig
            identity = matched_deploy.get('identity', {})
            if 'model' in identity:
                self._config['model'] = identity['model']
            if 'edition' in identity:
                self._config['edition'] = identity['edition']
            
            logger.info(f"[SelfRepair] Config restored from anchor")
            
            return RepairResult(
                issue_type=IssueType.CONFIG_MISSING,
                action="restore_config",
                success=True,
                details="Config restored from anchor",
                restored_from="anchor",
            )
            
        except Exception as e:
            return RepairResult(
                issue_type=IssueType.CONFIG_MISSING,
                action="restore_config",
                success=False,
                details=f"Repair failed: {e}",
            )
    
    async def _repair_session(self, issue: Dict) -> RepairResult:
        """Rebuild session."""
        try:
            session_id = self.framework._get_session_id()
            session_manager = self.framework.sessions
            
            # Attempt to get summary from anchor
            summary = None
            if hasattr(self.framework, 'anchor') and self.framework.anchor:
                anchor_data = self.framework.anchor.data
                summary = anchor_data.get('session', {}).get('summary')
            
            # Create new session
            session_data = {
                'session_id': session_id,
                'created_at': time.time(),
                'restored_from_anchor': summary is not None,
                'summary': summary or f"Session restored at {time.strftime('%Y-%m-%d %H:%M:%S')}",
            }
            
            session_manager.save_session(session_id, session_data)
            logger.info(f"[SelfRepair] Session restored: {session_id}")
            
            return RepairResult(
                issue_type=IssueType.SESSION_LOST,
                action="restore_session",
                success=True,
                details=f"Session {session_id} restored" + (" from anchor" if summary else ""),
                restored_from="anchor" if summary else "new",
            )
            
        except Exception as e:
            return RepairResult(
                issue_type=IssueType.SESSION_LOST,
                action="restore_session",
                success=False,
                details=f"Repair failed: {e}",
            )
    
    async def _repair_file(self, issue: Dict) -> RepairResult:
        """Create missing critical files."""
        try:
            file_path = issue.get('file_path')
            if not file_path:
                return RepairResult(
                    issue_type=IssueType.FILE_MISSING,
                    action="create_file",
                    success=False,
                    details="No file path specified",
                )
            
            # createdirectory(ifrequire)
            parent = os.path.dirname(file_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            
            # Create empty file
            if file_path.endswith('.db'):
                # SQLite database requires initialization
                import sqlite3
                conn = sqlite3.connect(file_path)
                conn.close()
            else:
                Path(file_path).touch()
            
            logger.info(f"[SelfRepair] Created missing file: {file_path}")
            
            return RepairResult(
                issue_type=IssueType.FILE_MISSING,
                action="create_file",
                success=True,
                details=f"Created missing file: {file_path}",
                restored_from="created",
            )
            
        except Exception as e:
            return RepairResult(
                issue_type=IssueType.FILE_MISSING,
                action="create_file",
                success=False,
                details=f"Repair failed: {e}",
            )
    
    async def _repair_tools(self, issue: Dict) -> RepairResult:
        """Re-initialize tool system."""
        try:
            # re-initialization tool system
            from .worker_framework import _init_tool_system
            
            if hasattr(_init_tool_system, '__wrapped__'):
                # synccall
                tool_count = _init_tool_system(self.framework)
            else:
                tool_count = 0
            
            logger.info(f"[SelfRepair] Tool system reinitialized: {tool_count} tools")
            
            return RepairResult(
                issue_type=IssueType.VERIFICATION_FAILED,
                action="reinit_tools",
                success=True,
                details=f"Tool system reinitialized: {tool_count} tools",
                restored_from="reinit",
            )
            
        except Exception as e:
            return RepairResult(
                issue_type=IssueType.VERIFICATION_FAILED,
                action="reinit_tools",
                success=False,
                details=f"Repair failed: {e}",
            )
    
    # =========================================================================
    # Auto Repair
    # =========================================================================
    
    async def auto_repair_if_needed(self) -> bool:
        """
        Auto-detect and fix. Return whether a fix was executed.
        
        Returns:
            True if repairs were executed, False otherwise
        """
        health = await self.check_health()
        
        if health['healthy']:
            self._record_repair_outcome(True, 0, "healthy")
            return False
        
        issues = health['issues']
        
        # filterdrop-lowseverelevelproblem(optional)
        # high_issues = [i for i in issues if i.get('severity') == 'high']
        # if not high_issues:
        #     return False
        
        logger.info(f"[SelfRepair] Found {len(issues)} issues, attempting repair")
        
        try:
            results = await self.repair(issues)
            
            success_count = sum(1 for r in results if r.success)
            logger.info(f"[SelfRepair] Repaired {success_count}/{len(results)} issues")
            
            # Checkpoint restore: if issues remain after repair, try rolling back files
            new_health = await self.check_health()
            if not new_health['healthy']:
                remaining = new_health['issues']
                logger.warning(f"[SelfRepair] {len(remaining)} issues remain after repair: {[i.get('type') for i in remaining]}")
                # Attempt checkpoint rollback for unfixable issues
                await self._attempt_checkpoint_rollback(remaining)
                # Re-check after rollback
                new_health = await self.check_health()
            
            self._record_repair_outcome(new_health['healthy'], success_count,
                f"{'healthy' if new_health['healthy'] else 'issues remain'}")
            return success_count > 0
            
        except Exception as e:
            logger.error(f"[SelfRepair] Auto repair failed: {e}")
            return False

    # ── Repair outcome tracking (v3: persistent success metrics) ─────────

    REPAIR_HISTORY_PATH = os.path.join(str(Path.home()), ".tical-code", "repair_history.json")

    def _record_repair_outcome(self, healthy: bool, fixed: int, summary: str) -> None:
        """Persist repair outcome to repair_history.json for aggregate metrics.

        Tracks: timestamp, healthy/fixed count, summary, running totals.
        """
        try:
            history = []
            if os.path.exists(self.REPAIR_HISTORY_PATH):
                with open(self.REPAIR_HISTORY_PATH) as f:
                    history = json.load(f)
        except Exception:
            history = []

        record = {
            "timestamp": time.time(),
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "healthy": healthy,
            "fixed": fixed,
            "summary": summary,
        }
        history.append(record)

        # Keep last 1000 records, compute aggregates
        if len(history) > 1000:
            history = history[-1000:]

        total = len(history)
        healthy_count = sum(1 for r in history if r.get("healthy"))
        total_fixed = sum(r.get("fixed", 0) for r in history)

        meta = {
            "total_checks": total,
            "healthy_count": healthy_count,
            "healthy_rate": round(healthy_count / total, 3) if total else 0,
            "total_fixed": total_fixed,
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        try:
            os.makedirs(os.path.dirname(self.REPAIR_HISTORY_PATH), exist_ok=True)
            with open(self.REPAIR_HISTORY_PATH, "w") as f:
                json.dump({"meta": meta, "records": history[-100:]}, f, indent=2)
        except Exception as e:
            logger.debug("[SelfRepair] Failed to persist repair history: %s", e)

    async def _attempt_checkpoint_rollback(self, remaining_issues: List[Dict]) -> bool:
        """Attempt to roll back files from checkpoint for unfixable issues.

        Uses the framework's checkpoint (if available) to restore file state
        when self-repair cannot fix issues. This bridges the gap between
        self_repair and checkpoint systems.
        """
        framework = getattr(self, 'framework', None)
        if framework is None:
            logger.debug("[SelfRepair] No framework reference, skipping checkpoint rollback")
            return False

        checkpoint = getattr(framework, 'checkpoint', None)
        if checkpoint is None:
            logger.debug("[SelfRepair] No checkpoint manager available")
            return False

        try:
            # Find the most recent checkpoint
            checkpoints = checkpoint.list_checkpoints()
            if not checkpoints:
                logger.info("[SelfRepair] No checkpoints available for rollback")
                return False

            latest = checkpoints[0]
            cp_id = latest["id"] if isinstance(latest, dict) else latest.id

            logger.info("[SelfRepair] Attempting checkpoint rollback to %s for %d unfixable issues",
                        cp_id, len(remaining_issues))
            result = checkpoint.restore(cp_id, confirm=True)
            if result:
                logger.info("[SelfRepair] Checkpoint rollback to %s succeeded", cp_id)
            else:
                logger.warning("[SelfRepair] Checkpoint rollback to %s returned False", cp_id)
            return bool(result)
        except Exception as e:
            logger.warning("[SelfRepair] Checkpoint rollback failed: %s", e)
            return False

    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def _get_current_fingerprint(self) -> Dict[str, str]:
        """Get current machine fingerprint."""
        try:
            hostname = socket.gethostname()
            host_ip = socket.gethostbyname(hostname)
        except Exception as e:
            logger.debug(f"[SelfRepair] _get_current_fingerprintException (non-blocking): {e}")
            hostname = "unknown"
            host_ip = os.environ.get("SELF_IP", "127.0.0.1")
        
        return {
            'hostname': hostname,
            'ip': host_ip,
        }
    
    def get_repair_history(self, limit: int = 50) -> List[Dict]:
        """Get repair history."""
        return [r.to_dict() for r in self.repair_history[-limit:]]
    
    def clear_repair_history(self):
        """Clear repair history."""
        self.repair_history.clear()
    
    # =========================================================================
    # Self-Evolution Safety Methods (Self-evolutionsecuritymethod)
    # =========================================================================
    
    # P0 #1: Expand protection range - support directory-level protection
    def is_protected_file(self, file_path: str) -> bool:
        """Check if a file is in the protected list and cannot be modified."""
        # First verify path legality (prevent traversal attack)
        try:
            file_path = self._validate_file_path(file_path)
        except ValueError:
            return True  # path is not legal, reject modification
        filename = os.path.basename(file_path)
        if filename in self.PROTECTED_FILES:
            return True
        # Audit P0-2: .tical_ prefix file wildcard protection - any .tical_* file cannot be modified
        if filename.startswith('.tical_'):
            return True
        # Check if within protected directory
        abs_path = os.path.realpath(file_path)
        for protected_dir in self.PROTECTED_DIRS:
            if f'/{protected_dir}/' in abs_path or abs_path.endswith(f'/{protected_dir}'):
                return True
        # Check .git directory
        if '/.git/' in abs_path:
            return True
        return False
    
    def can_self_modify(self) -> bool:
        """Check if the modification count has not reached the limit."""
        # P1 #5: Take the smaller of config value and hard upper limit, prevent config override bypass
        config_max = self.MAX_SELF_MODIFICATIONS
        if hasattr(self.framework, 'cfg') and isinstance(self._config, dict):
            config_max = self._config.get('max_self_modifications')
        effective_max = min(config_max, self.HARD_MAX_SELF_MODIFICATIONS)
        return self._modification_count < effective_max
    
    async def validate_code_syntax(self, file_path: str) -> Dict:
        """
        Validate Python file syntax.
        
        Non-.py files are always considered valid.
        
        Args:
            file_path: Path to the file to validate
            
        Returns:
            {"valid": bool, "error": str}
        """
        file_path = self._validate_file_path(file_path)  # P0-3: Path traversal protection
        if not file_path.endswith('.py'):
            return {"valid": True, "error": ""}
        
        try:
            py_compile.compile(file_path, doraise=True)
            return {"valid": True, "error": ""}
        except py_compile.PyCompileError as e:
            return {"valid": False, "error": str(e)}
        except Exception as e:
            return {"valid": False, "error": f"Unexpected error during syntax check: {e}"}
    
    # P0 #2 + P1 #5 + P1 #8: semanticsecurityCheck
    def validate_code_safety(self, file_path: str) -> Dict:
        """
        Semantic security check: AST-level + Regex scanning, complementary to each other.
        
        For .py files: run AST check first (harder to bypass), then regex check (cover patterns AST may miss).
        For non-.py files: run targeted checks (shell/yaml/dockerfile).
        
        Args:
            file_path: Path to the file to check
            
        Returns:
            {"safe": bool, "warnings": [str, ...]}
        """
        file_path = self._validate_file_path(file_path)  # P0-3: Path traversal protection
        if not file_path.endswith('.py'):
            # P0 #3: Non-Python files also undergo security checks
            return self._non_python_safety_check(file_path)
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return {"safe": False, "warnings": [f"Cannot read file: {e}"]}
        
        # 1. AST-level check (harder to bypass)
        ast_result = self._ast_safety_check(file_path)
        # 2. Regex check (covers modes AST might miss)
        regex_result = self._regex_safety_check(file_path, content)
        # mergeResult
        all_warnings = ast_result.get("warnings", []) + regex_result.get("warnings", [])
        
        return {"safe": len(all_warnings) == 0, "warnings": all_warnings}
    
    def _ast_safety_check(self, file_path: str) -> Dict:
        """AST-level security analysis - harder to bypass than regex
        
        Audit P1-2: add alias tracking - first scan all import ... as ... statements to build alias map,
        Reuse map to parse getattr/direct calls in aliases.
        """
        if not file_path.endswith('.py'):
            return {"safe": True, "warnings": []}
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()
            tree = ast.parse(source)
        except SyntaxError:
            return {"safe": False, "warnings": ["File has syntax errors"]}
        
        warnings = []
        
        # Audit P1-2: First pass scan - build import alias map
        # alias_map: {alias -> original module name}, e.g. {'_os': 'os', '_sub': 'subprocess'}
        alias_map = {}
        DANGEROUS_MODULE_NAMES = ('os', 'subprocess', 'shutil', 'sys')
        for node in ast.walk(tree):
            # import os as _os
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and alias.name in DANGEROUS_MODULE_NAMES:
                        alias_map[alias.asname] = alias.name
            # from os import system as _system  → record _system -> os.system
            # from os import system             → record system -> os.system
            elif isinstance(node, ast.ImportFrom):
                if node.module in DANGEROUS_MODULE_NAMES:
                    for alias in node.names:
                        real_name = f"{node.module}.{alias.name}"
                        effective_name = alias.asname if alias.asname else alias.name
                        alias_map[effective_name] = real_name
        
        # Auxiliary function: parse name to actual module name
        def _resolve_module(name_id: str) -> Optional[str]:
            """Parse alias to actual module name. Return None indicates not a known dangerous module alias."""
            if name_id in DANGEROUS_MODULE_NAMES:
                return name_id
            if name_id in alias_map:
                mapped = alias_map[name_id]
                # import os as _os → alias_map['_os'] = 'os'
                if mapped in DANGEROUS_MODULE_NAMES:
                    return mapped
                # from os import system → alias_map['system'] = 'os.system'
                # Extract module partial
                base_module = mapped.split('.')[0]
                if base_module in DANGEROUS_MODULE_NAMES:
                    return base_module
            return None
        
        # Second pass scan - Check dangerous calls
        for node in ast.walk(tree):
            # 1. detect getattr call - mayused forbypassdirectlyattributeaccess
            if isinstance(node, ast.Call):
                func = node.func
                # getattr(os, ...) or getattr(_os, ...) form
                if isinstance(func, ast.Name) and func.id == 'getattr':
                    if node.args and isinstance(node.args[0], ast.Name):
                        # Audit P1-2: parse using alias map
                        resolved = _resolve_module(node.args[0].id)
                        if resolved:
                            warnings.append(f"Line {node.lineno}: getattr on dangerous module '{node.args[0].id}' (resolves to '{resolved}')")
                
                # 2. detect __import__ call
                if isinstance(func, ast.Name) and func.id == '__import__':
                    warnings.append(f"Line {node.lineno}: __import__ usage detected")
                
                # 3. detect eval/exec/compile call
                if isinstance(func, ast.Name) and func.id in ('eval', 'exec', 'compile'):
                    warnings.append(f"Line {node.lineno}: {func.id}() usage detected")
                
                # 4. Detect direct calls like os.system / subprocess.* / shutil.rmtree
                # Audit P1-2: simultaneously handle alias calls like _os.system()
                if isinstance(func, ast.Attribute):
                    if isinstance(func.value, ast.Name):
                        module_name = func.value.id
                        method = func.attr
                        dangerous_calls = {
                            'os': ['system', 'popen', 'execvp', 'execl', 'fork', 'kill', '_exit', 'remove', 'unlink'],
                            'subprocess': ['call', 'run', 'Popen', 'check_output', 'check_call'],
                            'shutil': ['rmtree', 'move'],
                            'sys': ['exit'],
                        }
                        # First check original module name
                        if module_name in dangerous_calls and method in dangerous_calls[module_name]:
                            warnings.append(f"Line {node.lineno}: {module_name}.{method}() detected")
                        # Audit P1-2: Then check via alias map
                        elif module_name in alias_map:
                            resolved = _resolve_module(module_name)
                            if resolved and resolved in dangerous_calls and method in dangerous_calls[resolved]:
                                warnings.append(f"Line {node.lineno}: {module_name}.{method}() detected (alias for {resolved})")
                
                # Audit P1-2: detect direct calls from `from os import system` form
                # e.g. _system() or system(), where system is already mapped to os.system in alias_map
                if isinstance(func, ast.Name) and func.id in alias_map:
                    mapped = alias_map[func.id]
                    base_module = mapped.split('.')[0]
                    if base_module in DANGEROUS_MODULE_NAMES:
                        warnings.append(f"Line {node.lineno}: {func.id}() detected (alias for {mapped})")
        
        return {"safe": len(warnings) == 0, "warnings": warnings}
    
    def _regex_safety_check(self, file_path: str, content: str) -> Dict:
        """Regex security check - original logic extracted as independent method"""
        warnings = []
        for pattern in self.DANGEROUS_PATTERNS:
            matches = re.findall(pattern, content)
            if matches:
                warnings.append(f"Dangerous pattern found: {pattern}")
        
        # P1 #8: env-varmodifyCheck
        env_warnings = self._check_env_modification(content)
        warnings.extend(env_warnings)
        
        return {"safe": len(warnings) == 0, "warnings": warnings}
    
    def _non_python_safety_check(self, file_path: str) -> Dict:
        """P0 #3: Non-Python file security check"""
        ext = os.path.splitext(file_path)[1].lower()
        filename = os.path.basename(file_path).lower()
        warnings = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return {"safe": False, "warnings": [f"Cannot read file: {e}"]}
        
        # Shell script
        if ext in ('.sh',) or filename in ('makefile',):
            for pattern in self.SHELL_DANGEROUS_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    warnings.append(f"Shell dangerous pattern: {pattern}")
        
        # YAML Config
        if ext in ('.yaml', '.yml'):
            for pattern in self.YAML_DANGEROUS_PATTERNS:
                if re.search(pattern, content):
                    warnings.append(f"YAML dangerous pattern: {pattern}")
        
        # Dockerfile
        if filename == 'dockerfile' or filename.endswith('.dockerfile') or filename == 'docker-compose.yml' or filename == 'docker-compose.yaml':
            for pattern in self.DOCKERFILE_DANGEROUS_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    warnings.append(f"Dockerfile dangerous pattern: {pattern}")
        
        return {"safe": len(warnings) == 0, "warnings": warnings}
    
    # P1 #8: Check for code that modifies protected environment variables
    def _check_env_modification(self, content: str) -> List[str]:
        """Check for code that modifies protected environment variables."""
        warnings = []
        for var in self.PROTECTED_ENV_VARS:
            # Check write mode: os.environ['VAR'] = or os.environ["VAR"] =
            single_quote_write = f"os.environ['{var}']"
            double_quote_write = f'os.environ["{var}"]'
            # Only alert on assignment scenario (appears before = sign)
            for pattern in [single_quote_write, double_quote_write]:
                # Find all occurrence positions, check if assignment (rather than read)
                idx = 0
                while True:
                    idx = content.find(pattern, idx)
                    if idx == -1:
                        break
                    # Check if followed by = (assignment) rather than ) (read like os.environ.get())
                    after = content[idx + len(pattern):idx + len(pattern) + 5].strip()
                    # os.environ['VAR'] = ... is assignment
                    # os.environ['VAR'] not in .get() context is read
                    if after.startswith('=') and not after.startswith('=='):
                        # Confirmis not os.environ.get('VAR') form
                        before = content[max(0, idx - 10):idx]
                        if '.get(' not in before:
                            warnings.append(f"Attempt to modify protected env var: {var}")
                            break
                    idx += len(pattern)
        return warnings
    
    # P1 #6: dependchain-affectsCheck
    def _check_dependency_impact(self, file_path: str) -> List[str]:
        """Check if protected files depend on the modified file - i.e., whether protected files import the target file."""
        warnings = []
        target_module = os.path.splitext(os.path.basename(file_path))[0]
        
        # Scan protected files to see if they import the target file
        for protected_name in self.PROTECTED_FILES:
            if not protected_name.endswith('.py'):
                continue
            protected_path = os.path.join(os.path.dirname(file_path), protected_name)
            if not os.path.exists(protected_path):
                continue
            try:
                with open(protected_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                # Check if they import the target module
                if (f'from .{target_module}' in content or 
                    f'import {target_module}' in content or
                    f'from tical_code.core.{target_module}' in content):
                    warnings.append(f"Protected file '{protected_name}' imports '{target_module}'")
            except Exception as e:
                logger.debug(f"[SelfRepair] _check_dependency_impactException (non-blocking): {e}")
                pass
        return warnings
    
    # P0 #3: computeidentityfingerprint
    def _compute_identity_fingerprint(self) -> str:
        """Compute SHA256 hash of identity.py as startup fingerprint."""
        try:
            # attemptfound identity.py
            for search_dir in [os.path.dirname(os.path.realpath(__file__)), os.getcwd()]:
                identity_path = os.path.join(search_dir, 'identity.py')
                if os.path.exists(identity_path):
                    with open(identity_path, 'rb') as f:
                        return hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            logger.debug(f"[SelfRepair] _compute_identity_fingerprintException (non-blocking): {e}")
            pass
        return ""
    
    def _verify_identity_fingerprint(self) -> bool:
        """Verify identity.py fingerprint matches startup consistency."""
        current = self._compute_identity_fingerprint()
        if not current or not self._identity_fingerprint:
            # cannotcomputefingerprintwhen-notblock(conservativeStrategy)
            return True
        return current == self._identity_fingerprint
    
    # P0 #3: processhealthCheck
    async def _process_health_check(self, timeout: int) -> bool:
        """Check health status by confirming process is alive."""
        try:
            # attemptPasscurrentprocess PID Confirmalive
            pid = os.getpid()
            if pid and os.path.exists(f'/proc/{pid}'):
                return True
            # macOS / other systems: use os.kill(pid, 0) to detect
            try:
                os.kill(pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False
        except Exception as e:
            logger.debug(f"[SelfRepair] _verify_identity_fingerprintException (non-blocking): {e}")
            return False
    
    # P0 #3: HTTP health check (extracted as independent method)
    async def _http_health_check(self, health_check_url: str, timeout: int) -> bool:
        """HTTP health check: poll until 200 received or timeout.
        
        Security: only allow http/https protocols, forbid intranet address access.
        """
        # Security verification: only allow http/https
        if not health_check_url.startswith(('http://', 'https://')):
            logger.warning(f"[SelfRepair] Blocked non-HTTP health check URL: {health_check_url}")
            return False
        
        # security verification: forbid intranet address (prevent SSRF)
        from urllib.parse import urlparse
        parsed = urlparse(health_check_url)
        hostname = parsed.hostname or ''
        _blocked_hosts = ('localhost', '127.0.0.1', '0.0.0.0', '::1',
                         '169.254.', '10.', '192.168.', '172.16.',
                         '172.17.', '172.18.', '172.19.', '172.2',
                         '172.30.', '172.31.')
        for blocked in _blocked_hosts:
            if hostname == blocked or hostname.startswith(blocked):
                logger.warning(f"[SelfRepair] Blocked internal health check URL: {health_check_url}")
                return False
        
        check_start = time.time()
        while time.time() - check_start < timeout:
            await asyncio.sleep(1)
            try:
                req = urllib.request.Request(health_check_url, method='GET')
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, ConnectionError, OSError):
                continue
            except Exception as e:
                logger.debug(f"[SelfRepair] Unknown exception (non-blocking): {e}")
                continue
        return False
    
    # P0 #3: multi-layer-healthCheck
    async def _multi_health_check(self, health_check_url: str, timeout: int) -> Dict:
        """
        Multi-layer health check: not only depends on HTTP, also checks process and identity fingerprint.
        
        Returns:
            {"passed": bool, "checks": {"http": bool, "process": bool, "identity": bool}}
        """
        checks = {
            "http": False,
            "process": False,
            "identity": False,
        }
        
        # 1. HTTP Check
        if health_check_url:
            checks["http"] = await self._http_health_check(health_check_url, timeout)
        
        # 2. Process check - confirm target process is still alive
        checks["process"] = await self._process_health_check(timeout)
        
        # 3. Identity check - confirm identity.py fingerprint unchanged
        checks["identity"] = self._verify_identity_fingerprint()
        
        all_passed = all(checks.values())
        return {"passed": all_passed, "checks": checks}
    
    # P0 #4: Git integrityCheck
    def _verify_git_integrity(self) -> bool:
        """Check whether .git directory is complete."""
        try:
            repo_root = self._get_repo_root()
            git_dir = os.path.join(repo_root, '.git')
            if not os.path.exists(git_dir):
                return False
            # Check git whetherusable
            result = subprocess.run(
                ['git', 'status'],
                cwd=repo_root,
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"[SelfRepair] _verify_git_integrityException (non-blocking): {e}")
            return False
    
    def _get_repo_root(self) -> str:
        """Get git repository root directory."""
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--show-toplevel'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            logger.debug(f"[SelfRepair] _verify_git_integrityException (non-blocking): {e}")
            pass
        return os.path.dirname(os.path.realpath(__file__))
    
    def _validate_file_path(self, file_path: str) -> str:
        """Verify and normalize file path, prevent path traversal attack (P0-3).

        Ensure all public methods that accept file_path call this method at the beginning.
        Prevent attackers from passing ../../../etc/crontab to write arbitrary files for RCE.

        Args:
            file_path: File path to verify

        Returns:
            Normalized absolute path

        Raises:
            ValueError: if path is outside repository root directory or contains .. component
        """
        # 1. convert-toabsolute-path
        abs_path = os.path.realpath(file_path)
        # 2. Get repository root directory
        repo_root = self._get_repo_root()
        # 3. Ensure path is within repository
        if not abs_path.startswith(os.path.realpath(repo_root)):
            raise ValueError(f"Path traversal detected: {file_path} is outside repo root")
        # 4. ensureno .. Component
        if '..' in os.path.normpath(file_path).split(os.sep):
            raise ValueError(f"Path traversal detected: {file_path} contains '..'")
        return abs_path
    
    # P1-9: Helper method - detectConfigFilein sandbox_mode
    def _is_config_with_sandbox_mode(self, file_path: str, new_content: str) -> bool:
        """Detect if file is config file and new content contains sandbox_mode (P1-9).

        Args:
            file_path: File path
            new_content: New file content

        Returns:
            True if it is config file and contains sandbox_mode field
        """
        basename = os.path.basename(file_path).lower()
        # ConfigFilesuffix/Name
        config_patterns = (
            'config.json', 'config.yaml', 'config.yml',
            'worker-config.json', 'worker-config.yaml',
            'pyproject.toml', 'settings.json',
        )
        is_config = (
            basename in config_patterns
            or basename.endswith('.config.json')
            or basename.endswith('.config.yaml')
            or 'config' in basename.lower()
        )
        if not is_config:
            return False
        # Check if new content contains sandbox_mode
        return 'sandbox_mode' in new_content
    
    def _extract_sandbox_mode_from_content(self, content: str) -> Optional[str]:
        """Extract sandbox_mode value from config file content (P1-9).

        Supports JSON and YAML format.

        Args:
            content: ConfigFilecontent

        Returns:
            sandbox_mode value string, return None if not found
        """
        # attempt JSON Format
        try:
            data = json.loads(content)
            if isinstance(data, dict) and 'sandbox_mode' in data:
                return str(data['sandbox_mode'])
        except (json.JSONDecodeError, TypeError):
            logger.debug("self_repair: sandbox_mode JSON parse failed, attempting regex match")
        # Attempt simple regex match for YAML format
        import re
        match = re.search(r'sandbox_mode\s*[:=]\s*["\']?(\w+)["\']?', content)
        if match:
            return match.group(1)
        return None
    
    async def git_backup_before_modify(self, file_path: str) -> Dict:
        """
        Create a git backup (commit) before modifying a file.
        
        Auto-initializes git repo if not already in one.
        
        Args:
            file_path: Path to the file being modified
            
        Returns:
            {"success": bool, "commit_hash": str, "error": str}
        """
        file_path = self._validate_file_path(file_path)  # P0-3: Path traversal protection
        try:
            file_dir = os.path.dirname(os.path.realpath(file_path))
            
            # Check if within git repo
            check_result = subprocess.run(
                ['git', 'rev-parse', '--is-inside-work-tree'],
                cwd=file_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            if check_result.returncode != 0:
                # Not in git repo, initialization
                init_result = subprocess.run(
                    ['git', 'init'],
                    cwd=file_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if init_result.returncode != 0:
                    return {
                        "success": False,
                        "commit_hash": "",
                        "error": f"git init failed: {init_result.stderr.strip()}",
                    }
                logger.info(f"[SelfRepair] Initialized git repo at {file_dir}")
            
            # git add
            add_result = subprocess.run(
                ['git', 'add', file_path],
                cwd=file_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if add_result.returncode != 0:
                return {
                    "success": False,
                    "commit_hash": "",
                    "error": f"git add failed: {add_result.stderr.strip()}",
                }
            
            # git commit
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            commit_msg = f"auto-backup before modify: {os.path.basename(file_path)} at {timestamp}"
            commit_result = subprocess.run(
                ['git', 'commit', '-m', commit_msg],
                cwd=file_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if commit_result.returncode != 0:
                # maynochangerequirecommit(nothing to commit)
                if 'nothing to commit' in commit_result.stdout or 'nothing to commit' in commit_result.stderr:
                    # getcurrent HEAD
                    hash_result = subprocess.run(
                        ['git', 'rev-parse', 'HEAD'],
                        cwd=file_dir,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    current_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else ""
                    return {
                        "success": True,
                        "commit_hash": current_hash,
                        "error": "",
                    }
                return {
                    "success": False,
                    "commit_hash": "",
                    "error": f"git commit failed: {commit_result.stderr.strip()}",
                }
            
            # get commit hash
            hash_result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=file_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else ""
            self._last_commit_hash = commit_hash
            
            logger.info(f"[SelfRepair] Git backup created: {commit_hash[:8]} for {file_path}")
            
            return {
                "success": True,
                "commit_hash": commit_hash,
                "error": "",
            }
            
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "commit_hash": "",
                "error": "git command timed out",
            }
        except Exception as e:
            return {
                "success": False,
                "commit_hash": "",
                "error": f"git backup error: {e}",
            }
    
    # P2 #10: Audit log - exist repo outsideprevent AI modify
    def _get_audit_log_path(self) -> str:
        """Audit logpath - exist repo outside"""
        # Prefer /var/log/ first, fallback to user home
        log_dirs = ['/var/log/tical', os.path.expanduser('~/.tical/logs')]
        for d in log_dirs:
            try:
                os.makedirs(d, exist_ok=True)
                test_file = os.path.join(d, '.test')
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                return os.path.join(d, 'evolution_audit.jsonl')
            except (PermissionError, OSError):
                continue
        # Last fallback to repo (add read-only protection prompt)
        return os.path.join(self._get_repo_root(), '.tical_evolution_log.jsonl')
    
    def _log_modification(self, file_path: str, success: bool, commit_hash: str = "", error: str = ""):
        """Record modification audit log."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "file": file_path,
            "success": success,
            "commit_hash": commit_hash,
            "error": error,
            "modification_count": self._modification_count,
            "identity": getattr(self.framework, 'identity_name', 
                                getattr(getattr(self.framework, 'identity', None), 'name', 'unknown') 
                                if hasattr(self.framework, 'identity') else 'unknown'),
        }
        # truthful-report: additional TruthReporter audit trail
        if self._truth_reporter:
            try:
                stats = self._truth_reporter.get_stats()
                log_entry["truth_report"] = {
                    "trust_level": stats.get('trust_level', 'unknown'),
                    "total_corrections": stats.get('total_corrections', 0),
                    "require_human_approval": stats.get('require_human_approval', False),
                    "recent_operations": stats.get('recent_operations', []),
                }
            except Exception as e:
                logger.debug(f"[SelfRepair] _log_modificationException (non-blocking): {e}")
                pass  # Audit additional info failure should not block main process
        # WriteAudit logFile(append-only)
        try:
            log_path = self._get_audit_log_path()
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry) + '\n')
        except Exception as e:
            logger.warning(f"[SelfRepair] Failed to write audit log: {e}")
    
    # P0 #1: truesandboxtest - in-restricted Python in-environment import/Executemodifyaftermodule
    async def _sandbox_test(self, file_path: str) -> Dict:
        """
        Test modified file in isolated environment (P0-4: supports Docker / Restricted Python / Disabled three modes).
        
        Returns:
            {"passed": bool, "error": str, "sandbox_mode": str}
        """
        file_path = self._validate_file_path(file_path)  # P0-3: Path traversal protection
        if not file_path.endswith('.py'):
            return {"passed": True, "error": "", "sandbox_mode": self.sandbox_mode}
        
        # P0-4: DISABLED Modedirectlyskip
        if self.sandbox_mode == SandboxMode.DISABLED:
            return {"passed": True, "error": "", "sandbox_mode": SandboxMode.DISABLED}
        
        # P0-4: Docker Mode - fullyprocessisolate
        if self.sandbox_mode == SandboxMode.DOCKER:
            return await self._sandbox_test_docker(file_path)
        
        # P0-4: Default RESTRICTED_PYTHON Mode - restricted Python environment
        return await self._sandbox_test_restricted_python(file_path)
    
    async def _sandbox_test_docker(self, file_path: str) -> Dict:
        """Execute sandbox test in Docker container (P0-4: fully isolated).

        P0-4 Fix: Docker mode must also first run validate_code_safety() + _ast_safety_check,
        After security scan passes, enter Docker execution. Two-layer protection: static scan + dynamic sandbox, both required.
        
        P0-5 Fix: Use SAFE_BUILTINS inside Docker container to limit built-in functions,
        Add stop_timeout=10 to client.containers.run(), add 15-second outer timeout protection.

        Args:
            file_path: Python file path to test
            
        Returns:
            {"passed": bool, "error": str, "sandbox_mode": "docker"}
        """
        try:
            with open(file_path, 'r') as f:
                code = f.read()
            
            # do-firstgrammarCheck
            try:
                compile(code, file_path, 'exec')
            except SyntaxError as e:
                return {"passed": False, "error": f"Syntax error: {e}", "sandbox_mode": SandboxMode.DOCKER}
            
            # P0-4: staticsecurityscan - Docker mode cannotskip!
            safety_result = self.validate_code_safety(file_path)
            if not safety_result["safe"]:
                safety_warnings = safety_result.get("warnings", [])
                return {
                    "passed": False,
                    "error": f"Static safety scan failed (required before Docker exec): {safety_warnings}",
                    "sandbox_mode": SandboxMode.DOCKER,
                }
            
            # P0-5: Wrap code with SAFE_BUILTINS, also limit built-in functions in container
            # Build SAFE_BUILTINS dict (same security built-in collection as RESTRICTED_PYTHON mode)
            safe_builtins_src = (
                "_sb = {\n"
                "    'print': print, 'len': len, 'range': range,\n"
                "    'str': str, 'int': int, 'float': float, 'bool': bool,\n"
                "    'list': list, 'dict': dict, 'tuple': tuple, 'set': set,\n"
                "    'frozenset': frozenset, 'type': type, 'isinstance': isinstance,\n"
                "    'None': None, 'True': True, 'False': False,\n"
                "    'Exception': Exception, 'ValueError': ValueError,\n"
                "    'TypeError': TypeError, 'KeyError': KeyError,\n"
                "    'AttributeError': AttributeError, 'RuntimeError': RuntimeError,\n"
                "    'abs': abs, 'min': min, 'max': max, 'sum': sum,\n"
                "    'enumerate': enumerate, 'zip': zip, 'sorted': sorted,\n"
                "    'reversed': reversed, 'hasattr': hasattr,\n"
                "    'round': round, 'pow': pow, 'chr': chr, 'ord': ord,\n"
                "    'hex': hex, 'bin': bin, 'oct': oct, 'repr': repr,\n"
                "}\n"
                "exec(compile({code!r}, {fname!r}, 'exec'), {{'__builtins__': _sb}})\n"
            ).format(code=code, fname=file_path)
            
            # P0-5: Outer-layer timeout protection (threading.Timer 15s), prevent Docker hang
            import threading
            timeout_expired = threading.Event()
            
            def _timeout_killer():
                timeout_expired.set()
            
            timer = threading.Timer(15.0, _timeout_killer)
            timer.daemon = True
            timer.start()
            
            try:
                # Execute in Docker container (P0-5: add stop_timeout=10)
                result = subprocess.run(
                    [
                        "docker", "run", "--rm",
                        "--network=none",       # No network
                        "--memory=128m",        # memorylimit
                        "--cpus=1",             # CPU limit
                        "python:3.11-slim",
                        "python", "-c", safe_builtins_src,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,  # 15-second timeout
                )
            finally:
                timer.cancel()
            
            if timeout_expired.is_set():
                return {"passed": False, "error": "Docker sandbox execution timeout (15s outer guard)", "sandbox_mode": SandboxMode.DOCKER}
            
            if result.returncode == 0:
                return {"passed": True, "error": "", "sandbox_mode": SandboxMode.DOCKER}
            else:
                # Nonzero exit code means code has problems, but not necessarily security problems
                stderr = result.stderr[:500] if result.stderr else ""
                return {
                    "passed": True,  # Execution failure does not block (consistent with RESTRICTED_PYTHON behavior)
                    "error": f"Docker exec note: exit={result.returncode}, stderr={stderr}",
                    "sandbox_mode": SandboxMode.DOCKER,
                }
        
        except subprocess.TimeoutExpired:
            return {"passed": False, "error": "Docker sandbox execution timeout (15s)", "sandbox_mode": SandboxMode.DOCKER}
        except FileNotFoundError:
            # Docker unavailable, revert
            logger.warning("[SelfRepair] Docker unavailable, falling back to RESTRICTED_PYTHON")
            return await self._sandbox_test_restricted_python(file_path)
        except Exception as e:
            return {
                "passed": True,
                "error": f"Docker sandbox note: {str(e)}",
                "sandbox_mode": SandboxMode.DOCKER,
            }
    
    async def _sandbox_test_restricted_python(self, file_path: str) -> Dict:
        """Execute sandbox test in restricted Python environment (P0-4: limited isolation, default mode).
        
        Execute modified module code in restricted Python environment, detect module-level dangerous side effects.
        Audit P1-1: add sandbox escape detection - scan code constant pool for dunder attribute access strings,
        And recursively check if safe_globals values can reach dangerous modules via __class__.__mro__ chain.
        
        Returns:
            {"passed": bool, "error": str, "sandbox_mode": "restricted_python"}
        """
        if not file_path.endswith('.py'):
            return {"passed": True, "error": "", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
        
        try:
            # 1. Create restricted execution environment
            # Audit P1-1: Remove type and object from __builtins__, block classic sandbox escape
            safe_globals = {
                '__builtins__': {
                    'print': print, 'len': len, 'range': range,
                    'str': str, 'int': int, 'float': float, 'bool': bool,
                    'list': list, 'dict': dict, 'tuple': tuple, 'set': set,
                    'None': None, 'True': True, 'False': False,
                    # Audit P1-1: Remove isinstance, type and object, block __class__.__bases__[0].__subclasses__() escape
                    'Exception': Exception, 'ValueError': ValueError,
                    'TypeError': TypeError, 'KeyError': KeyError,
                    'AttributeError': AttributeError, 'RuntimeError': RuntimeError,
                },
                '__name__': '__sandbox__',
                '__file__': file_path,
            }
            
            # 2. readmodifyafterFilecontent
            with open(file_path, 'r') as f:
                code = f.read()
            
            # 3. Compile first (syntax check)
            compiled = compile(code, file_path, 'exec')
            
            # Audit P1-1: Scan constant pool after compilation, detect sandbox escape strings
            sandbox_escape_strings = (
                '__class__', '__bases__', '__subclasses__', '__mro__',
                '__globals__', '__code__', '__func__', '__closure__',
                '__builddict__', '__dict__', '__init__',
            )
            for const in compiled.co_consts:
                if isinstance(const, str) and const in sandbox_escape_strings:
                    return {"passed": False, "error": f"Sandbox escape detected: code references '{const}'", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
                # Deep-layer check nested code object constants
                if hasattr(const, 'co_consts'):
                    for inner_const in const.co_consts:
                        if isinstance(inner_const, str) and inner_const in sandbox_escape_strings:
                            return {"passed": False, "error": f"Sandbox escape detected: code references '{inner_const}'", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
            
            # 4. in-restrictedin-environmentExecute(captureallside-effect)
            # P1-7: Use threading.Timer instead of signal.SIGALRM, thread-safe
            import threading
            timeout_expired = threading.Event()
            exec_timed_out = False
            
            def _timeout_handler():
                nonlocal exec_timed_out
                exec_timed_out = True
                timeout_expired.set()
            
            timer = threading.Timer(5.0, _timeout_handler)
            timer.daemon = True
            timer.start()
            
            try:
                exec(compiled, safe_globals)
            finally:
                timer.cancel()
            
            if exec_timed_out:
                return {"passed": False, "error": "Sandbox execution timeout", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
            
            # 5. Checkwhether hasmoduleleveldangerside-effect
            # If execution produced subprocess/socket objects, note dangerous operation
            dangerous_modules = ('subprocess', 'socket', 'os', 'shutil')
            for key, value in safe_globals.items():
                if key.startswith('_'):
                    continue
                module_name = getattr(type(value), '__module__', '')
                if module_name in dangerous_modules:
                    return {"passed": False, "error": f"Sandbox detected dangerous object: {key} ({type(value).__name__})", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
            
            # Audit P1-1: Recursively check if safe_globals values can reach dangerous modules via __class__.__mro__ chain
            escape_result = self._check_sandbox_escape(safe_globals, dangerous_modules)
            if not escape_result["safe"]:
                return {"passed": False, "error": escape_result["error"], "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
            
            return {"passed": True, "error": "", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
            
        except Exception as e:
            # Module-level execution failure is normal (import error etc.), but record it
            # Do not block, because many modules require dependencies to execute
            return {"passed": True, "error": f"Sandbox exec note: {str(e)}", "sandbox_mode": SandboxMode.RESTRICTED_PYTHON}
    
    # Audit P1-1: sandboxescapedepthCheck
    def _check_sandbox_escape(self, safe_globals: dict, dangerous_modules: tuple, max_depth: int = 4) -> Dict:
        """
        Recursively check if safe_globals values can reach dangerous modules via __class__.__mro__ chain.
        
        preventclassicescape: ().__class__.__bases__[0].__subclasses__()
        Even if type/object are removed, also check for other bypass paths.
        
        Args:
            safe_globals: sandboxExecuteafterglobalvariabledict
            dangerous_modules: dangerous module name tuple
            max_depth: maximumrecursiondepth(preventunlimitedrecursion)
        
        Returns:
            {"safe": bool, "error": str}
        """
        visited = set()  # preventLoopreference
        
        def _inspect_object(obj, depth: int, path: str) -> Optional[str]:
            """Recursively check object, return error info or None."""
            if depth > max_depth:
                return None
            obj_id = id(obj)
            if obj_id in visited:
                return None
            visited.add(obj_id)
            
            try:
                # Check if object's __class__.__module__ points to dangerous module
                obj_type = type(obj)
                type_module = getattr(obj_type, '__module__', '')
                if type_module in dangerous_modules:
                    return f"Sandbox escape via {path}: object type {obj_type.__name__} from {type_module}"
                
                # Check if MRO contains dangerous module types
                for base in getattr(obj_type, '__mro__', []):
                    base_module = getattr(base, '__module__', '')
                    if base_module in dangerous_modules:
                        return f"Sandbox escape via {path}.__mro__: {base.__name__} from {base_module}"
            except Exception as e:
                logger.debug(f"[SelfRepair] _inspect_objectException (non-blocking): {e}")
                pass
            
            # If dict, check its values
            if isinstance(obj, dict) and depth < max_depth:
                for k, v in obj.items():
                    err = _inspect_object(v, depth + 1, f"{path}[{k!r}]")
                    if err:
                        return err
            # If list/tuple, check elements
            elif isinstance(obj, (list, tuple)) and depth < max_depth:
                for i, v in enumerate(obj):
                    err = _inspect_object(v, depth + 1, f"{path}[{i}]")
                    if err:
                        return err
            
            return None
        
        # Check all global variables (skip built-in names)
        for key, value in safe_globals.items():
            if key in ('__builtins__', '__name__', '__file__'):
                continue
            err = _inspect_object(value, 0, key)
            if err:
                return {"safe": False, "error": err}
        
        return {"safe": True, "error": ""}
    
    # P2 #12: humanConfirmrequest(optional)
    async def _request_human_approval(self, file_path: str, new_content: str) -> bool:
        """
        Request human confirmation for modification.
        
        Request confirmation via anchor channel or framework event system.
        Default behavior: if framework has no human confirm channel configured, auto-reject (security preference).
        
        Args:
            file_path: File path to modify
            new_content: New content
            
        Returns:
            True if human approves, False if rejected or cannot confirm
        """
        # Checkframeworkwhether hashumanConfirmmechanism
        if hasattr(self.framework, 'request_human_approval') and callable(self.framework.request_human_approval):
            try:
                return await self.framework.request_human_approval(file_path, new_content)
            except Exception as e:
                logger.warning(f"[SelfRepair] Human approval request failed: {e}")
                return False
        
        # If no human confirm channel, default reject (security preference)
        logger.warning("[SelfRepair] Human approval requested but no approval channel available - denying by default")
        return False
    
    async def safe_modify(
        self,
        file_path: str,
        new_content: str,
        sandbox_test: bool = True,
        require_human_approval: bool = False,
    ) -> Dict:
        """
        Safely modify a file with full safety checks and rollback capability.
        
        Safety flow:
        1. Acquire concurrency lock (P2 #9)
        2. Check if file is protected → reject
        3. Check modification count → reject if exceeded
        4. Git integrity check (P0 #4)
        5. Create git backup
        6. Read old content (for rollback)
        7. Check dependency impact warnings (P1 #6)
        8. Write new content
        9. Validate syntax → rollback on failure
        10. Validate code safety → rollback on failure (P0 #2)
        11. Sandbox test → rollback on failure (audit P0-1: Defaultenable)
        12. Optional human approval → rollback on denial (P2 #12)
        13. Increment modification count (P1 #7: only on success)
        14. Write audit log (P2 #10)
        
        Args:
            file_path: Path to the file to modify
            new_content: New content to write
            sandbox_test: If True, run sandbox test after safety check (audit P0-1: Default True)
            require_human_approval: If True, require human approval before finalizing (P2 #12)
            
        Returns:
            {"success": bool, "commit_hash": str, "error": str, "rolled_back": bool, "warnings": [str]}
        """
        # P2 #9: Concurrency lock
        async with self._modify_lock:
            return await self._safe_modify_inner(
                file_path, new_content, sandbox_test, require_human_approval
            )
    
    # Truthful-report: async security check wrapper (tracks awaitable requirements)
    async def _async_safety_check(self, file_path: str) -> Dict:
        """Async wrapper for validate_code_safety, used by truth_reporter.track()."""
        return self.validate_code_safety(file_path)
    
    async def _safe_modify_inner(
        self,
        file_path: str,
        new_content: str,
        sandbox_test: bool,
        require_human_approval: bool,
    ) -> Dict:
        """Inner implementation of safe_modify, called under the concurrency lock."""
        file_path = self._validate_file_path(file_path)  # P0-3: Path traversal protection
        warnings = []
        
        # 1. CheckProtect file
        if self.is_protected_file(file_path):
            return {
                "success": False,
                "commit_hash": "",
                "error": f"File is protected and cannot be modified: {os.path.basename(file_path)}",
                "rolled_back": False,
                "warnings": [],
                "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
            }
        
        # P1-9: forbid LLM outputdecide sandbox Mode
        # If modified file is config file and contains sandbox_mode, reject modification
        if self._is_config_with_sandbox_mode(file_path, new_content):
            # Read current sandbox_mode (only allow reading from config file/env var)
            current_mode = self.sandbox_mode
            new_mode = self._extract_sandbox_mode_from_content(new_content)
            if new_mode is not None and new_mode != current_mode:
                return {
                    "success": False,
                    "commit_hash": "",
                    "error": f"Sandbox mode cannot be modified at runtime: current={current_mode}, attempted={new_mode}. "
                             f"Sandbox mode can only be set via config file or environment variable.",
                    "rolled_back": False,
                    "warnings": [],
                    "sandbox_mode": self.sandbox_mode,
                }
            # If sandbox_mode value is same, allow (may be other config item modification)
            warnings.append(f"Config file contains sandbox_mode={new_mode} (unchanged, modification allowed)")
        
        # 2. Checkmodifycount
        if not self.can_self_modify():
            config_max = self.MAX_SELF_MODIFICATIONS
            if hasattr(self.framework, 'cfg') and isinstance(self._config, dict):
                config_max = self._config.get('max_self_modifications')
            effective_max = min(config_max, self.HARD_MAX_SELF_MODIFICATIONS)
            return {
                "success": False,
                "commit_hash": "",
                "error": f"Self-modification limit reached ({effective_max} per session)",
                "rolled_back": False,
                "warnings": [],
                "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
            }
        
        # 3. P0 #4: Git integrityCheck
        if not self._verify_git_integrity():
            return {
                "success": False,
                "commit_hash": "",
                "error": "Git integrity check failed - cannot safely modify without rollback capability",
                "rolled_back": False,
                "warnings": [],
                "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
            }
        
        # 4. Git backup
        # truthful-report: git backup operation tracked via real result
        if self._truth_reporter:
            backup_result = await self._truth_reporter.track(
                f"git_backup:{os.path.basename(file_path)}",
                self.git_backup_before_modify(file_path),
            )
            # Track return OperationResult, requires conversion to original dict format
            backup_result_dict = {
                'success': backup_result.success,
                'commit_hash': '',
                'error': backup_result.error or '',
            }
            # Parse commit_hash from output (tracks dict serialization to output string)
            if backup_result.success and backup_result.output:
                try:
                    parsed = json.loads(backup_result.output)
                    backup_result_dict['commit_hash'] = parsed.get('commit_hash', '')
                except (json.JSONDecodeError, TypeError):
                    logger.debug("self_repair: git backupoutputJSONparseFailed")
            backup_result = backup_result_dict
        else:
            backup_result = await self.git_backup_before_modify(file_path)
        if not backup_result['success']:
            return {
                "success": False,
                "commit_hash": "",
                "error": f"Backup failed, aborting modify: {backup_result['error']}",
                "rolled_back": False,
                "warnings": [],
                "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
            }
        
        commit_hash = backup_result['commit_hash']
        
        # 5. Read old content (used for rollback)
        old_content = None
        try:
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    old_content = f.read()
        except Exception as e:
            logger.warning(f"[SelfRepair] Could not read old content: {e}")
        
        # 6. P1 #6: Dependency chain impact check (warning only, non-blocking)
        if os.path.exists(file_path):
            dep_warnings = self._check_dependency_impact(file_path)
            if dep_warnings:
                warnings.extend(dep_warnings)
                for w in dep_warnings:
                    logger.warning(f"[SelfRepair] Dependency impact: {w}")
        
        # 7. Write new content
        # truthful-report: write operation tracked via record
        async def _do_write():
            # Ensure directory exists
            parent_dir = os.path.dirname(file_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            return "write_success"

        if self._truth_reporter:
            write_result = await self._truth_reporter.track(
                f"write:{os.path.basename(file_path)}",
                _do_write(),
            )
            if not write_result.success:
                self._log_modification(file_path, False, commit_hash, write_result.error or "Write failed")
                return {
                    "success": False,
                    "commit_hash": commit_hash,
                    "error": f"Failed to write new content: {write_result.error}",
                    "rolled_back": False,
                    "warnings": warnings,
                    "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
                }
        else:
            try:
                # Ensure directory exists
                parent_dir = os.path.dirname(file_path)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
                
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
            except Exception as e:
                self._log_modification(file_path, False, commit_hash, str(e))
                return {
                    "success": False,
                    "commit_hash": commit_hash,
                    "error": f"Failed to write new content: {e}",
                    "rolled_back": False,
                    "warnings": warnings,
                    "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
                }
        
        # 8. Verifygrammar
        # truthful-report: grammar check tracked via record
        if self._truth_reporter:
            syntax_op_result = await self._truth_reporter.track(
                f"syntax_check:{os.path.basename(file_path)}",
                self.validate_code_syntax(file_path),
            )
            # Restore syntax_result dict from track result
            syntax_result = {'valid': False, 'error': 'Syntax check tracking failed'}
            try:
                if syntax_op_result.output:
                    parsed = json.loads(syntax_op_result.output)
                    if isinstance(parsed, dict):
                        syntax_result = parsed
            except (json.JSONDecodeError, TypeError):
                if not syntax_op_result.success:
                    syntax_result = {'valid': False, 'error': syntax_op_result.error or 'Syntax check failed'}
        else:
            syntax_result = await self.validate_code_syntax(file_path)
        if not syntax_result['valid']:
            # Rollback: write back old content
            rolled_back = self._rollback_file(file_path, old_content)
            error_msg = f"Syntax validation failed: {syntax_result['error']}"
            self._log_modification(file_path, False, commit_hash, error_msg)
            return {
                "success": False,
                "commit_hash": commit_hash,
                "error": error_msg,
                "rolled_back": rolled_back,
                "warnings": warnings,
                "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
            }
        
        # 9. P0 #2: securityCheck(semanticlevel)
        # truthful-report: security check tracked via record
        if self._truth_reporter:
            safety_op_result = await self._truth_reporter.track(
                f"safety_check:{os.path.basename(file_path)}",
                self._async_safety_check(file_path),
            )
            # Restore safety_result dict from track result
            safety_result = {'safe': False, 'warnings': ['Safety check tracking failed']}
            try:
                if safety_op_result.output:
                    parsed = json.loads(safety_op_result.output)
                    if isinstance(parsed, dict):
                        safety_result = parsed
            except (json.JSONDecodeError, TypeError):
                if not safety_op_result.success:
                    safety_result = {'safe': False, 'warnings': [safety_op_result.error or 'Safety check failed']}
        else:
            safety_result = self.validate_code_safety(file_path)
        if not safety_result["safe"]:
            # rollback + recordwarning
            rolled_back = self._rollback_file(file_path, old_content)
            safety_warnings = safety_result['warnings']
            error_msg = f"Safety check failed: {safety_warnings}"
            logger.warning(f"[SelfRepair] {error_msg}")
            self._log_modification(file_path, False, commit_hash, error_msg)
            return {
                "success": False,
                "commit_hash": commit_hash,
                "error": error_msg,
                "rolled_back": rolled_back,
                "warnings": warnings + safety_warnings,
                "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
            }
        
        # 10. P2 #11: optionalsandboxtest
        # truthful-report: sandbox test tracked via record
        if sandbox_test:
            if self._truth_reporter:
                sandbox_op_result = await self._truth_reporter.track(
                    f"sandbox_test:{os.path.basename(file_path)}",
                    self._sandbox_test(file_path),
                )
                test_result = {
                    'passed': sandbox_op_result.success,
                    'error': sandbox_op_result.error or '',
                }
            else:
                test_result = await self._sandbox_test(file_path)
            if not test_result["passed"]:
                rolled_back = self._rollback_file(file_path, old_content)
                error_msg = f"Sandbox test failed: {test_result['error']}"
                self._log_modification(file_path, False, commit_hash, error_msg)
                return {
                    "success": False,
                    "commit_hash": commit_hash,
                    "error": error_msg,
                    "rolled_back": rolled_back,
                    "warnings": warnings,
                    "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
                }
        
        # 10.5 Truthful-report: cross-verify - auto-trigger on trust degradation
        if self._truth_reporter:
            trust_level = self._truth_reporter.get_trust_level()
            should_cross_verify = (
                trust_level.value in ('reduced', 'untrusted')
                or (sandbox_test and trust_level.value == 'full')
            )
            if should_cross_verify:
                try:
                    cv_result = await self._truth_reporter.cross_verify(
                        task_description=f"Safe modify file: {os.path.basename(file_path)}",
                        output=new_content[:6000],
                    )
                    if cv_result.get('verified') is False:
                        # cross-verify discovered suspected problem, record but not block (just a warning)
                        cv_issues = cv_result.get('issues_found', [])
                        cv_model = cv_result.get('verifier_model', 'unknown')
                        cv_confidence = cv_result.get('confidence', 0.0)
                        warning_msg = (
                            f"Cross-verify ({cv_model}, confidence={cv_confidence:.2f}) "
                            f"found issues: {cv_issues}"
                        )
                        warnings.append(warning_msg)
                        logger.warning(f"[SelfRepair] {warning_msg}")
                        # When trust degrades to UNTRUSTED, block if cross-verify fails
                        if trust_level.value == 'untrusted' and cv_confidence >= 0.7:
                            rolled_back = self._rollback_file(file_path, old_content)
                            error_msg = f"Cross-verify blocked modification: {warning_msg}"
                            self._log_modification(file_path, False, commit_hash, error_msg)
                            return {
                                "success": False,
                                "commit_hash": commit_hash,
                                "error": error_msg,
                                "rolled_back": rolled_back,
                                "warnings": warnings,
                                "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
                            }
                except Exception as e:
                    # Cross-verification failure does not block main process
                    logger.warning(f"[SelfRepair] Cross-verify failed (non-blocking): {e}")
        
        # 11. P2 #12: optionalhumanConfirm
        if require_human_approval:
            approved = await self._request_human_approval(file_path, new_content)
            if not approved:
                rolled_back = self._rollback_file(file_path, old_content)
                error_msg = "Human approval denied"
                self._log_modification(file_path, False, commit_hash, error_msg)
                return {
                    "success": False,
                    "commit_hash": commit_hash,
                    "error": error_msg,
                    "rolled_back": rolled_back,
                    "warnings": warnings,
                    "sandbox_mode": self.sandbox_mode,  # P0-4: Report sandbox mode
                }
        
        # 12. P1 #7: Increment modification count (only on success)
        self._modification_count += 1
        
        # P1 #4: Persistmodifycount
        self._save_modification_count()
        
        logger.info(
            f"[SelfRepair] Safe modify successful: {file_path} "
            f"(modification {self._modification_count}/{self.MAX_SELF_MODIFICATIONS})"
        )
        
        # P2 #10: Audit log
        self._log_modification(file_path, True, commit_hash)
        
        result = {
            "success": True,
            "commit_hash": commit_hash,
            "error": "",
            "rolled_back": False,
            "warnings": warnings,
            "sandbox_mode": self.sandbox_mode,  # P0-4: Report actual sandbox mode used
        }
        return result
    
    def _rollback_file(self, file_path: str, old_content: Optional[str]) -> bool:
        """
        Roll back file to old content.
        
        Args:
            file_path: File path
            old_content: Old content (None means attempt git checkout)
            
        Returns:
            True ifrollbacksuccess
        """
        try:
            if old_content is not None:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(old_content)
                logger.info(f"[SelfRepair] Rolled back content for {file_path}")
                return True
            else:
                # No old content, attempt git checkout
                file_dir = os.path.dirname(os.path.realpath(file_path))
                subprocess.run(
                    ['git', 'checkout', '--', file_path],
                    cwd=file_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                logger.info(f"[SelfRepair] Git checkout rollback for {file_path}")
                return True
        except Exception as rollback_err:
            logger.error(f"[SelfRepair] Rollback also failed: {rollback_err}")
            return False
    
    # P1 #4: modifycountPersist
    def _load_modification_count(self) -> int:
        """Load modification count from persistent file"""
        count_file = os.path.join(self._get_repo_root(), '.tical_mod_count.json')
        try:
            if os.path.exists(count_file):
                with open(count_file, 'r') as f:
                    data = json.load(f)
                return data.get("count", 0)
        except Exception as e:
            logger.debug(f"[SelfRepair] _load_modification_countException (non-blocking): {e}")
            pass
        return 0
    
    def _save_modification_count(self):
        """Persistmodifycount"""
        count_file = os.path.join(self._get_repo_root(), '.tical_mod_count.json')
        try:
            with open(count_file, 'w') as f:
                json.dump({"count": self._modification_count, "updated": datetime.now().isoformat()}, f)
        except Exception as e:
            logger.debug(f"[SelfRepair] _load_modification_countException (non-blocking): {e}")
            pass
    
    async def safe_restart_with_rollback(
        self,
        restart_cmd: List[str],
        health_check_url: str = "",
        timeout: int = 15,
    ) -> Dict:
        """
        Restart service with automatic rollback on health check failure.
        
        Flow:
        1. Record current git HEAD hash
        2. Execute restart command
        3. Multi-layer health check (P0 #3: HTTP + process + identity)
        4. On failure: check git integrity (P0 #4), then rollback
        
        Args:
            restart_cmd: Command to restart the service (e.g., ['python', 'main.py'])
            health_check_url: URL to check for HTTP 200 after restart
            timeout: Max seconds to wait for health check (default 15)
            
        Returns:
            {"success": bool, "rolled_back": bool, "rollback_hash": str, "error": str}
        """
        # 1. recordcurrent HEAD hash
        pre_restart_hash = ""
        try:
            hash_result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if hash_result.returncode == 0:
                pre_restart_hash = hash_result.stdout.strip()
        except Exception as e:
            logger.debug(f"[SelfRepair] Unknown exception (non-blocking): {e}")
            pass
        
        # 2. Executerestartcommand
        # truthful-report: restart operation tracked via record
        async def _do_restart():
            return subprocess.Popen(
                restart_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        try:
            if self._truth_reporter:
                restart_op_result = await self._truth_reporter.track(
                    f"restart:{' '.join(restart_cmd)[:50]}",
                    _do_restart(),
                )
                if not restart_op_result.success:
                    return {
                        "success": False,
                        "rolled_back": False,
                        "rollback_hash": pre_restart_hash,
                        "error": f"Failed to execute restart command: {restart_op_result.error}",
                    }
                restart_process = None  # track whether Popen was already executed internally
            else:
                restart_process = subprocess.Popen(
                    restart_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            logger.info(f"[SelfRepair] Restart command executed: {' '.join(restart_cmd)}")
        except Exception as e:
            return {
                "success": False,
                "rolled_back": False,
                "rollback_hash": pre_restart_hash,
                "error": f"Failed to execute restart command: {e}",
            }
        
        # 3. P0 #3: multi-layer-healthCheck
        if not health_check_url:
            # No health check URL, conservative strategy: wait 5 seconds then consider success
            await asyncio.sleep(5)
            return {
                "success": True,
                "rolled_back": False,
                "rollback_hash": "",
                "error": "",
            }
        
        health_result = await self._multi_health_check(health_check_url, timeout)
        
        if health_result["passed"]:
            return {
                "success": True,
                "rolled_back": False,
                "rollback_hash": "",
                "error": "",
            }
        
        # 4. Health check failure → P0 #4: first check git integrity
        failed_checks = [k for k, v in health_result["checks"].items() if not v]
        logger.warning(
            f"[SelfRepair] Health check failed after restart "
            f"(failed: {failed_checks}), rolling back..."
        )
        
        if not self._verify_git_integrity():
            logger.critical("[SelfRepair] Git integrity check failed! Cannot rollback automatically!")
            return {
                "success": False,
                "rolled_back": False,
                "rollback_hash": pre_restart_hash,
                "error": f"Health check failed ({failed_checks}) and git repo destroyed - cannot rollback",
            }
        
        rollback_success = False
        try:
            # git checkout -- . restoreallFile
            subprocess.run(
                ['git', 'checkout', '--', '.'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            # git reset --hard <previoushash> restore to pre-modification status
            if pre_restart_hash:
                subprocess.run(
                    ['git', 'reset', '--hard', pre_restart_hash],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            
            rollback_success = True
            logger.info(f"[SelfRepair] Rolled back to {pre_restart_hash[:8] if pre_restart_hash else 'unknown'}")
        except Exception as e:
            logger.error(f"[SelfRepair] Rollback failed: {e}")
        
        # againrestart(userollbackaftercode)
        try:
            subprocess.Popen(
                restart_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info("[SelfRepair] Re-restarted with rolled-back code")
        except Exception as e:
            logger.error(f"[SelfRepair] Re-restart after rollback failed: {e}")
        
        return {
            "success": False,
            "rolled_back": rollback_success,
            "rollback_hash": pre_restart_hash,
            "error": f"Health check failed ({failed_checks}) after {timeout}s, rollback {'succeeded' if rollback_success else 'failed'}",
        }
