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

"""OSV.dev supply chain vulnerability scanner distilled from SkillSpector.

Parses requirements.txt and pyproject.toml for Python dependencies,
queries the OSV.dev batch API for known vulnerabilities, and maps
findings to the EITElite AuditResult / Finding types.

Uses only stdlib (urllib, tomllib, re, time, dataclasses) - no
additional dependencies required.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from .types import AuditResult, Finding


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
_OSV_VULN_URL = "https://api.osv.dev/v1/vulns"
_REQUEST_TIMEOUT = 10  # seconds

_CACHE_TTL_SECS = 3600.0  # 1 hour

# Severity labels in descending order of severity
_SEVERITY_LABELS = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW"})


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VulnResult:
    """A single vulnerability found for a package."""

    vuln_id: str
    summary: str
    severity: str
    aliases: tuple[str, ...]


# ---------------------------------------------------------------------------
# In-memory cache: (normalized_name, version) -> (timestamp, list[VulnResult])
# ---------------------------------------------------------------------------

_cache: dict[tuple[str, str | None], tuple[float, list[VulnResult]]] = {}


def _normalize_pkg_name(name: str) -> str:
    """Normalize a Python package name per PEP 503 (lowercase, hyphens)."""
    return name.lower().replace("_", "-").replace(".", "-").strip()


def _cache_key(name: str, version: str | None) -> tuple[str, str | None]:
    return (_normalize_pkg_name(name), version)


def _get_cached(key: tuple[str, str | None]) -> list[VulnResult] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, results = entry
    if (time.monotonic() - ts) > _CACHE_TTL_SECS:
        del _cache[key]
        return None
    return results


def _put_cache(
    key: tuple[str, str | None], results: list[VulnResult]
) -> None:
    _cache[key] = (time.monotonic(), results)


def clear_cache() -> None:
    """Clear the in-memory vulnerability cache."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Dependency file parsing
# ---------------------------------------------------------------------------

# Regex for requirements.txt lines:  package[extras] comparison version
_RE_REQ_LINE = re.compile(
    r"^\s*"
    r"([a-zA-Z0-9][\w.\-]*)"
    r"(?:\[[^\]]*\])?\s*"
    r"(?:(>=|==|!=|<=|>|<|~=)\s*([a-zA-Z0-9.*\-_]+))?"
    r"\s*(?:#.*)?$",
    re.IGNORECASE,
)


def _parse_requirements_txt(content: str) -> list[tuple[str, str | None]]:
    """Parse a requirements.txt string into (name, version_or_None) pairs.

    Returns the pinned version for ``==``, the lower bound for ``>=``,
    or *None* when no version is specified.
    """
    deps: list[tuple[str, str | None]] = []
    for line in content.splitlines():
        line_stripped = line.strip()
        if (
            not line_stripped
            or line_stripped.startswith("#")
            or line_stripped.startswith("-")
        ):
            continue
        m = _RE_REQ_LINE.match(line_stripped)
        if m:
            name = _normalize_pkg_name(m.group(1))
            op = m.group(2)
            ver = m.group(3)
            if op == "==" and ver:
                deps.append((name, ver))
            elif op and ver:
                deps.append((name, ver))
            else:
                deps.append((name, None))
    return deps


# PEP 508 dependency string regex (name + optional version specifier)
_RE_PEP508 = re.compile(
    r"^\s*"
    r"([a-zA-Z0-9][\w.\-]*(?:\s*\[[^\]]*\])?)"
    r"\s*"
    r"(?:"
    r"(>=|==|!=|<=|>|<|~=)\s*([a-zA-Z0-9.\-*_]+)"
    r"(?:\s*,\s*(?:>=|==|!=|<=|>|<|~=)\s*[a-zA-Z0-9.\-*_]+)*"
    r")?\s*$",
    re.IGNORECASE,
)


def _parse_pep508_deps(raw: list[str]) -> list[tuple[str, str | None]]:
    """Parse a list of PEP 508 dependency strings."""
    deps: list[tuple[str, str | None]] = []
    for dep_str in raw:
        dep_str_stripped = dep_str.strip()
        if not dep_str_stripped or dep_str_stripped.startswith("#"):
            continue
        m = _RE_PEP508.match(dep_str_stripped)
        if m:
            name_raw = m.group(1).split("[")[0].strip()
            name = _normalize_pkg_name(name_raw)
            op = m.group(2)
            ver = m.group(3)
            if op == "==" and ver:
                deps.append((name, ver))
            elif op and ver:
                deps.append((name, ver))
            else:
                deps.append((name, None))
    return deps


def _parse_pyproject_toml(content: str) -> list[tuple[str, str | None]]:
    """Parse pyproject.toml dependencies into (name, version_or_None)."""
    import tomllib

    try:
        data = tomllib.loads(content)
    except Exception:
        return []

    deps: list[tuple[str, str | None]] = []

    project = data.get("project", {})
    raw_deps = project.get("dependencies", [])
    if isinstance(raw_deps, list):
        deps.extend(_parse_pep508_deps(raw_deps))

    optional_deps = project.get("optional-dependencies", {})
    if isinstance(optional_deps, dict):
        for group_deps in optional_deps.values():
            if isinstance(group_deps, list):
                deps.extend(_parse_pep508_deps(group_deps))

    return deps


# ---------------------------------------------------------------------------
# CVSS severity estimation (adapted from SkillSpector, no CVSS lib needed)
# ---------------------------------------------------------------------------

_CVSS_VECTOR_RE = re.compile(r"CVSS:[34][.\d]*/(.+)")

_CVSS_HIGH_METRICS: set[str] = {
    "AV:N",
    "AC:L",
    "PR:N",
    "UI:N",
    "S:C",
    "C:H",
    "I:H",
    "A:H",
    "AT:N",
    "VC:H",
    "VI:H",
    "VA:H",
    "SC:H",
    "SI:H",
    "SA:H",
}


def _estimate_cvss_severity(vector: str) -> str | None:
    """Estimate severity from a CVSS v3 or v4 vector string.

    Counts how many base metrics are at their most-severe value.
    Provides a reasonable triage approximation without a CVSS library.
    """
    m = _CVSS_VECTOR_RE.match(vector)
    if not m:
        return None
    metrics = m.group(1).split("/")
    high_count = sum(1 for metric in metrics if metric in _CVSS_HIGH_METRICS)
    total = len(metrics)
    if total == 0:
        return None
    ratio = high_count / total
    if ratio >= 0.75:
        return "CRITICAL"
    if ratio >= 0.5:
        return "HIGH"
    if ratio >= 0.25:
        return "MEDIUM"
    return "LOW"


def _severity_from_vuln(vuln: dict) -> str:
    """Extract the highest severity string from an OSV vulnerability object.

    Priority:
    1. ``database_specific.severity`` - GHSA sets this reliably.
    2. ``affected[].ecosystem_specific.severity``.
    3. ``severity[].score`` CVSS vector (parsed to estimate band).
    4. Default ``HIGH`` when no info is available.
    """
    db_specific = vuln.get("database_specific", {})
    ghsa_severity = db_specific.get("severity", "")
    if ghsa_severity:
        return ghsa_severity.upper()
    for affected in vuln.get("affected", []):
        eco_specific = affected.get("ecosystem_specific", {})
        sev = eco_specific.get("severity", "")
        if sev:
            return sev.upper()
    for severity_entry in vuln.get("severity", []):
        score_str = severity_entry.get("score", "")
        if score_str:
            estimated = _estimate_cvss_severity(score_str)
            if estimated:
                return estimated
    return "HIGH"


def _parse_vuln(vuln: dict) -> VulnResult:
    """Convert an OSV vulnerability dict to a VulnResult."""
    aliases = tuple(vuln.get("aliases", []))
    return VulnResult(
        vuln_id=vuln.get("id", "UNKNOWN"),
        summary=vuln.get("summary", vuln.get("details", "")[:200]),
        severity=_severity_from_vuln(vuln),
        aliases=aliases,
    )


def _build_query(name: str, version: str | None) -> dict:
    """Build an OSV batch query dict for a single package."""
    q: dict = {"package": {"name": name, "ecosystem": "PyPI"}}
    if version:
        q["version"] = version
    return q


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib urllib only)
# ---------------------------------------------------------------------------


def _urllib_post_json(url: str, data: dict) -> dict | None:
    """POST JSON via urllib, return parsed dict or None on error."""
    try:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError):
        return None


def _urllib_get_json(url: str) -> dict | None:
    """GET JSON via urllib, return parsed dict or None on error."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# SupplyChainAnalyzer
# ---------------------------------------------------------------------------


class SupplyChainAnalyzer:
    """Scan Python dependency files for known CVEs via OSV.dev.

    Parses ``requirements.txt`` (pip) and ``pyproject.toml`` (PEP 508)
    dependency declarations, queries the OSV.dev batch API for matching
    vulnerabilities, and returns findings mapped to the EITElite
    :class:`AuditResult` / :class:`Finding` types.

    Uses **only stdlib** - ``urllib`` for HTTP, ``tomllib`` for TOML.
    Results are cached in-memory for 1 hour. Network errors are silently
    ignored (empty findings for unreachable packages).
    """

    def __init__(self) -> None:
        # Optional injected HTTP client for testing (httpx-compatible interface)
        self._http_client: Any = None

    # ── Dependency injection ──────────────────────────────────────────

    def set_httpx_client(self, client: object) -> None:
        """Inject a custom HTTP client for testing.

        The injected client must expose ``.post(url, json=..., timeout=...)``
        returning an object with ``.raise_for_status()`` and ``.json()``
        methods. When set, this replaces the default urllib-based HTTP.
        """
        self._http_client = client

    # ── Public API ────────────────────────────────────────────────────

    def analyze(self, content: str, file_path: str = "") -> AuditResult:
        """Analyze dependency file content for known vulnerabilities.

        Detects file format from ``file_path`` extension:
          - ``.txt``  -> ``requirements.txt`` (pip)
          - ``.toml`` -> ``pyproject.toml`` (PEP 517)
          - empty / unknown -> ``requirements.txt``-style parsing

        Args:
            content: Raw file content as a string.
            file_path: Path hint used to determine format.

        Returns:
            :class:`AuditResult` with one :class:`Finding` per detected
            vulnerability.
        """
        start = time.monotonic()

        deps = self._parse_deps(content, file_path)
        if not deps:
            return AuditResult(
                findings=[],
                analyzer="supply_chain",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        vuln_results = self._query_osv_batch(deps)
        findings: list[Finding] = []
        for (name, version), vulns in zip(deps, vuln_results):
            for vuln in vulns:
                findings.append(
                    self._vuln_to_finding(vuln, file_path, name, version)
                )

        return AuditResult(
            findings=findings,
            analyzer="supply_chain",
            duration_ms=(time.monotonic() - start) * 1000,
        )

    def scan_path(self, directory: str) -> AuditResult:
        """Walk a directory looking for dependency files and scan them.

        Checks for ``requirements.txt`` and ``pyproject.toml`` at the
        top level of the given directory.

        Args:
            directory: Path to scan.

        Returns:
            Combined :class:`AuditResult` across all files found.
        """
        start = time.monotonic()
        all_findings: list[Finding] = []

        for filename in ("requirements.txt", "pyproject.toml"):
            filepath = os.path.join(directory, filename)
            if os.path.isfile(filepath):
                try:
                    with open(
                        filepath, "r", encoding="utf-8", errors="replace"
                    ) as f:
                        content = f.read()
                except OSError:
                    continue
                result = self.analyze(content, file_path=filepath)
                all_findings.extend(result.findings)

        return AuditResult(
            findings=all_findings,
            analyzer="supply_chain",
            duration_ms=(time.monotonic() - start) * 1000,
        )

    @classmethod
    def scan_file(cls, path: str) -> AuditResult:
        """Scan a single dependency file by path, detecting type from extension.

        Args:
            path: Path to ``requirements.txt`` or ``pyproject.toml``.

        Returns:
            :class:`AuditResult` with vulnerability findings, or an error
            result if the file cannot be read or the extension is unsupported.
        """
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".txt", ".toml"):
            return AuditResult(
                findings=[],
                analyzer="supply_chain",
                error=f"Unsupported file extension: {ext}",
            )
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as exc:
            return AuditResult(
                findings=[],
                analyzer="supply_chain",
                error=str(exc),
            )
        return cls().analyze(content, file_path=path)

    # ── Internal helpers ──────────────────────────────────────────────

    def _parse_deps(
        self, content: str, file_path: str
    ) -> list[tuple[str, str | None]]:
        """Parse dependencies from file content based on extension."""
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".toml":
            return _parse_pyproject_toml(content)
        return _parse_requirements_txt(content)

    def _query_osv_batch(
        self, deps: list[tuple[str, str | None]]
    ) -> list[list[VulnResult]]:
        """Query OSV.dev batch API, checking cache first.

        Returns a list parallel to *deps* - one entry per dependency,
        each a (possibly empty) list of VulnResult.
        """
        if not deps:
            return []

        all_results: list[list[VulnResult]] = [[] for _ in deps]

        # Check cache for each dependency
        uncached_indices: list[int] = []
        uncached_queries: list[dict] = []

        for i, (name, version) in enumerate(deps):
            key = _cache_key(name, version)
            cached = _get_cached(key)
            if cached is not None:
                all_results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_queries.append(_build_query(name, version))

        if not uncached_queries:
            return all_results

        # Query OSV batch API for uncached entries
        batch_response = self._do_osv_batch(uncached_queries)
        if batch_response is None:
            # Network error - return whatever we have from cache
            return all_results

        batch_results = batch_response.get("results", [])
        for batch_idx, idx in enumerate(uncached_indices):
            if batch_idx >= len(batch_results):
                break
            vulns_raw = batch_results[batch_idx].get("vulns", [])
            if not vulns_raw:
                name, version = deps[idx]
                _put_cache(_cache_key(name, version), [])
                continue

            vuln_ids = [v["id"] for v in vulns_raw if "id" in v]
            if self._http_client is not None:
                vuln_details = self._fetch_vuln_details_injected(vuln_ids)
            else:
                vuln_details = _fetch_vuln_details(vuln_ids)
            all_results[idx] = vuln_details

            name, version = deps[idx]
            _put_cache(_cache_key(name, version), vuln_details)

        return all_results

    def _do_osv_batch(self, queries: list[dict]) -> dict | None:
        """Execute OSV batch query using injected client or urllib."""
        if self._http_client is not None:
            try:
                resp = self._http_client.post(
                    _OSV_BATCH_URL,
                    json={"queries": queries},
                    timeout=_REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception:
                return None
        return _urllib_post_json(_OSV_BATCH_URL, {"queries": queries})

    def _fetch_vuln_details_injected(
        self, vuln_ids: list[str]
    ) -> list[VulnResult]:
        """Fetch vuln details using the injected HTTP client."""
        if not vuln_ids:
            return []
        results: list[VulnResult] = []
        for vid in vuln_ids[:10]:
            try:
                resp = self._http_client.get(  # type: ignore[union-attr]
                    f"{_OSV_VULN_URL}/{vid}"
                )
                resp.raise_for_status()
                results.append(_parse_vuln(resp.json()))
            except Exception:
                results.append(VulnResult(vid, "", "HIGH", ()))
        return results

    @staticmethod
    def _vuln_to_finding(
        vuln: VulnResult,
        file_path: str,
        dep_name: str,
        dep_version: str | None,
    ) -> Finding:
        """Convert a :class:`VulnResult` to a EITElite :class:`Finding`."""
        version_info = f" {dep_version}" if dep_version else ""
        msg_parts: list[str] = [f"[{vuln.vuln_id}]"]
        if vuln.summary:
            msg_parts.append(vuln.summary)
        if vuln.aliases:
            msg_parts.append(f"aliases: {', '.join(vuln.aliases)}")
        message = " \u2014 ".join(msg_parts)

        return Finding(
            rule_id=f"SC-{vuln.vuln_id}",
            message=message,
            severity=vuln.severity,
            file_path=file_path,
            line=0,
            confidence=1.0,
            context=f"dependency: {dep_name}{version_info}",
            matched_text=f"{dep_name} {dep_version or '*'}",
            tags=["supply_chain", "cve", vuln.severity.lower()],
        )


def _fetch_vuln_details(vuln_ids: list[str]) -> list[VulnResult]:
    """Fetch full vulnerability details for a list of OSV IDs (urllib).

    Limits to the first 10 IDs to avoid excessive API calls.
    Returns empty VulnResult entries on fetch failures.
    """
    if not vuln_ids:
        return []
    results: list[VulnResult] = []
    for vid in vuln_ids[:10]:
        vuln_data = _urllib_get_json(f"{_OSV_VULN_URL}/{vid}")
        if vuln_data is not None:
            try:
                results.append(_parse_vuln(vuln_data))
            except Exception:
                results.append(VulnResult(vid, "", "HIGH", ()))
        else:
            results.append(VulnResult(vid, "", "HIGH", ()))
    return results
