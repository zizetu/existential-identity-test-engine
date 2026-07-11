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
Security Baseline Hardening
===========================

Provides TOCTOU path validation, SSRF protection, and sensitive info
redaction for the EITElite agent platform.

Core design:
1. TOCTOUProtection - path security check, prevents symlink attacks and path traversal
2. SSRFProtection - URL security check, prevents private IP access and DNS rebinding
3. Sensitive info redaction - regex detects API Key/Token/password/private-key/connection strings
4. Outbound filtering - integrates URL verification + SSRF protection + domain allow/block lists

Security principles:
- security checks are mandatory, cannot be bypassed
- redaction must not lose functional info (retain structure, only replace values)
- lock between check and operation, prevents race conditions
- pure stdlib preferred

Author: Tical (Zize Tu)
Version: see tical_code.__version__
"""

import ipaddress
import logging
import os
import re
import socket
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# =============================================================================
# TOCTOUProtection - pathsecurity
# =============================================================================

# global path lock, prevents race condition between check and operation
_path_lock = threading.Lock()


@dataclass
class PathSafetyConfig:
    """pathsecurityConfig.

    Attributes:
        allowed_dirs: allowroot-ofdirectorylist
        deny_symlinks: whetherrejectsymlink
        deny_absolute: whetherrejectabsolute-pathbreak-outsandbox
        max_path_length: pathmaximumlength
    """
    allowed_dirs: List[str] = field(default_factory=lambda: ["."])
    deny_symlinks: bool = True
    deny_absolute: bool = True
    max_path_length: int = 4096


def validate_path_safety(
    path: str,
    allowed_dirs: Optional[List[str]] = None,
    config: Optional[PathSafetyConfig] = None,
) -> Tuple[bool, str]:
    """
    pathsecurityCheck(TOCTOUProtection).

    Checkcontent:
    - pathtraverseattack:reject .. break-outallowdirectory
    - absolute-path: check if within allowed directories
    - symlink:Checkresolveafterrealpathwhethersecurity
    - pathlength:rejecttoo-longpath

    note: this function internally locks, guarantees atomicity between check and resolve.

    Args:
        path: path to check
        allowed_dirs: list of allowed root directories (None uses config)
        config: pathsecurityConfig

    Returns:
        (safe, reason): safe=True indicates path is safe, reason is empty or cause note
    """
    cfg = config or PathSafetyConfig()
    dirs = allowed_dirs or cfg.allowed_dirs

    # empty path check
    if not path or not path.strip():
        return False, "empty path"

    # pathlengthCheck
    if len(path) > cfg.max_path_length:
        return False, f"path too long: {len(path)} > {cfg.max_path_length}"

    # Checkpathtraverse(originalpathin..)
    # note: don't directly reject .., rather check if resolve breaks out
    normalized = os.path.normpath(path)

    # Checkabsolute-path
    if os.path.isabs(normalized) and cfg.deny_absolute:
        # absolute path must be within allowed_dirs
        abs_resolved = os.path.realpath(normalized)
        if not _is_path_in_allowed_dirs(abs_resolved, dirs):
            return False, f"absolute path outside allowed dirs: {normalized}"

    # symlinkCheck(lockpreventTOCTOU)
    with _path_lock:
        # for relative path, check resolve result relative to current working directory
        if not os.path.isabs(normalized):
            resolved = os.path.realpath(os.path.join(os.getcwd(), normalized))
        else:
            resolved = os.path.realpath(normalized)

        if cfg.deny_symlinks:
            # check if path contains symlink
            if _contains_symlink(path):
                # symlink's resolve result must be within allowed directories
                if not _is_path_in_allowed_dirs(resolved, dirs):
                    return False, (
                        f"symlink points outside allowed dirs: "
                        f"path={normalized} → resolved={resolved}"
                    )

        # final check: resolved path must be within allowed directories
        if not _is_path_in_allowed_dirs(resolved, dirs):
            return False, (
                f"resolved path outside allowed dirs: "
                f"path={normalized} → resolved={resolved}"
            )

    return True, ""


def resolve_and_validate(
    path: str,
    allowed_dirs: Optional[List[str]] = None,
    config: Optional[PathSafetyConfig] = None,
) -> Tuple[Optional[str], bool]:
    """
    parse realpath and verify safety.

    lock-executes resolve and verify, prevents TOCTOU race condition.

    Args:
        path: path to parse
        allowed_dirs: allowroot-ofdirectorylist
        config: pathsecurityConfig

    Returns:
        (resolved_path, safe): resolved_path is the parsed absolute path or None, safe indicates whether secure
    """
    safe, reason = validate_path_safety(path, allowed_dirs, config)
    if not safe:
        logger.warning(f"[Security] path check failed: {reason}")
        return None, False

    with _path_lock:
        resolved = os.path.realpath(path)
        # secondary verify resolved path
        if not _is_path_in_allowed_dirs(resolved, allowed_dirs or ["."]):
            return None, False

    return resolved, True


def _is_path_in_allowed_dirs(resolved_path: str, allowed_dirs: List[str]) -> bool:
    """
    Check if resolved path is within allowed directory.

    Args:
        resolved_path: already parsed absolute path
        allowed_dirs: allowroot-ofdirectorylist

    Returns:
        True indicates within allowed directory
    """
    resolved_abs = os.path.abspath(resolved_path)

    for allowed in allowed_dirs:
        allowed_abs = os.path.realpath(os.path.abspath(allowed))
        # path must be within allowed directory (prefix match, ensure is child path)
        if resolved_abs == allowed_abs or resolved_abs.startswith(allowed_abs + os.sep):
            return True

    return False


def _contains_symlink(path: str) -> bool:
    """
    Check if path contains symlink.

    Args:
        path: path to check

    Returns:
        True indicates path contains symlink
    """
    try:
        # level-by-levelCheckpathComponent
        parts = os.path.normpath(path).split(os.sep)
        current = "/" if os.path.isabs(path) else "."

        for part in parts:
            if not part or part == ".":
                continue
            current = os.path.join(current, part)
            if os.path.islink(current):
                return True
    except (OSError, ValueError):
        logger.debug("security_baseline: symlink check exception, treating as potentially dangerous")

    return False


# =============================================================================
# SSRFProtection - URL security
# =============================================================================

# privateIPsubnet(RFC 1918 + loopback + link-local + metadata)
_PRIVATE_IP_RANGES: List[ipaddress.IPv4Network] = [
    ipaddress.IPv4Network('127.0.0.0/8'),       # Loopback
    ipaddress.IPv4Network('10.0.0.0/8'),         # private class A
    ipaddress.IPv4Network('172.16.0.0/12'),      # private class B
    ipaddress.IPv4Network('192.168.0.0/16'),     # private class C
    ipaddress.IPv4Network('169.254.0.0/16'),     # Link-local
    ipaddress.IPv4Network('0.0.0.0/8'),          # currentnetwork
    ipaddress.IPv4Network('100.64.0.0/10'),      # CGNAT
    ipaddress.IPv4Network('198.18.0.0/15'),      # benchmarktest
    ipaddress.IPv4Network('224.0.0.0/4'),        # multicast
    ipaddress.IPv4Network('240.0.0.0/4'),        # retain
]

# IPv6privaterange
_PRIVATE_IP_RANGES_V6: List[ipaddress.IPv6Network] = [
    ipaddress.IPv6Network('::1/128'),            # Loopback
    ipaddress.IPv6Network('fc00::/7'),           # ULA
    ipaddress.IPv6Network('fe80::/10'),          # Link-local
    ipaddress.IPv6Network('ff00::/8'),           # multicast
]

# dangerprotocolblacklist
_DANGEROUS_SCHEMES: FrozenSet[str] = frozenset({
    'file', 'gopher', 'dict', 'ftp', 'tftp',
    'ldap', 'ldaps', 'jar', 'netdoc', 'ssh',
    'telnet', 'sftp',
})


@dataclass
class URLSafetyConfig:
    """URLsecurityConfig.

    Attributes:
        allowed_schemes: allowed URL schemes (default http/https)
        domain_whitelist: domainwhitelist(if-empty-then-nolimitdomain)
        domain_blacklist: domainblacklist
        check_dns_rebinding: whether to check DNS rebinding
        allow_private_ip: Whether to allow private IP (default not allowed)
        max_redirects: maximumredirectcount
    """
    allowed_schemes: FrozenSet[str] = frozenset({'http', 'https'})
    domain_whitelist: List[str] = field(default_factory=list)
    domain_blacklist: List[str] = field(default_factory=list)
    check_dns_rebinding: bool = True
    allow_private_ip: bool = False
    max_redirects: int = 5
    dns_timeout: float = 5.0


def validate_url(
    url: str,
    config: Optional[URLSafetyConfig] = None,
) -> Tuple[bool, str]:
    """URL security check (SSRF protection).

    Check content:
    - protocol security: only allow http/https, reject dangerous schemes like file://
    - private IP blacklist: reject 127.x, 10.x, 172.16-31.x, 192.168.x, 169.254.x
    - DNS rebinding protection: check if parsed domain resolves to private IP
    - domain allow/block list check

    Args:
        url: URL to check
        config: URLsecurityConfig

    Returns:
        (safe, reason): safe=TrueindicatesURLsecurity
    """
    cfg = config or URLSafetyConfig()

    # empty URL check
    if not url or not url.strip():
        return False, "empty URL"

    # parseURL
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL parse failed: {e}"

    # Protocol check
    scheme = parsed.scheme.lower()
    if not scheme:
        return False, "URL missing scheme"

    if scheme in _DANGEROUS_SCHEMES:
        return False, f"dangerous scheme: {scheme}://"

    if scheme not in cfg.allowed_schemes:
        return False, f"disallowed scheme: {scheme}:// (allowed: {', '.join(sorted(cfg.allowed_schemes))})"

    # hostnameCheck
    hostname = parsed.hostname
    if not hostname:
        return False, "URL missing hostname"

    # domainblacklist
    hostname_lower = hostname.lower()
    for blocked in cfg.domain_blacklist:
        if hostname_lower == blocked.lower() or hostname_lower.endswith('.' + blocked.lower()):
            return False, f"domain in blacklist: {hostname}"

    # domain whitelist (if configured)
    if cfg.domain_whitelist:
        allowed = False
        for wl in cfg.domain_whitelist:
            if hostname_lower == wl.lower() or hostname_lower.endswith('.' + wl.lower()):
                allowed = True
                break
        if not allowed:
            return False, f"domain not in whitelist: {hostname}"

    # privateIPCheck(directlyIPaddress)
    if not cfg.allow_private_ip:
        is_private, reason = _check_ip_private(hostname)
        if is_private:
            return False, reason

    # DNS rebinding protection
    if cfg.check_dns_rebinding and not cfg.allow_private_ip:
        try:
            # resolve domain to IP with timeout
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(cfg.dns_timeout)
            try:
                addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            finally:
                socket.setdefaulttimeout(old_timeout)
            for family, _, _, _, sockaddr in addr_info:
                ip_str = sockaddr[0]
                is_private, reason = _check_ip_private(ip_str)
                if is_private:
                    return False, f"DNS rebinding protection: {hostname} resolved to private IP {ip_str}"
        except socket.gaierror:
            # DNS resolution failed - block as SSRF defense
            logger.warning(f"[Security] DNS resolution failed (SSRF): {hostname}")
            return False, "DNS resolution failed"
        except socket.timeout:
            logger.debug(f"[Security] DNS resolution timed out: {hostname}")
        except Exception as e:
            logger.debug(f"[Security] DNS check error: {hostname}, {e}")

    return True, ""


def _check_ip_private(ip_str: str) -> Tuple[bool, str]:
    """
    Check if IP address is private/reserved address.

    Args:
        ip_str: IP address string

    Returns:
        (is_private, reason)
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False, ""  # is notvalidIP,mayordomain

    # CheckIPv4privaterange
    if isinstance(ip, ipaddress.IPv4Address):
        for network in _PRIVATE_IP_RANGES:
            if ip in network:
                return True, f"private IP: {ip_str} (network: {network})"

    # CheckIPv6privaterange
    if isinstance(ip, ipaddress.IPv6Address):
        for network in _PRIVATE_IP_RANGES_V6:
            if ip in network:
                return True, f"private IPv6: {ip_str} (network: {network})"

    return False, ""


def _check_ssrf(url: str) -> None:
    """SSRF guard: validate URL safety before making outbound requests.

    Calls validate_url() and raises ValueError if the URL is unsafe
    (private IP, dangerous scheme, DNS rebinding, etc.).

    Args:
        url: URL to validate

    Raises:
        ValueError: URL is unsafe for outbound requests
    """
    safe, reason = validate_url(url)
    if not safe:
        raise ValueError(f"SSRF check failed for {url}: {reason}")


# =============================================================================
# Sensitive info desensitization
# =============================================================================

# desensitizeRegex patternlist
_DEFAULT_REDACTION_PATTERNS: List[Tuple[str, str, re.Pattern]] = [
    # API KeyMode
    (
        "api_key_openai",
        r'sk-[a-zA-Z0-9]{20,}',
        re.compile(r'sk-[a-zA-Z0-9]{20,}'),
    ),
    (
        "api_key_google",
        r'AIza[a-zA-Z0-9_-]{35}',
        re.compile(r'AIza[a-zA-Z0-9_-]{35}'),
    ),
    (
        "api_key_generic",
        r'["\']?api[_-]?key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?',
        re.compile(r'["\']?api[_-]?key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?', re.IGNORECASE),
    ),
    # TokenMode
    (
        "token_github",
        r'ghp_[a-zA-Z0-9]{36}',
        re.compile(r'ghp_[a-zA-Z0-9]{36}'),
    ),
    (
        "token_gitlab",
        r'glpat-[a-zA-Z0-9\-]{20,}',
        re.compile(r'glpat-[a-zA-Z0-9\-]{20,}'),
    ),
    # passwordMode
    (
        "password",
        r'(?:password|passwd|pwd)\s*[:=]\s*\S+',
        re.compile(r'(?:password|passwd|pwd)\s*[:=]\s*\S+', re.IGNORECASE),
    ),
    # private-keyMode
    (
        "private_key",
        r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    ),
    # MIMO/Together AI token plan keys
    (
        "mimo_key",
        r'tp-[a-zA-Z0-9]{20,}',
        re.compile(r'tp-[a-zA-Z0-9]{20,}'),
    ),
    # connection string patterns
    (
        "connection_mongodb",
        r'mongodb://[^:\s]+:[^@\s]+@',
        re.compile(r'mongodb://[^:\s]+:[^@\s]+@'),
    ),
    (
        "connection_postgres",
        r'postgres(?:ql)?://[^:\s]+:[^@\s]+@',
        re.compile(r'postgres(?:ql)?://[^:\s]+:[^@\s]+@', re.IGNORECASE),
    ),
    (
        "connection_mysql",
        r'mysql://[^:\s]+:[^@\s]+@',
        re.compile(r'mysql://[^:\s]+:[^@\s]+@'),
    ),
    (
        "connection_redis",
        r'redis://:[^@\s]+@',
        re.compile(r'redis://:[^@\s]+@'),
    ),
    # AWSsecretMode
    (
        "aws_access_key",
        r'AKIA[0-9A-Z]{16}',
        re.compile(r'AKIA[0-9A-Z]{16}'),
    ),
    (
        "aws_secret_key",
        r'["\']?aws[_-]?secret[_-]?access[_-]?key["\']?\s*[:=]\s*["\']?[A-Za-z0-9/+=]{40}["\']?',
        re.compile(r'["\']?aws[_-]?secret[_-]?access[_-]?key["\']?\s*[:=]\s*["\']?[A-Za-z0-9/+=]{40}["\']?', re.IGNORECASE),
    ),
    # Bearer token
    (
        "bearer_token",
        r'Bearer\s+[a-zA-Z0-9_\-\.]{20,}',
        re.compile(r'Bearer\s+[a-zA-Z0-9_\-\.]{20,}', re.IGNORECASE),
    ),
]


@dataclass
class RedactionConfig:
    """desensitizeConfig.

    Attributes:
        enabled: whetherenabledesensitize
        replacement_format: replaceFormat,Default [REDACTED_{type}]
        custom_patterns: custom desensitize patterns [(name, pattern_str)]
    """
    enabled: bool = True
    replacement_format: str = "[REDACTED_{type}]"
    custom_patterns: List[Tuple[str, str]] = field(default_factory=list)


# compileafterdesensitizeModecache
_compiled_patterns: Optional[List[Tuple[str, re.Pattern]]] = None
_compiled_lock = threading.Lock()


def _get_compiled_patterns(
    config: Optional[RedactionConfig] = None,
) -> List[Tuple[str, re.Pattern]]:
    """
    getcompileafterdesensitizeModelist.

    Args:
        config: desensitization config (contains custom patterns)

    Returns:
        [(type_name, compiled_pattern)]
    """
    global _compiled_patterns

    if config and config.custom_patterns:
        # has-selfdefineMode,re-compile
        patterns = [
            (name, re.compile(pat))
            for name, pat in config.custom_patterns
        ]
        # addDefaultMode
        for name, _, compiled in _DEFAULT_REDACTION_PATTERNS:
            patterns.append((name, compiled))
        return patterns

    # use cached default patterns
    if _compiled_patterns is None:
        with _compiled_lock:
            if _compiled_patterns is None:
                _compiled_patterns = [
                    (name, compiled)
                    for name, _, compiled in _DEFAULT_REDACTION_PATTERNS
                ]

    return _compiled_patterns


def redact_secrets(
    text: str,
    config: Optional[RedactionConfig] = None,
) -> str:
    """
    autodesensitizein-textsensitiveinfo.

    detect and replace the following patterns:
    - API Key: sk-xxx, AIzaxxx
    - Token: ghp_xxx, glpat-xxx
    - password: password=xxx, passwd=xxx
    - private-key: -----BEGIN PRIVATE KEY-----
    - connection strings: mongodb://user:***@, postgres://user:***@
    - AWSsecret: AKIAxxxx
    - Bearer token: Bearer xxx

    replace with [REDACTED_{type}] format, retain structure, only replace values.

    Args:
        text: text to desensitize
        config: desensitizeConfig

    Returns:
        desensitized text
    """
    cfg = config or RedactionConfig()

    if not cfg.enabled:
        return text

    if not text:
        return text

    patterns = _get_compiled_patterns(cfg)
    result = text

    for type_name, pattern in patterns:
        replacement = cfg.replacement_format.format(type=type_name)
        result = pattern.sub(replacement, result)

    return result


# =============================================================================
# Outbound filtering
# =============================================================================

@dataclass
class OutboundConfig:
    """outboundrequestfilterConfig.

    Attributes:
        url_config: URLsecurityConfig
        domain_whitelist: domainwhitelist(if-empty-then-nolimit)
        domain_blacklist: domainblacklist
        redact_query_params: names of query parameters that require desensitization in URLs
        allowed_methods: allowed HTTP methods
    """
    url_config: URLSafetyConfig = field(default_factory=URLSafetyConfig)
    domain_whitelist: List[str] = field(default_factory=list)
    domain_blacklist: List[str] = field(default_factory=list)
    redact_query_params: List[str] = field(default_factory=lambda: [
        'token', 'key', 'api_key', 'api-key', 'secret',
        'password', 'access_token', 'refresh_token',
        'client_secret', 'private_key',
    ])
    allowed_methods: FrozenSet[str] = frozenset({
        'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS',
    })


def check_outbound_request(
    url: str,
    method: str = "GET",
    config: Optional[OutboundConfig] = None,
) -> Tuple[bool, str]:
    """
    outboundrequestsecurityCheck.

    integrates URL verification + SSRF protection + domain allow/block list.

    Args:
        url: requestURL
        method: HTTPmethod
        config: Outbound filteringConfig

    Returns:
        (allowed, reason): allowed=Trueindicatesallow
    """
    cfg = config or OutboundConfig()

    # HTTPmethodCheck
    method_upper = method.upper()
    if method_upper not in cfg.allowed_methods:
        return False, f"disallowed HTTP method: {method}"

    # URLsecurityCheck(SSRFProtection)
    safe, reason = validate_url(url, cfg.url_config)
    if not safe:
        return False, f"URL security check failed: {reason}"

    # extradomainwhitelistCheck
    if cfg.domain_whitelist:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if hostname:
                hostname_lower = hostname.lower()
                allowed = False
                for wl in cfg.domain_whitelist:
                    if hostname_lower == wl.lower() or hostname_lower.endswith('.' + wl.lower()):
                        allowed = True
                        break
                if not allowed:
                    return False, f"domain not in outbound whitelist: {hostname}"
        except Exception as e:
            logger.debug(f"[SecurityBaseline] unknown exception (non-fatal): {e}")
            pass

    # extradomainblacklistCheck
    if cfg.domain_blacklist:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if hostname:
                hostname_lower = hostname.lower()
                for bl in cfg.domain_blacklist:
                    if hostname_lower == bl.lower() or hostname_lower.endswith('.' + bl.lower()):
                        return False, f"domain in outbound blacklist: {hostname}"
        except Exception as e:
            logger.debug(f"[SecurityBaseline] unknown exception (non-fatal): {e}")
            pass

    return True, ""


def redact_url_params(url: str, params_to_redact: Optional[List[str]] = None) -> str:
    """
    desensitizeURLinsensitivequeryParameter.

    replace token=xxx&key=yyy with token=[REDACTED]&key=[REDACTED]

    Args:
        url: original URL
        params_to_redact: list of parameter names requiring desensitization

    Returns:
        desensitized URL
    """
    if not params_to_redact:
        params_to_redact = [
            'token', 'key', 'api_key', 'api-key', 'secret',
            'password', 'access_token', 'refresh_token',
        ]

    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url

        # parsequeryParameter
        from urllib.parse import parse_qs, urlencode, urlunparse

        params = parse_qs(parsed.query, keep_blank_values=True)
        redacted = False

        for key in list(params.keys()):
            key_lower = key.lower()
            if any(p in key_lower for p in [p.lower() for p in params_to_redact]):
                params[key] = ['[REDACTED]']
                redacted = True

        if redacted:
            # rebuildURL
            new_query = urlencode(params, doseq=True)
            return urlunparse(parsed._replace(query=new_query))

    except Exception as e:
        logger.debug(f"[Security] URL param redaction error: {e}")

    return url


# =============================================================================
# sandboxintegrationauxiliary
# =============================================================================

def sandbox_path_check(
    path: str,
    allowed_dirs: List[str],
) -> Tuple[bool, str]:
    """
    path safety check before sandbox execution (simplified interface).

    Args:
        path: path to check
        allowed_dirs: list of allowed directories

    Returns:
        (safe, reason)
    """
    config = PathSafetyConfig(
        allowed_dirs=allowed_dirs,
        deny_symlinks=True,
        deny_absolute=True,
    )
    return validate_path_safety(path, allowed_dirs, config)


def sandbox_network_check(
    url: str,
    allowed_domains: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """
    sandboxinnernetworkrequestsecurityCheck(simplifyinterface).

    Args:
        url: requestURL
        allowed_domains: list of allowed domains

    Returns:
        (allowed, reason)
    """
    url_config = URLSafetyConfig(
        domain_whitelist=allowed_domains or [],
        allow_private_ip=False,
    )
    outbound_config = OutboundConfig(
        url_config=url_config,
        domain_whitelist=allowed_domains or [],
    )
    return check_outbound_request(url, "GET", outbound_config)


def sandbox_output_redact(output: str) -> str:
    """
    sandboxoutputautodesensitize(simplifyinterface).

    Args:
        output: sandboxoutputtext

    Returns:
        desensitizeafteroutput
    """
    return redact_secrets(output)
