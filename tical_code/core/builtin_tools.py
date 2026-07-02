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

# provenance:ticalasi-zzt-2026​
"""
⚠️ LEGACY MODULE - DO NOT USE FOR NEW CODE ⚠️

This module is the async tool execution system, superseded by tool_executor.py.
- tool_executor.py is the canonical tool system used by unified_worker.py
- builtin_tools.py is only referenced by worker_framework.py (also unconnected)
- Unique features (RateLimiter, http_post) have been migrated to tool_executor.py

This file is preserved for reference. Do not add new functionality here.
"""



import asyncio
import fnmatch
import glob
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import time
from collections import deque
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ToolResult for handler return types
try:
    from tical_code.plugins import ToolResult
except ImportError:
    @dataclass
    class ToolResult:
        """Result from a tool execution."""
        success: bool
        data: Any = None
        error: Optional[str] = None
        verified: bool = False
        elapsed_ms: float = 0.0

        def to_dict(self) -> Dict:
            return {
                'success': self.success,
                'data': self.data,
                'error': self.error,
                'verified': self.verified,
                'elapsed_ms': self.elapsed_ms,
            }

# ToolDefinition, VerifyLevel and SandboxLevel
try:
    from .tool_registry import ToolDefinition, VerifyLevel, SandboxLevel
except ImportError:
    class VerifyLevel:
        NONE = 0
        BASIC = 1
        SCHEMA = 2
        DUAL = 3
        HUMAN = 4
        IDENTITY = 5
    
    class SandboxLevel:
        NONE = "none"
        RECOMMENDED = "recommended"
        REQUIRED = "required"
    
    @dataclass
    class ToolDefinition:
        name: str
        description: str
        params: Dict[str, Any]
        handler: Any
        verify_level: VerifyLevel = VerifyLevel.SCHEMA
        timeout: int = 30
        edition: str = "lite"
        requires_confirmation: bool = False
        allowed_roles: List[str] = field(default_factory=lambda: ["all"])
        sandbox_level: str = SandboxLevel.NONE

# =============================================================================
# v3 DoD Security Constants
# =============================================================================

# Output truncation limit (100KB for v3 DoD)
MAX_OUTPUT_LENGTH = 100_000  # 100KB - v3 DoD requirement
MAX_ERROR_LENGTH = 2000

# System directories that are always blocked (for filesystem isolation)
SYSTEM_DIRS_BLOCKED = {
    '/etc', '/usr', '/bin', '/sbin', '/lib', '/lib64',
    '/root/.ssh', '/home/*/.ssh', '/.ssh',
    '/proc', '/sys', '/dev',
    '/var', '/boot', '/opt',
}

# Private IP ranges for SSRF protection
SSRF_BLOCKED_PATTERNS = [
    r'^127\.',           # 127.x.x.x (loopback)
    r'^10\.',            # 10.x.x.x (private)
    r'^172\.(1[6-9]|2[0-9]|3[0-1])\.',  # 172.16-31.x.x (private)
    r'^192\.168\.',      # 192.168.x.x (private)
    r'^169\.254\.',      # 169.254.x.x (link-local)
    r'^0\.',             # 0.x.x.x
    r'^::1$',            # IPv6 loopback
    r'^fe80:',           # IPv6 link-local
]

# Rate limiting configuration
RATE_LIMIT_WINDOW = 1.0  # 1 second window
RATE_LIMIT_MAX_CALLS = 10  # v0.6: 5→10  # max 5 calls per window


# =============================================================================
# Security Context (per-worker isolation)
# =============================================================================

@dataclass
class SecurityContext:
    """
    Security context for sandbox isolation.
    
    Each worker has its own security context that restricts:
    - File system access (allowed_dirs)
    - Network access (allowed_domains)
    """
    allowed_dirs: List[str] = field(default_factory=lambda: ["~"])
    allowed_domains: List[str] = field(default_factory=list)  # Empty = allow all
    
    def is_dir_allowed(self, path: str) -> bool:
        """Check if a directory path is allowed."""
        # Expand user home
        resolved = os.path.realpath(os.path.expanduser(path))
        
        for allowed in self.allowed_dirs:
            allowed_resolved = os.path.realpath(os.path.expanduser(allowed))
            if resolved.startswith(allowed_resolved + os.sep) or resolved == allowed_resolved:
                return True
        
        # Check system directories
        for sys_dir in SYSTEM_DIRS_BLOCKED:
            if resolved.startswith(sys_dir):
                return False
        
        return False
    
    def is_domain_allowed(self, domain: str) -> bool:
        """Check if a domain is allowed (whitelist)."""
        # Empty whitelist means allow all
        if not self.allowed_domains:
            return True
        
        domain_lower = domain.lower()
        for allowed in self.allowed_domains:
            allowed_lower = allowed.lower()
            if domain_lower == allowed_lower or domain_lower.endswith('.' + allowed_lower):
                return True
        
        return False
    
    def is_ip_private(self, host: str) -> bool:
        """Check if host is a private/internal IP (SSRF check)."""
        for pattern in SSRF_BLOCKED_PATTERNS:
            if re.match(pattern, host):
                return True
        return False


# Global security context (can be set per worker)
_security_context: SecurityContext = SecurityContext()
_cron_manager = None  # Set by set_cron_manager() from unified_worker
_memory_store = None  # Set by set_memory_store() from unified_worker


def set_security_context(ctx: SecurityContext):
    """Set the global security context."""
    global _security_context
    _security_context = ctx


def get_security_context() -> SecurityContext:
    """Get the current security context."""
    return _security_context


def set_cron_manager(manager) -> None:
    """Set the global cron manager (injected by unified_worker at startup)."""
    global _cron_manager
    _cron_manager = manager


def set_memory_store(store) -> None:
    """Set the global memory store (injected by unified_worker at startup)."""
    global _memory_store
    _memory_store = store


# =============================================================================
# URL Helpers (for http_get/http_post/extract_text)
# =============================================================================

# URL parsing helper
def _parse_url(url: str):
    """Parse URL and return parsed result."""
    try:
        from urllib.parse import urlparse
        return urlparse(url)
    except Exception as e:
        logger.debug(f"[URLparse] parseFailed: {e}")
        return None


# SSRF protection - check if URL is allowed
SSRF_BLOCKED_RANGES = [
    '127.', '0.', '10.', '192.168.', '172.16.', '172.17.', '172.18.', '172.19.',
    '172.20.', '172.21.', '172.22.', '172.23.', '172.24.', '172.25.', '172.26.',
    '172.27.', '172.28.', '172.29.', '172.30.', '172.31.', '169.254.', '::1',
    'fe80:', 'fc00:', 'fd00:'
]

# Allowed domains whitelist (empty = allow all except blocked)
ALLOWED_DOMAINS = set(os.environ.get('TICAL_ALLOWED_DOMAINS', '').split(',')) - {'', 'none'}


def _is_url_allowed(url: str) -> bool:
    """
    Check if URL is allowed (SSRF protection).
    
    Blocks:
    - Private IP ranges (127.x, 10.x, 192.168.x, etc.)
    - Localhost
    - Link-local addresses
    
    Args:
        url: URL to check
        
    Returns:
        True if allowed, False if blocked
    """
    parsed = _parse_url(url)
    if not parsed:
        return False
    
    # Check scheme
    if parsed.scheme not in ('http', 'https'):
        return False
    
    hostname = parsed.hostname.lower() or ''
    
    # Block localhost
    if hostname in ('localhost', 'localhost.localdomain'):
        return False
    
    # Block private IPs
    for blocked in SSRF_BLOCKED_RANGES:
        if hostname.startswith(blocked):
            return False

    # DNS resolution check: resolve hostname to catch private IPs behind domain names
    try:
        resolved = socket.gethostbyname(hostname)
        if ipaddress.ip_address(resolved).is_private:
            return False
    except Exception:
        pass

    # Check against allowed domains whitelist if configured
    if ALLOWED_DOMAINS:
        return hostname in ALLOWED_DOMAINS
    
    return True


# =============================================================================
# Rate Limiter
# =============================================================================

class RateLimiter:
    """
    Sliding window rate limiter for tool calls.
    
    v3 DoD requirement: max 5 calls per second per worker.
    Zero dependencies - uses time.time() + deque.
    """
    
    def __init__(self, max_calls: int = RATE_LIMIT_MAX_CALLS, window: float = RATE_LIMIT_WINDOW):
        self.max_calls = max_calls
        self.window = window
        self._calls: deque = deque()
        self._lock = asyncio.Lock()
    
    async def check(self) -> bool:
        """
        Check if a call is allowed.
        
        Returns:
            True if allowed, False if rate limited
        """
        async with self._lock:
            now = time.time()
            
            # Remove expired entries
            while self._calls and self._calls[0] < now - self.window:
                self._calls.popleft()
            
            # Check if under limit
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return True
            
            return False
    
    def check_sync(self) -> bool:
        """Synchronous check for non-async contexts.
        
        v0.5.6: use threading.Lock protectconcurrencyaccess,preventrace-conditionCondition.
        """
        import threading
        if not hasattr(self, '_sync_lock'):
            self._sync_lock = threading.Lock()
        
        with self._sync_lock:
            now = time.time()
            
            # Remove expired entries
            while self._calls and self._calls[0] < now - self.window:
                self._calls.popleft()
            
            # Check if under limit
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return True
            
            return False


# Global rate limiter
_rate_limiter = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter."""
    return _rate_limiter


def set_rate_limiter(limiter: RateLimiter):
    """Set a custom rate limiter."""
    global _rate_limiter
    _rate_limiter = limiter


# Working directory restriction (None = no restriction)
WORK_DIR = os.environ.get('TICAL_WORK_DIR', '/tmp/tical-work')


# =============================================================================
# Shell Command Whitelist
# =============================================================================

# Commands allowed in shell_exec (security whitelist)
SHELL_ALLOWED_COMMANDS = {
    'ls', 'cat', 'pwd', 'git', 'grep', 'find', 'wc', 'head', 'tail',
    'df', 'du', 'ps', 'top', 'echo', 'date', 'whoami', 'hostname', 'uname',
    'mkdir', 'cp', 'mv', 'sort', 'uniq', 'awk', 'sed', 'cut', 'tr',
    'cd', 'env', 'printenv', 'id', 'uptime', 'free', 'netstat', 'ss',
    'python3', 'pytest', 'pip3', 'touch', 'diff', 'curl', 'wget',
    'msmtp', 'docker', 'systemctl', 'journalctl', 'npm', 'node',
}

# Dangerous patterns that will be blocked
SHELL_BLOCKED_PATTERNS = [
    r'rm\s+-rf\s+',           # Recursive delete
    r'rm\s+-rf\s+/',          # Delete root
    r'mkfs',                   # Filesystem format
    r'dd\s+.*of=/',           # Direct write to device
    r'chmod\s+777',           # Full permissions
    r'chmod\s+-R\s+777',      # Recursive full permissions
    r'>\s*/etc/',             # Write to system config
    r'>\s*/var/',             # Write to system var
    r'>\s*/bin/',             # Write to system bin
    r'>\s*/usr/',             # Write to system usr
    r'pip\s+install',         # Package install
    r'pip3\s+install',        # Package install
    r'curl\s*\|\s*sh',        # Pipe to shell
    r'wget\s*\|\s*sh',        # Pipe to shell
    r';\s*sh\s*$',            # End with shell
    r'`.*sh.*`',              # Command substitution to shell
    r'\$\(.*sh.*\)',          # Command substitution to shell
    r'sudo\s+su',             # Privilege escalation
    r':\(\)\{',               # Fork bomb
]


# =============================================================================
# Tool Definitions
# =============================================================================

# JSON Schema for read_file
READ_FILE_PARAMS = {
    'type': 'object',
    'properties': {
        'path': {
            'type': 'string',
            'description': 'File path to read (parameter name is "path")'
        },
        'offset': {
            'type': 'integer',
            'description': 'Line offset to start reading (0-indexed)',
            'minimum': 0
        },
        'limit': {
            'type': 'integer',
            'description': 'Maximum number of lines to read',
            'minimum': 1,
            'default': 100
        }
    },
    'required': ['path']
}

# JSON Schema for write_file
WRITE_FILE_PARAMS = {
    'type': 'object',
    'properties': {
        'path': {
            'type': 'string',
            'description': 'File path to write (parameter name is "path")'
        },
        'content': {
            'type': 'string',
            'description': 'Content to write to file'
        },
        'append': {
            'type': 'boolean',
            'description': 'Append to file instead of overwriting',
            'default': False
        }
    },
    'required': ['path', 'content']
}

# JSON Schema for list_dir
LIST_DIR_PARAMS = {
    'type': 'object',
    'properties': {
        'path': {
            'type': 'string',
            'description': 'Directory path to list (parameter name is "path")',
            'default': '.'
        },
        'all': {
            'type': 'boolean',
            'description': 'Include hidden files (starting with .)',
            'default': False
        }
    },
    'required': []
}

# JSON Schema for shell_exec
SHELL_EXEC_PARAMS = {
    'type': 'object',
    'properties': {
        'cmd': {
            'type': 'string',
            'description': 'Shell command to execute (parameter name is "cmd", NOT "command")'
        },
        'timeout': {
            'type': 'integer',
            'description': 'Timeout in seconds',
            'minimum': 1,
            'maximum': 60,
            'default': 10
        }
    },
    'required': ['cmd']
}

# JSON Schema for http_get
HTTP_GET_PARAMS = {
    'type': 'object',
    'properties': {
        'url': {
            'type': 'string',
            'description': 'URL to fetch via GET'
        },
        'timeout': {
            'type': 'integer',
            'description': 'Timeout in seconds',
            'minimum': 1,
            'maximum': 30,
            'default': 10
        }
    },
    'required': ['url']
}

# JSON Schema for http_post
HTTP_POST_PARAMS = {
    'type': 'object',
    'properties': {
        'url': {
            'type': 'string',
            'description': 'URL to fetch via POST'
        },
        'data': {
            'type': 'string',
            'description': 'POST body data'
        },
        'headers': {
            'type': 'object',
            'description': 'HTTP headers',
            'default': {}
        },
        'timeout': {
            'type': 'integer',
            'description': 'Timeout in seconds',
            'minimum': 1,
            'maximum': 30,
            'default': 10
        }
    },
    'required': ['url']
}

# JSON Schema for search_files
SEARCH_FILES_PARAMS = {
    'type': 'object',
    'properties': {
        'pattern': {
            'type': 'string',
            'description': 'File name glob pattern (parameter name is "pattern", NOT "file_pattern". e.g. *.py, *worker*)'
        },
        'directory': {
            'type': 'string',
            'description': 'Directory to search in',
            'default': '.'
        },
        'content_pattern': {
            'type': 'string',
            'description': 'Text pattern to search inside files'
        }
    },
    'required': ['pattern']
}


# =============================================================================
# Tool Handlers
# =============================================================================

def read_file_handler(path: str, offset: int = 0, limit: int = 100) -> Dict[str, Any]:
    """
    Read file content with line numbers.

    Args:
        path: File path to read
        offset: Line offset to start (0-indexed)
        limit: Maximum lines to read

    Returns:
        Dict with success, content (with line numbers), and metadata
    """
    # Rate limit check
    if not _rate_limiter.check_sync():
        return {'success': False, 'error': 'rate limit exceeded'}
    
    try:
        # Security: check allowed_dirs
        if not _security_context.is_dir_allowed(path):
            return {'success': False, 'error': f'Path access denied: {path}'}

        if not os.path.exists(path):
            return {'success': False, 'error': f'File not found: {path}'}

        if not os.path.isfile(path):
            return {'success': False, 'error': f'Not a file: {path}'}

        # Read with line numbers
        lines = []
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for i, line in enumerate(f):
                if i >= offset:
                    lines.append(f'{i + 1}: {line.rstrip()}')
                if len(lines) >= limit:
                    break

        total_lines = sum(1 for _ in open(path, 'r', encoding='utf-8', errors='replace'))

        result = {
            'success': True,
            'path': path,
            'lines': lines,
            'count': len(lines),
            'offset': offset,
            'total_lines': total_lines,
            'has_more': offset + len(lines) < total_lines
        }
        # v0.5.9: truncate over-long result to prevent context explosion
        return _truncate_tool_result(result)
    except PermissionError:
        return {'success': False, 'error': f'Permission denied: {path}'}
    except Exception as e:
        return {'success': False, 'error': _truncate_error(str(e))}


def write_file_handler(path: str, content: str, append: bool = False) -> Dict[str, Any]:
    """
    Safely write content to a file.

    v0.13: post-write auto lint + rollback on failure.

    Args:
        path: File path to write
        content: Content to write
        append: Append mode instead of overwrite

    Returns:
        Dict with success and metadata
    """
    # Rate limit check
    if not _rate_limiter.check_sync():
        return {'success': False, 'error': 'rate limit exceeded'}
    
    try:
        # Security: check allowed_dirs
        if not _security_context.is_dir_allowed(path):
            return {'success': False, 'error': f'Path access denied: {path}'}

        # v0.13: use write process with lint
        # write_with_lint removed: _legacy module deleted

        # append mode currently does not lint (content is appended, not a complete file)
        if append:
            # create parent directory
            parent_dir = os.path.dirname(path)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(content)
            stat = os.stat(path)
            return {
                'success': True,
                'path': path,
                'bytes_written': len(content.encode('utf-8')),
                'size_bytes': stat.st_size,
                'mode': 'append',
            }

        # non-append mode
        parent_dir = os.path.dirname(path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        stat = os.stat(path)
        return {
            'success': True,
            'path': path,
            'bytes_written': len(content.encode('utf-8')),
            'size_bytes': stat.st_size,
            'mode': 'overwrite',
        }
    except PermissionError:
        return {'success': False, 'error': f'Permission denied: {path}'}
    except Exception as e:
        return {'success': False, 'error': _truncate_error(str(e))}


def list_dir_handler(path: str = '.', all: bool = False) -> Dict[str, Any]:
    """
    List directory contents.

    Args:
        path: Directory to list
        all: Include hidden files

    Returns:
        Dict with success, files list, and metadata
    """
    # Rate limit check
    if not _rate_limiter.check_sync():
        return {'success': False, 'error': 'rate limit exceeded'}
    
    try:
        # Security: check allowed_dirs
        if not _security_context.is_dir_allowed(path):
            return {'success': False, 'error': f'Path access denied: {path}'}

        full_path = os.path.realpath(os.path.expanduser(path))
        if not os.path.exists(full_path):
            return {'success': False, 'error': f'Directory not found: {path}'}

        if not os.path.isdir(full_path):
            return {'success': False, 'error': f'Not a directory: {path}'}

        entries = []
        for entry in os.listdir(full_path):
            if not all and entry.startswith('.'):
                continue

            full_entry = os.path.join(full_path, entry)
            try:
                stat = os.stat(full_entry)
                entry_type = 'dir' if os.path.isdir(full_entry) else 'file'
                entries.append({
                    'name': entry,
                    'type': entry_type,
                    'size': stat.st_size,
                    'modified': stat.st_mtime
                })
            except PermissionError:
                entries.append({'name': entry, 'type': 'unknown', 'error': 'permission denied'})

        # Sort: directories first, then files, alphabetically
        entries.sort(key=lambda x: (x.get('type') != 'dir', x.get('name', '')))

        return {
            'success': True,
            'path': full_path,
            'entries': entries,
            'count': len(entries)
        }
    except PermissionError:
        return {'success': False, 'error': f'Permission denied: {path}'}
    except Exception as e:
        return {'success': False, 'error': _truncate_error(str(e))}


def shell_exec_handler(cmd: str, timeout: int = 30) -> Dict[str, Any]:
    """
    Execute a shell command with security whitelist.

    Security measures:
    - Command must start with an allowed command
    - Dangerous patterns are blocked
    - Timeout prevents infinite loops
    - Output truncated to 100KB (v3 DoD)

    Args:
        cmd: Command to execute
        timeout: Timeout in seconds

    Returns:
        Dict with success, stdout, stderr, exit_code
    """
    # Rate limit check
    if not _rate_limiter.check_sync():
        return {
            'success': False,
            'error': 'rate limit exceeded',
            'stdout': '',
            'stderr': '',
            'exit_code': -1
        }
    
    try:
        # Validate command
        validation = _validate_shell_command(cmd)
        if not validation['valid']:
            return {
                'success': False,
                'error': f'Command blocked: {validation["reason"]}',
                'stdout': '',
                'stderr': '',
                'exit_code': -1
            }

        # Execute with timeout - shlex.split, never shell=True
        import shlex as _shlex
        try:
            args = _shlex.split(cmd)
        except ValueError:
            return {
                'success': False,
                'error': 'Malformed command (unmatched quotes)',
                'stdout': '',
                'stderr': '',
                'exit_code': -1
            }
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=WORK_DIR if WORK_DIR else None
        )

        return _truncate_tool_result({
            'success': result.returncode == 0,
            'stdout': _truncate_output(result.stdout),  # 100KB truncation (v3 DoD)
            'stderr': _truncate_output(result.stderr),    # 100KB truncation (v3 DoD)
            'exit_code': result.returncode,
            'command': cmd,
            'timeout': timeout
        })
    except subprocess.TimeoutExpired:
        return {
            'success': False,
            'error': f'Command timed out after {timeout}s',
            'stdout': '',
            'stderr': '',
            'exit_code': -1
        }
    except Exception as e:
        return {
            'success': False,
            'error': _truncate_error(str(e)),
            'stdout': '',
            'stderr': '',
            'exit_code': -1
        }


def http_get_handler(url: str, timeout: int = 10) -> Dict[str, Any]:
    """
    Perform HTTP GET request with SSRF protection and domain whitelist.

    v3 DoD Security:
    - Domain whitelist check (allowed_domains)
    - SSRF protection (blocks private IP ranges)
    - Output truncated to 100KB

    Args:
        url: URL to fetch
        timeout: Timeout in seconds

    Returns:
        Dict with success, status, content, headers
    """
    # Rate limit check
    if not _rate_limiter.check_sync():
        return {'success': False, 'error': 'rate limit exceeded'}
    
    try:
        import urllib.request
        import urllib.error
        from urllib.parse import urlparse

        # Validate URL
        if not url.startswith(('http://', 'https://')):
            return {'success': False, 'error': 'Invalid URL scheme (must be http or https)'}

        # Parse URL to get domain
        parsed = urlparse(url)
        host = parsed.netloc.split(':')[0]  # Remove port if present

        # SSRF check: block private IPs
        if _security_context.is_ip_private(host):
            return {'success': False, 'error': f'SSRF blocked: private IP range ({host})'}

        # Domain whitelist check
        if not _security_context.is_domain_allowed(host):
            return {'success': False, 'error': f'Domain not in whitelist: {host}'}

        # Make request
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'eite-agent/0.1.0'}
        )

        with urllib.request.urlopen(req, timeout=timeout) as response:
            content = response.read().decode('utf-8', errors='replace')
            # v3 DoD: truncate to 100KB
            if len(content) > MAX_OUTPUT_LENGTH:
                content = content[:MAX_OUTPUT_LENGTH] + f'\n... [truncated, total {len(content)} bytes]'

            return {
                'success': True,
                'status': response.status,
                'url': response.url,
                'content': content,
                'headers': dict(response.headers),
                'content_length': len(content)
            }

    except urllib.error.HTTPError as e:
        return {
            'success': False,
            'error': f'HTTP {e.code}: {e.reason}',
            'status': e.code
        }
    except urllib.error.URLError as e:
        return {
            'success': False,
            'error': f'URL error: {e.reason}'
        }
    except Exception as e:
        return {'success': False, 'error': _truncate_error(str(e))}


def http_post_handler(
    url: str,
    data: str = "",
    headers: Dict[str, str] = None,
    timeout: int = 10
) -> Dict[str, Any]:
    """
    Perform HTTP POST request with SSRF protection and domain whitelist.

    v3 DoD Security:
    - Domain whitelist check (allowed_domains)
    - SSRF protection (blocks private IP ranges)
    - Output truncated to 100KB

    Args:
        url: URL to fetch
        data: POST body data
        headers: HTTP headers
        timeout: Timeout in seconds

    Returns:
        Dict with success, status, content, headers
    """
    # Rate limit check
    if not _rate_limiter.check_sync():
        return {'success': False, 'error': 'rate limit exceeded'}
    
    try:
        import urllib.request
        import urllib.error
        from urllib.parse import urlparse

        # Validate URL
        if not url.startswith(('http://', 'https://')):
            return {'success': False, 'error': 'Invalid URL scheme (must be http or https)'}

        # Parse URL to get domain
        parsed = urlparse(url)
        host = parsed.netloc.split(':')[0]

        # SSRF check: block private IPs
        if _security_context.is_ip_private(host):
            return {'success': False, 'error': f'SSRF blocked: private IP range ({host})'}

        # Domain whitelist check
        if not _security_context.is_domain_allowed(host):
            return {'success': False, 'error': f'Domain not in whitelist: {host}'}

        # Make request
        req_headers = {'User-Agent': 'eite-agent/0.1.0'}
        if headers:
            req_headers.update(headers)
        
        req = urllib.request.Request(
            url,
            data=data.encode('utf-8') if data else None,
            headers=req_headers,
            method='POST'
        )

        with urllib.request.urlopen(req, timeout=timeout) as response:
            content = response.read().decode('utf-8', errors='replace')
            # v3 DoD: truncate to 100KB
            if len(content) > MAX_OUTPUT_LENGTH:
                content = content[:MAX_OUTPUT_LENGTH] + f'\n... [truncated, total {len(content)} bytes]'

            return {
                'success': True,
                'status': response.status,
                'url': response.url,
                'content': content,
                'headers': dict(response.headers),
                'content_length': len(content)
            }

    except urllib.error.HTTPError as e:
        return {
            'success': False,
            'error': f'HTTP {e.code}: {e.reason}',
            'status': e.code
        }
    except urllib.error.URLError as e:
        return {
            'success': False,
            'error': f'URL error: {e.reason}'
        }
    except Exception as e:
        return {'success': False, 'error': _truncate_error(str(e))}


def search_files_handler(
    pattern: str,
    directory: str = '.',
    content_pattern: Optional[str] = None
) -> Dict[str, Any]:
    """
    Search for files by name pattern or content.

    Args:
        pattern: Glob pattern for file names (e.g., *.py)
        directory: Directory to search in
        content_pattern: Optional text to search inside files

    Returns:
        Dict with success, matched files list
    """
    # Rate limit check
    if not _rate_limiter.check_sync():
        return {'success': False, 'error': 'rate limit exceeded'}
    
    try:
        # Security: check allowed_dirs
        if not _security_context.is_dir_allowed(directory):
            return {'success': False, 'error': f'Path access denied: {directory}'}

        search_dir = os.path.realpath(os.path.expanduser(directory))
        if not os.path.exists(search_dir):
            return {'success': False, 'error': f'Directory not found: {directory}'}

        matches = []

        if content_pattern:
            # Search inside files
            for root, dirs, files in os.walk(search_dir):
                # Skip hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for filename in files:
                    if fnmatch.fnmatch(filename, pattern):
                        filepath = os.path.join(root, filename)
                        try:
                            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                                content = f.read()
                                if content_pattern in content:
                                    # Find line number
                                    for i, line in enumerate(f, 1):
                                        if content_pattern in line:
                                            matches.append({
                                                'path': filepath,
                                                'line': i,
                                                'context': line.strip()[:100]
                                            })
                                            break
                        except (PermissionError, IsADirectoryError):
                            pass
        else:
            # Search by name only
            for filepath in glob.glob(os.path.join(search_dir, '**', pattern), recursive=True):
                if len(matches) >= 1000:
                    break  # DoS protection: cap glob results
                if os.path.isfile(filepath):
                    stat = os.stat(filepath)
                    matches.append({
                        'path': filepath,
                        'size': stat.st_size,
                        'modified': stat.st_mtime
                    })

        return _truncate_tool_result({
            'success': True,
            'pattern': pattern,
            'directory': search_dir,
            'matches': matches,
            'count': len(matches)
        })
    except PermissionError:
        return {'success': False, 'error': f'Permission denied: {directory}'}
    except Exception as e:
        return {'success': False, 'error': _truncate_error(str(e))}


# =============================================================================
# Security Helpers
# =============================================================================

def _is_safe_path(path: str) -> bool:
    """Check if path is safe (within work directory)."""
    if WORK_DIR is None:
        return True

    try:
        resolved = os.path.realpath(path)
        work_resolved = os.path.realpath(WORK_DIR)
        return resolved.startswith(work_resolved)
    except Exception as e:
        logger.debug(f"[pathsecurity] securityCheck failure: {e}")
        return False


def _resolve_safe_path(path: str) -> Optional[str]:
    """Resolve path and check if safe."""
    if WORK_DIR is None:
        return os.path.realpath(path)

    try:
        resolved = os.path.realpath(os.path.join(WORK_DIR, path))
        work_resolved = os.path.realpath(WORK_DIR)
        if resolved.startswith(work_resolved):
            return resolved
        return None
    except Exception as e:
        logger.debug(f"[pathsecurity] pathparseFailed: {e}")
        return None


def _validate_shell_command(cmd: str) -> Dict[str, Any]:
    """
    Validate shell command against security rules.

    Returns:
        Dict with 'valid' (bool) and optional 'reason'
    """
    if not cmd or not cmd.strip():
        return {'valid': False, 'reason': 'Empty command'}

    # security: forbid shell operators, prevent command injection (e.g. "ls; rm -rf /")
    # v0.5.7: && allowed for command chains (e.g. git add && git commit), other operators still forbidden
    # note: ; still forbidden (can execute any subsequent command), || still forbidden (execute alternative on failure)
    # v0.5.7: operator Strategy - balances security and usability
    # ; → forbid (can execute any subsequent command)
    # || → forbid (execute alternative command on failure)
    # ` → forbid (command substitution)
    # & → only forbid independent & (background execution), 2>&1 redirects allowed
    # | → allow (pipeline)
    # && → allow (command chain)
    for op in [';', '||', '`']:
        if op in cmd:
            return {
                'valid': False,
                'reason': f'Shell operator forbidden: {repr(op)}'
            }
    # independent & check: match before/after as space/leading/trailing &, but not && or 2>&1
    if re.search(r'(?<!&)&(?!&|=|>)', cmd):
        # exclude redirect: 2>&1, 1>&2 etc.
        if not re.match(r'^[^&]*\d>&\d+$', cmd.strip()):
            return {
                'valid': False,
                'reason': 'Shell operator forbidden: background execution (&)'
            }

    # security: forbid $() command substitution
    if '$(' in cmd:
        return {
            'valid': False,
            'reason': 'Command substitution forbidden: $()'
        }

    # Strip leading bash comment lines (# comments confuse the whitelist)
    cmd_stripped = '\n'.join(
        l for l in cmd.strip().split('\n')
        if not l.lstrip().startswith('#')
    ).strip()
    # Get the first word (command)
    first_word = cmd_stripped.split()[0] if cmd_stripped.split() else ''

    # Check if command is in whitelist
    if first_word not in SHELL_ALLOWED_COMMANDS:
        return {'valid': False, 'reason': f"Command '{first_word}' not in whitelist"}

    # Check for dangerous patterns
    for pattern in SHELL_BLOCKED_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return {'valid': False, 'reason': f'Dangerous pattern detected: {pattern}'}

    # Block commands with dangerous options
    dangerous_options = ['--no-preserve-root', '--privileged']
    for opt in dangerous_options:
        if opt in cmd:
            return {'valid': False, 'reason': f'Dangerous option: {opt}'}

    return {'valid': True}


def _truncate_error(error: str, max_length: int = MAX_ERROR_LENGTH) -> str:
    """Truncate error message."""
    if len(error) <= max_length:
        return error
    return error[:max_length] + f'... [truncated, total {len(error)} chars]'


def _truncate_output(output: str, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    """
    Truncate command output.
    
    v3 DoD: max_length is now 100KB (was 10KB).
    """
    if len(output) <= max_length:
        return output
    return output[:max_length] + f'\n... [truncated, total {len(output)} chars]'


def _truncate_tool_result(result: Dict[str, Any], max_chars: int = 2000, head: int = 800, tail: int = 800) -> Dict[str, Any]:
    """Truncate over-long tool results, keeping head and tail portions.
    
    v0.5.9: prevent context explosion, read_file can return entire file content of 45K+ characters.
    
    Args:
        result: Tool execution result dict
        max_chars: maximum character count
        head: head character count to keep
        tail: tail character count to keep
    """
    # First check total serialized size
    try:
        result_str = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return result
    
    if len(result_str) <= max_chars:
        return result
    
    # targeting lines fieldtruncate(read_file mainoutput)
    if 'lines' in result and isinstance(result['lines'], list):
        lines = result['lines']
        # Concatenate lines into a string to check size
        lines_text = '\n'.join(lines)
        if len(lines_text) > max_chars:
            # line-by-line compute, keep head and tail
            head_lines = []
            head_chars = 0
            for line in lines:
                if head_chars + len(line) + 1 > head:
                    break
                head_lines.append(line)
                head_chars += len(line) + 1
            
            tail_lines = []
            tail_chars = 0
            for line in reversed(lines):
                if tail_chars + len(line) + 1 > tail:
                    break
                tail_lines.insert(0, line)
                tail_chars += len(line) + 1
            
            omitted_count = len(lines) - len(head_lines) - len(tail_lines)
            if omitted_count > 0:
                result = dict(result)  # shallow copy
                result['lines'] = head_lines + [f'... [omitted {omitted_count} lines, {len(lines)} total lines]'] + tail_lines
                result['truncated'] = True
    
    # targeting output fieldtruncate(shell_exec mainoutput)
    elif 'output' in result and isinstance(result['output'], str):
        output = result['output']
        if len(output) > max_chars:
            result = dict(result)
            result['output'] = (
                output[:head] +
                f'\n... [omitted {len(output) - head - tail} characters, {len(output)} total characters] \n' +
                output[-tail:]
            )
            result['truncated'] = True
    
    # targeting stdout fieldtruncate(shell_exec Returnstdout/stderr)
    elif 'stdout' in result and isinstance(result['stdout'], str) and len(result.get('stdout', '')) > max_chars:
        stdout = result['stdout']
        result = dict(result)
        result['stdout'] = (
            stdout[:head] +
            f'\n... [omitted {len(stdout) - head - tail} characters, {len(stdout)} total characters] \n' +
            stdout[-tail:]
        )
        result['truncated'] = True
    
    # targeting matched/matches fieldtruncate(search_files mainoutput)
    elif ('matched' in result and isinstance(result['matched'], list)):
        matched = result['matched']
        if len(matched) > 20:  # at mostretain20MatchResult
            result = dict(result)
            result['matched'] = matched[:20]
            result['truncated'] = True
            result['total_matched'] = len(matched)
    elif ('matches' in result and isinstance(result['matches'], list)):
        matches = result['matches']
        if len(matches) > 20:  # at mostretain20MatchResult
            result = dict(result)
            result['matches'] = matches[:20]
            result['truncated'] = True
            result['total_matched'] = len(matches)
    
    return result


# =============================================================================
# Built-in Tool Definitions
# =============================================================================

def get_builtin_tools() -> List[Any]:
    """
    Get list of all built-in tool definitions.

    Returns:
        List of ToolDefinition objects ready for registration
    """
    from .tool_registry import ToolDefinition, VerifyLevel, SandboxLevel

    return [
        ToolDefinition(
            name='read_file',
            description='Read file content with line numbers. Supports offset and limit for partial reads.',
            params=READ_FILE_PARAMS,
            handler=read_file_handler,
            verify_level=VerifyLevel.SCHEMA,
            timeout=30,
            edition='both',
            sandbox_level=SandboxLevel.RECOMMENDED,  # P1-6: Filereadsuggestsandbox
        ),
        ToolDefinition(
            name='write_file',
            description='Safely write content to a file. Can append or overwrite.',
            params=WRITE_FILE_PARAMS,
            handler=write_file_handler,
            verify_level=VerifyLevel.SCHEMA,
            timeout=30,
            edition='both',
            sandbox_level=SandboxLevel.REQUIRED,  # P1-6: FileWritemust-sandbox
        ),
        ToolDefinition(
            name='list_dir',
            description='List directory contents with file metadata.',
            params=LIST_DIR_PARAMS,
            handler=list_dir_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.NONE,  # P1-6: directorylistread-only,security
        ),
        ToolDefinition(
            name='shell_exec',
            description='Execute shell commands with security whitelist. Only safe commands allowed.',
            params=SHELL_EXEC_PARAMS,
            handler=shell_exec_handler,
            verify_level=VerifyLevel.SCHEMA,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.REQUIRED,  # P1-6: shell Executemust-sandbox
        ),
        ToolDefinition(
            name='http_get',
            description='Perform HTTP GET request to fetch URL content. SSRF protected.',
            params=HTTP_GET_PARAMS,
            handler=http_get_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.NONE,  # P1-6: HTTP request has SSRF protection
        ),
        ToolDefinition(
            name='http_post',
            description='Perform HTTP POST request. SSRF protected.',
            params=HTTP_POST_PARAMS,
            handler=http_post_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.NONE,  # P1-6: HTTP request has SSRF protection
        ),
        ToolDefinition(
            name='search_files',
            description='Search for files by name pattern or content text.',
            params=SEARCH_FILES_PARAMS,
            handler=search_files_handler,
            verify_level=VerifyLevel.SCHEMA,
            timeout=30,
            edition='both',
            sandbox_level=SandboxLevel.NONE,  # P1-6: file search is read-only, safe
        ),
    ]


async def register_builtin_tools(registry=None) -> int:
    """
    Register all built-in tools to the registry.

    Args:
        registry: ToolRegistry instance (creates if None)

    Returns:
        Number of tools registered
    """
    if registry is None:
        from .tool_registry import get_registry
        registry = get_registry()

    tools = get_builtin_tools()
    for tool in tools:
        await registry.register(tool)

    logger.info(f"Registered {len(tools)} built-in tools")
    return len(tools)


def register_builtin_tools_sync(registry=None) -> int:
    """Synchronous version of register_builtin_tools."""
    if registry is None:
        from .tool_registry import ToolRegistry
        registry = ToolRegistry()

    tools = get_builtin_tools()
    for tool in tools:
        registry.register_sync(tool)

    logger.info(f"Registered {len(tools)} built-in tools")
    return len(tools)


# =============================================================================
# patch_file Tool (v2 1a DoD)
# =============================================================================

# JSON Schema for patch_file
PATCH_FILE_PARAMS = {
    'type': 'object',
    'properties': {
        'path': {
            'type': 'string',
            'description': 'File path to patch (parameter name is "path", NOT "file_path")'
        },
        'find': {
            'type': 'string',
            'description': 'Text to find and replace - exact string match, NOT regex (parameter name is "find", NOT "old_content")'
        },
        'replace': {
            'type': 'string',
            'description': 'Replacement text - will replace the matched "find" text (parameter name is "replace", NOT "new_content")'
        },
        'count': {
            'type': 'integer',
            'description': 'Maximum number of replacements (1-10)',
            'minimum': 1,
            'maximum': 10,
            'default': 1
        },
        'backup': {
            'type': 'boolean',
            'description': 'Create backup before patching',
            'default': True
        }
    },
    'required': ['path', 'find', 'replace']
}

# Maximum patches per call
MAX_PATCH_COUNT = 10


async def patch_file_handler(params: Dict[str, Any], context: Dict[str, Any]) -> ToolResult:
    """
    Patch a file using find+replace.
    
    v0.13: post-write auto lint, rollback to backup on lint failure.
    
    Security measures:
    - Creates backup before patching
    - Limited to max 10 replacements per call
    - Only for admin/operator roles (via allowed_roles)
    
    Args:
        params: Dictionary with path, find, replace, count, backup
        context: Execution context
        
    Returns:
        ToolResult with patch count and backup info
    """
    path = params.get('path', '')
    find_text = params.get('find', '')
    replace_text = params.get('replace', '')
    count = min(params.get('count', 1), MAX_PATCH_COUNT)
    backup = params.get('backup', True)
    
    # Validation
    if not path:
        return ToolResult(success=False, error="path is required")
    if not find_text:
        return ToolResult(success=False, error="find text is required")
    
    path = os.path.expanduser(path)
    
    # security: uses security_baseline validate_path_safety for path traversal protection
    # resolve real path (including symlinks) and check if within allowed directory
    try:
        from tical_code.core.security_baseline import validate_path_safety
        safe, reason = validate_path_safety(path)
        if not safe:
            return ToolResult(success=False, error=f"Path not allowed: {reason}")
    except ImportError:
        # Degrade: use basic check when security_baseline unavailable
        resolved = os.path.realpath(path)
        if '..' in resolved or resolved.startswith('/etc') or resolved.startswith('/var'):
            return ToolResult(success=False, error="Path not allowed for security reasons")
    
    try:
        # Read original file
        with open(path, 'r', encoding='utf-8') as f:
            original_content = f.read()
        
        # Create backup
        backup_path = None
        if backup:
            backup_path = f"{path}.bak"
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(original_content)
            logger.debug(f"Created backup: {backup_path}")
        
        # Perform replacement
        new_content = original_content.replace(find_text, replace_text, count)
        
        # Count actual replacements
        patch_count = original_content.count(find_text)
        if patch_count > MAX_PATCH_COUNT:
            patch_count = MAX_PATCH_COUNT
        
        # Write patched content
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)

        return ToolResult(
            success=True,
            data={
                'path': path,
                'patches_applied': patch_count,
                'backup_path': backup_path,
            }
        )
        
    except FileNotFoundError:
        return ToolResult(success=False, error=f"File not found: {path}")
    except PermissionError:
        return ToolResult(success=False, error=f"Permission denied: {path}")
    except Exception as e:
        return ToolResult(success=False, error=f"Patch failed: {str(e)}")


# =============================================================================
# extract_text Tool (v2 1b DoD)
# =============================================================================

# JSON Schema for extract_text
EXTRACT_TEXT_PARAMS = {
    'type': 'object',
    'properties': {
        'url': {
            'type': 'string',
            'description': 'URL to extract text from'
        },
        'max_length': {
            'type': 'integer',
            'description': 'Maximum text length to extract',
            'minimum': 100,
            'maximum': 100000,
            'default': 50000
        }
    },
    'required': ['url']
}


async def extract_text_handler(params: Dict[str, Any], context: Dict[str, Any]) -> ToolResult:
    """
    Extract plain text from a webpage using html.parser (stdlib only).
    
    Uses Python's built-in html.parser module - no new dependencies.
    
    Args:
        params: Dictionary with url and optional max_length
        context: Execution context
        
    Returns:
        ToolResult with extracted text
    """
    from html.parser import HTMLParser
    
    url = params.get('url', '')
    max_length = params.get('max_length', 50000)
    
    if not url:
        return ToolResult(success=False, error="url is required")
    
    # SSRF protection
    if not _is_url_allowed(url):
        return ToolResult(success=False, error=f"URL not allowed: {url}")
    
    # Check rate limit
    rate_limiter = get_rate_limiter()
    parsed = _parse_url(url)
    if parsed:
        domain = parsed.netloc
        if not rate_limiter.check(domain):
            return ToolResult(
                success=False,
                error=f"Rate limit exceeded for domain: {domain}"
            )
    
    try:
        import urllib.request
        import urllib.error
        
        # Fetch HTML
        with urllib.request.urlopen(url, timeout=10) as response:
            html = response.read().decode('utf-8', errors='ignore')
        
        # Extract text using html.parser
        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text_parts = []
                self.skip_tags = {'script', 'style', 'noscript'}
                self.current_tag = None
            
            def handle_starttag(self, tag, attrs):
                self.current_tag = tag
            
            def handle_endtag(self, tag):
                if tag == self.current_tag:
                    self.current_tag = None
            
            def handle_data(self, data):
                if self.current_tag not in self.skip_tags:
                    text = data.strip()
                    if text:
                        self.text_parts.append(text)
            
            def get_text(self):
                return ' '.join(self.text_parts)
        
        extractor = TextExtractor()
        extractor.feed(html)
        text = extractor.get_text()
        
        # Truncate if needed
        if len(text) > max_length:
            text = text[:max_length] + f"... [truncated from {len(text)} chars]"
        
        return ToolResult(
            success=True,
            data={
                'url': url,
                'text': text,
                'original_length': len(text)
            }
        )
        
    except urllib.error.URLError as e:
        return ToolResult(success=False, error=f"URL fetch failed: {str(e)}")
    except Exception as e:
        return ToolResult(success=False, error=f"Extract failed: {str(e)}")


# Update get_builtin_tools to include new tools

# =============================================================================
# check_self handler
# =============================================================================
def check_self_handler() -> Dict[str, Any]:
    """Report current config, model, version, hostname."""
    try:
        result = {
            "version": open(os.path.join(os.path.dirname(__file__), "..", "..", "VERSION")).read().strip(),
            "hostname": os.uname().nodename,
            "platform": "eite-agent",
        }
        # Try to get config info
        config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config.json")
        if os.path.exists(config_path):
            import json
            cfg = json.load(open(config_path))
            result["model"] = cfg.get("ai_model", "unknown")
            result["profile"] = cfg.get("profile", "full")
        return {"success": True, "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# Web fetch handler (alias for http_get, returns plain text)
# =============================================================================
def web_fetch_handler(url: str, timeout: int = 15) -> Dict[str, Any]:
    """Fetch URL content as readable text. SSRF protected."""
    try:
        if not _security_context.is_domain_allowed(url):
            return {"success": False, "error": f"Domain not allowed: {url}"}
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # Strip HTML tags for plain text
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        max_len = 50000
        if len(text) > max_len:
            text = text[:max_len] + "... [truncated]"
        return {"success": True, "data": text, "url": url, "bytes": len(html)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# Chat send handler (send message to mesh peers)
# =============================================================================
def chat_send_handler(target: str, message: str) -> Dict[str, Any]:
    """Send a message to another worker in the mesh."""
    # Placeholder - actual mesh comms require channel bridge
    return {"success": False, "error": "Mesh communication not wired yet. Use SSH to reach peers."}


# =============================================================================
# Memory search handler
# =============================================================================
def memory_search_handler(query: str, limit: int = 10) -> Dict[str, Any]:
    """Search past conversations and knowledge via FTS5."""
    if _memory_store is None:
        return {"success": False, "error": "MemoryStore not available"}
    try:
        results = _memory_store.search(query, limit=limit)
        formatted = []
        for r in results:
            formatted.append({
                "source": r.file_key,
                "section": r.section_title,
                "snippet": r.snippet.replace(">>>", "**").replace("<<<", "**"),
                "relevance": round(r.rank, 3),
            })
        return {"success": True, "query": query, "results": formatted, "total": len(formatted)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# Memory save handler
# =============================================================================
def memory_save_handler(key: str, content: str, section: str = "") -> Dict[str, Any]:
    """Persist important facts for future recall."""
    if _memory_store is None:
        return {"success": False, "error": "MemoryStore not available"}
    try:
        from tical_code.core.memory_store import MemoryEntry
        entry = MemoryEntry(
            file_key=key,
            content=content,
            section_title=section or "general",
        )
        _memory_store.save(entry)
        return {"success": True, "key": key, "section": section}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# Cron management handlers
# =============================================================================
def cron_add_handler(job_id: str, schedule: str, command: str, name: str = "") -> Dict[str, Any]:
    """Add a scheduled cron job."""
    if _cron_manager is None:
        return {"success": False, "error": "CronManager not available"}
    try:
        import asyncio
        from tical_code.core.cron import CronJob, CronSchedule
        job = CronJob(
            job_id=job_id,
            name=name or job_id,
            description=command[:100],
            schedule=CronSchedule.parse(schedule),
            task_type="shell",
            task_params={"cmd": command},
            created_by="user",
        )
        asyncio.run(_cron_manager.add_job(job))
        return {"success": True, "job_id": job_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


def cron_list_handler(enabled_only: bool = False) -> Dict[str, Any]:
    """List scheduled cron jobs."""
    if _cron_manager is None:
        return {"success": False, "error": "CronManager not available"}
    try:
        jobs = _cron_manager.list_jobs(enabled_only=enabled_only)
        return {"success": True, "jobs": [
            {"id": j.job_id, "name": j.name, "schedule": str(j.schedule),
             "enabled": j.enabled, "last_run": str(j.last_run or "")}
            for j in jobs
        ]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def cron_remove_handler(job_id: str) -> Dict[str, Any]:
    """Remove a scheduled cron job."""
    if _cron_manager is None:
        return {"success": False, "error": "CronManager not available"}
    try:
        import asyncio
        asyncio.run(_cron_manager.remove_job(job_id))
        return {"success": True, "job_id": job_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# verify_multi handler
# =============================================================================
def verify_multi_handler(prompt: str) -> Dict[str, Any]:
    """Send the same prompt to all available models and compare answers."""
    try:
        # Lazy import to avoid circular dependency
        from tical_code.core.tool_executor import exec_verify_multi
        result = exec_verify_multi({"prompt": prompt})
        return result
    except Exception as e:
        return {"success": False, "error": f"verify_multi failed: {e}"}


# =============================================================================
# Task management handlers
# =============================================================================
def task_create_handler(goal: str, context: str = "") -> Dict[str, Any]:
    """Create a new autonomous task."""
    return {"success": False, "error": "Task management requires TaskStateMachine module. Not available via builtin_tools."}

def task_list_handler() -> Dict[str, Any]:
    """List active autonomous tasks."""
    return {"success": False, "error": "Task management requires TaskStateMachine module. Not available via builtin_tools."}

def task_status_handler(task_id: str) -> Dict[str, Any]:
    """Check status of an autonomous task."""
    return {"success": False, "error": "Task management requires TaskStateMachine module. Not available via builtin_tools."}


_builtin_tools_cache = None

# =============================================================================
# SubAgent Delegation Tool Handlers (v0.3 P1)
# =============================================================================
# These handlers bridge subagent.py (async, framework-based) to the sync
# tool_executor dispatch system. They use a global _subagent_manager reference
# wired via set_subagent_manager().

import asyncio as _asyncio


def delegate_task_handler(*args, **kwargs) -> Dict[str, Any]:
    """Sync wrapper for delegate_tool_handler from subagent.py.

    Delegates a task to a sub-agent for parallel execution.
    Uses the globally-wired _subagent_manager from tool_executor.
    """
    from .tool_executor import _subagent_manager as _mgr

    params = args[0] if args else kwargs
    description = params.get("description")
    if not description:
        return {"error": "Missing required parameter: description"}

    if _mgr is None:
        return {"error": "SubAgentManager not wired. Call set_subagent_manager() during bootstrap."}

    tools = params.get("tools")
    max_iterations = params.get("max_iterations", 5)

    try:
        task = _asyncio.run(_mgr.delegate(
            description=description,
            tools=tools,
            max_iterations=max_iterations,
        ))
        return {
            "success": True,
            "task_id": task.task_id,
            "status": "pending",
            "message": f"Task delegated. Use get_subagent_result with task_id=\"{task.task_id}\" to retrieve results.",
        }
    except Exception as e:
        logger.error(f"[delegate_task_handler] Error: {e}")
        return {"success": False, "error": str(e)}


def get_subagent_result_handler(*args, **kwargs) -> Dict[str, Any]:
    """Sync wrapper for get_subagent_result_handler from subagent.py.

    Retrieves the result of a previously delegated sub-agent task.
    Uses the globally-wired _subagent_manager from tool_executor.
    """
    from .tool_executor import _subagent_manager as _mgr

    params = args[0] if args else kwargs
    task_id = params.get("task_id")
    if not task_id:
        return {"error": "Missing required parameter: task_id"}

    if _mgr is None:
        return {"error": "SubAgentManager not wired. Call set_subagent_manager() during bootstrap."}

    try:
        task = _asyncio.run(_mgr.get_result(task_id))
        if not task:
            return {"success": False, "error": f"Task not found: {task_id}"}
        return {
            "success": True,
            "task_id": task.task_id,
            "status": task.status,
            "result": task.result,
            "verified": task.verified,
            "elapsed_ms": task.elapsed_ms,
        }
    except Exception as e:
        logger.error(f"[get_subagent_result_handler] Error: {e}")
        return {"success": False, "error": str(e)}


def get_builtin_tools() -> List:
    """
    Get list of all built-in tool definitions.
    
    Returns cached list if available, otherwise builds and caches.
    """
    global _builtin_tools_cache
    
    if _builtin_tools_cache is not None:
        return _builtin_tools_cache
    
    from .tool_registry import ToolDefinition, VerifyLevel, SandboxLevel
    
    _builtin_tools_cache = [
        ToolDefinition(
            name='read_file',
            description='Read file content with line numbers. Supports offset and limit for partial reads.',
            params=READ_FILE_PARAMS,
            handler=read_file_handler,
            verify_level=VerifyLevel.SCHEMA,
            timeout=30,
            edition='both',
            sandbox_level=SandboxLevel.NONE,
        ),
        ToolDefinition(
            name='write_file',
            description='Safely write content to a file. Can append or overwrite.',
            params=WRITE_FILE_PARAMS,
            handler=write_file_handler,
            verify_level=VerifyLevel.SCHEMA,
            timeout=30,
            edition='both',
            sandbox_level=SandboxLevel.RECOMMENDED,
        ),
        ToolDefinition(
            name='list_dir',
            description='List directory contents with file metadata.',
            params=LIST_DIR_PARAMS,
            handler=list_dir_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.NONE,
        ),
        ToolDefinition(
            name='shell_exec',
            description='Execute shell commands with security whitelist. Only safe commands allowed.',
            params=SHELL_EXEC_PARAMS,
            handler=shell_exec_handler,
            verify_level=VerifyLevel.SCHEMA,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.REQUIRED,
        ),
        ToolDefinition(
            name='http_get',
            description='Perform HTTP GET request to fetch URL content. SSRF protected.',
            params=HTTP_GET_PARAMS,
            handler=http_get_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.RECOMMENDED,
        ),
        ToolDefinition(
            name='http_post',
            description='Perform HTTP POST request. SSRF protected.',
            params=HTTP_POST_PARAMS,
            handler=http_post_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.RECOMMENDED,
        ),
        ToolDefinition(
            name='search_files',
            description='Search for files by name pattern or content text.',
            params=SEARCH_FILES_PARAMS,
            handler=search_files_handler,
            verify_level=VerifyLevel.SCHEMA,
            timeout=30,
            edition='both',
            sandbox_level=SandboxLevel.NONE,
        ),
        # v2 1a: patch_file tool
        ToolDefinition(
            name='patch_file',
            description='Patch a file using find+replace. Creates backup first. Limited to 10 replacements.',
            params=PATCH_FILE_PARAMS,
            handler=patch_file_handler,
            verify_level=VerifyLevel.IDENTITY,
            timeout=30,
            edition='both',
            allowed_roles=['admin', 'operator'],  # v2 0d: restricted roles
            sandbox_level=SandboxLevel.RECOMMENDED,
        ),
        # v2 1b: extract_text tool
        ToolDefinition(
            name='extract_text',
            description='Extract plain text from a webpage using html.parser. No new dependencies.',
            params=EXTRACT_TEXT_PARAMS,
            handler=extract_text_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=15,
            edition='both',
            sandbox_level=SandboxLevel.RECOMMENDED,
        ),
        # check_self: report identity, version, model
        ToolDefinition(
            name='check_self',
            description='Report your actual config, model, and version verbatim.',
            params={"type": "object", "properties": {}},
            handler=check_self_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.NONE,
        ),
        # web_fetch: fetch URL content as text
        ToolDefinition(
            name='web_fetch',
            description='Fetch URL content as readable plain text. SSRF protected.',
            params={"type": "object", "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 15},
            }, "required": ["url"]},
            handler=web_fetch_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=15,
            edition='full',
            sandbox_level=SandboxLevel.RECOMMENDED,
        ),
        # chat_send: mesh communication
        ToolDefinition(
            name='chat_send',
            description='Send a message to other workers in the mesh.',
            params={"type": "object", "properties": {
                "target": {"type": "string", "description": "Target worker name"},
                "message": {"type": "string", "description": "Message content"},
            }, "required": ["target", "message"]},
            handler=chat_send_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='full',
            sandbox_level=SandboxLevel.NONE,
        ),
        # memory_search: search past conversations
        ToolDefinition(
            name='memory_search',
            description='Search past conversations and knowledge using FTS5.',
            params={"type": "object", "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 10},
            }, "required": ["query"]},
            handler=memory_search_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=15,
            edition='full',
            sandbox_level=SandboxLevel.NONE,
        ),
        # memory_save: persist facts
        ToolDefinition(
            name='memory_save',
            description='Persist important facts for future recall.',
            params={"type": "object", "properties": {
                "key": {"type": "string", "description": "Memory key/identifier"},
                "content": {"type": "string", "description": "Content to remember"},
                "section": {"type": "string", "description": "Optional section/category", "default": ""},
            }, "required": ["key", "content"]},
            handler=memory_save_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='full',
            sandbox_level=SandboxLevel.NONE,
        ),
        # cron_add: schedule a job
        ToolDefinition(
            name='cron_add',
            description='Add a scheduled cron job.',
            params={"type": "object", "properties": {
                "job_id": {"type": "string", "description": "Unique job identifier"},
                "schedule": {"type": "string", "description": "Schedule (e.g. '30m', 'every 2h', '0 9 * * *')"},
                "command": {"type": "string", "description": "Shell command to execute"},
                "name": {"type": "string", "description": "Human-friendly name", "default": ""},
            }, "required": ["job_id", "schedule", "command"]},
            handler=cron_add_handler,
            verify_level=VerifyLevel.IDENTITY,
            timeout=10,
            edition='full',
            sandbox_level=SandboxLevel.NONE,
        ),
        # cron_list: list scheduled jobs
        ToolDefinition(
            name='cron_list',
            description='List all scheduled cron jobs.',
            params={"type": "object", "properties": {
                "enabled_only": {"type": "boolean", "description": "Only show enabled jobs", "default": False},
            }},
            handler=cron_list_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='full',
            sandbox_level=SandboxLevel.NONE,
        ),
        # cron_remove: delete a job
        ToolDefinition(
            name='cron_remove',
            description='Remove a scheduled cron job.',
            params={"type": "object", "properties": {
                "job_id": {"type": "string", "description": "Job ID to remove"},
            }, "required": ["job_id"]},
            handler=cron_remove_handler,
            verify_level=VerifyLevel.IDENTITY,
            timeout=10,
            edition='full',
            sandbox_level=SandboxLevel.NONE,
        ),
        # verify_multi: multi-model verification
        ToolDefinition(
            name='verify_multi',
            description='Send same prompt to ALL available models and compare answers.',
            params={"type": "object", "properties": {
                "prompt": {"type": "string", "description": "Prompt to send to all models"},
            }, "required": ["prompt"]},
            handler=verify_multi_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=30,
            edition='full',
            sandbox_level=SandboxLevel.NONE,
        ),
        # safe_modify: self-repair pipeline modification
        ToolDefinition(
            name='safe_modify',
            description='Safely modify a file with full safety checks (protected file check, git backup, syntax validation, code safety, sandbox test, cross-verify, audit log). USE THIS instead of file_write for system code.',
            params={"type": "object", "properties": {
                "path": {"type": "string", "description": "File path to modify"},
                "new_content": {"type": "string", "description": "New complete file content"},
                "reason": {"type": "string", "description": "Human-readable reason for this modification"},
                "sandbox_test": {"type": "boolean", "description": "Run sandbox test after write (default: true)"},
            }, "required": ["path", "new_content", "reason"]},
            handler=safe_modify_handler,
            verify_level=VerifyLevel.SCHEMA,
            timeout=60,
            edition='full',
            sandbox_level=SandboxLevel.REQUIRED,
        ),
        # safe_modify_diff: targeted diff through safety pipeline
        ToolDefinition(
            name='safe_modify_diff',
            description='Apply a targeted find-and-replace through the safe_modify pipeline (safety checks + rollback). USE THIS instead of patch_file for system code edits.',
            params={"type": "object", "properties": {
                "path": {"type": "string", "description": "File path to edit"},
                "old_string": {"type": "string", "description": "Text to find (include surrounding context for uniqueness)"},
                "new_string": {"type": "string", "description": "Replacement text. Pass empty string to delete."},
                "reason": {"type": "string", "description": "Human-readable reason for this modification"},
            }, "required": ["path", "old_string", "new_string", "reason"]},
            handler=safe_modify_diff_handler,
            verify_level=VerifyLevel.SCHEMA,
            timeout=60,
            edition='full',
            sandbox_level=SandboxLevel.REQUIRED,
        ),
        # checkpoint_list: list available checkpoints
        ToolDefinition(
            name='checkpoint_list',
            description='List all available checkpoints/snapshots with optional status filter.',
            params={"type": "object", "properties": {
                "status": {"type": "string", "description": "Optional filter: 'incomplete' or 'complete'", "enum": ["incomplete", "complete"]},
            }},
            handler=checkpoint_list_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='full',
            sandbox_level=SandboxLevel.NONE,
        ),
        # checkpoint_restore: restore from a checkpoint
        ToolDefinition(
            name='checkpoint_restore',
            description='Restore files from a checkpoint. Requires confirm=True. Call without confirm first to preview what files will be affected.',
            params={"type": "object", "properties": {
                "checkpoint_id": {"type": "string", "description": "Checkpoint ID to restore from"},
                "selective_files": {"type": "array", "items": {"type": "string"}, "description": "Optional list of specific file paths to restore"},
                "confirm": {"type": "boolean", "description": "Must be True to proceed. Call without confirm first to preview.", "default": False},
            }, "required": ["checkpoint_id"]},
            handler=checkpoint_restore_handler,
            verify_level=VerifyLevel.IDENTITY,
            timeout=30,
            edition='full',
            sandbox_level=SandboxLevel.NONE,
        ),
        # ask_user: ask the human for input when stuck
        ToolDefinition(
            name='ask_user',
            description='Ask the human user for input when you are stuck, need a CAPTCHA code, need confirmation, '
                        'or cannot proceed with the current task. Use this instead of trying the same thing repeatedly.',
            params={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user. Be specific about what you need."
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context explaining why you need this input (e.g., 'CAPTCHA detected on login page', 'need confirmation to proceed')"
                    }
                },
                "required": ["question"]
            },
            handler=ask_user_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.NONE,
        ),
        # end_task: signal task completion
        ToolDefinition(
            name='end_task',
            description='Signal that the current task is complete. Call when all work is done. Triggers memory consolidation.',
            params={"type": "object", "properties": {
                "success": {"type": "boolean", "description": "Whether the task succeeded", "default": True},
            }},
            handler=end_task_handler,
            verify_level=VerifyLevel.BASIC,
            timeout=10,
            edition='both',
            sandbox_level=SandboxLevel.NONE,
        ),

    ]

    return _builtin_tools_cache

# =============================================================================
# safe_modify / safe_modify_diff / checkpoint handlers - bridge to module-level exec
# =============================================================================

def end_task_handler(*args, **kwargs) -> Dict[str, Any]:
    """Bridge handler: delegates to tool_executor.exec_end_task.
    Accepts both handler({"success": True}) and handler(key=val, ...)."""
    from tical_code.core.tool_executor import exec_end_task
    params = args[0] if args else kwargs
    return exec_end_task(params)

def safe_modify_handler(*args, **kwargs) -> Dict[str, Any]:
    """Bridge handler: delegates to tool_executor.exec_safe_modify.
    Accepts both handler({"success": True}) and handler(key=val, ...)."""
    from tical_code.core.tool_executor import exec_safe_modify
    params = args[0] if args else kwargs
    return exec_safe_modify(params)


def safe_modify_diff_handler(*args, **kwargs) -> Dict[str, Any]:
    """Bridge handler: delegates to tool_executor.exec_safe_modify_diff.
    Accepts both handler({"success": True}) and handler(key=val, ...)."""
    from tical_code.core.tool_executor import exec_safe_modify_diff
    params = args[0] if args else kwargs
    return exec_safe_modify_diff(params)


def checkpoint_list_handler(*args, **kwargs) -> Dict[str, Any]:
    """Bridge handler: delegates to tool_executor.exec_checkpoint_list.
    Accepts both handler({"success": True}) and handler(key=val, ...)."""
    from tical_code.core.tool_executor import exec_checkpoint_list
    params = args[0] if args else kwargs
    return exec_checkpoint_list(params)


def checkpoint_restore_handler(*args, **kwargs) -> Dict[str, Any]:
    """Bridge handler: delegates to tool_executor.exec_checkpoint_restore.
    Accepts both handler({"success": True}) and handler(key=val, ...)."""
    from tical_code.core.tool_executor import exec_checkpoint_restore
    params = args[0] if args else kwargs
    return exec_checkpoint_restore(params)


# =============================================================================
# ask_user Tool - ask the human user for input
# =============================================================================
def ask_user_handler(*args, **kwargs) -> Dict[str, Any]:
    """Ask the human user for input when the AI is stuck, needs a CAPTCHA code,
    needs confirmation, or cannot proceed with the current task.

    Use this instead of trying the same thing repeatedly.

    Args:
        params: Dictionary with question (required) and context (optional)
        context: Execution context

    Returns:
        Dict with needs_user_input=True flag so the executor pauses for human response
    """
    question = params.get("question", "")
    context_str = params.get("context", "")

    message = f"[NEED_USER_INPUT] {question}"
    if context_str:
        message += f"\nContext: {context_str}"

    return {
        "success": True,
        "output": message,
        "needs_user_input": True,
        "question": question,
        "context": context_str,
    }

