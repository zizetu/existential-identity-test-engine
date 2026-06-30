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

#!/usr/bin/env python3
"""
web_sense.py - tical-code v0.7 web-sense primitive

Agent's eyes: fetch web pages and extract structured body text content.
This is not a mode, but rather the Agent's sense-the-internet primitive.

Capabilities:
- HTML→Plain text extraction (denoise/strip tags)
- Auto encoding detection (progressive decoding)
- SSRF protection (forbid internal network access)
- robots.txt compliance
- gzip decompress / redirect loop detection
"""

import gzip
import ipaddress
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from typing import Dict, List, Optional

# ============ Config ============

FETCH_TIMEOUT = int(os.environ.get("TICAL_FETCH_TIMEOUT", "15"))
FETCH_MAX_CONTENT = int(os.environ.get("TICAL_FETCH_MAX", "512000"))  # 500KB
FETCH_MAX_RETRIES = 3
FETCH_MAX_REDIRECTS = 5
FETCH_USER_AGENT = os.environ.get(
    "TICAL_FETCH_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ============ SSRF Protection ============

def _is_private_ip(ip_str: str) -> bool:
    """Check whether the IP is a private/internal network address. Uses stdlib ipaddress."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_reserved
    except ValueError:
        return True  # invalid IP treated as internal

def _check_ssrf(url: str) -> None:
    """SSRF protection: forbid fetching internal network addresses.

    Args:
        url: URL to fetch

    Raises:
        ValueError: URL points to an internal network address
    """
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"invalid URL: {url}")
    try:
        ip = socket.gethostbyname(hostname)
        if _is_private_ip(ip):
            raise ValueError(f"SSRF protection: Forbidden access to internal address {ip} ({hostname})")
    except socket.gaierror as e:
        # P1-7: DNS resolution failure = block, not pass.
        # Silent pass was an SSRF risk via DNS rebinding - unresolvable
        # hostnames could bypass the IP check entirely.
        raise ValueError(f"SSRF protection: DNS resolution failed for {hostname} ({e})")

# ============ robots.txt cache ============

_robots_cache: Dict[str, urllib.robotparser.RobotFileParser] = {}

def _check_robots(url: str) -> bool:
    """Check robots.txt to see whether fetching is allowed. Results cached by domain.

    Returns:
        True indicates fetching is allowed
    """
    parsed = urllib.parse.urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"

    if domain not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{domain}/robots.txt")
        try:
            rp.read()
        except Exception:
            pass  # robots.txt unreadable - allow by default
        _robots_cache[domain] = rp

    return _robots_cache[domain].can_fetch(FETCH_USER_AGENT, url)

# ============ HTML Parser ============

class _TextExtractor(HTMLParser):
    """Extract plain text and links from HTML. Ignores noise tags like script/style/nav, etc."""

    IGNORE_TAGS = frozenset({
        "script", "style", "nav", "header", "footer",
        "aside", "noscript", "head"
    })
    BLOCK_TAGS = frozenset({
        "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "td", "tr", "article", "section", "blockquote", "pre"
    })
    NOISE_KEYWORDS = frozenset({
        "homepage", "login", "register", "copyrightall", "copyright", "ICP_record",
        "contactwe", "aboutwe", "site map", "return-totop"
    })

    def __init__(self):
        super().__init__()
        self._chunks: List[str] = []
        self._ignore_depth: int = 0
        self._in_title: bool = False
        self._title: str = ""
        self._links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]):
        # title special handling: extract title even inside <head>, don't add ignore_depth
        if tag == "title":
            self._in_title = True
            return
        if tag in ("meta", "link"):
            return  # self-closing tags have no end tag, don't add depth
        if tag in self.IGNORE_TAGS:
            self._ignore_depth += 1
        elif tag == "a" and self._ignore_depth == 0:
            for name, value in attrs:
                if name == "href" and value and (value.startswith("http") or value.startswith("/")):
                    self._links.append(value)

    def handle_endtag(self, tag: str):
        # title special handling
        if tag == "title":
            self._in_title = False
            return
        if tag in self.IGNORE_TAGS:
            self._ignore_depth = max(0, self._ignore_depth - 1)
        elif tag in self.BLOCK_TAGS and self._ignore_depth == 0:
            self._chunks.append("\n")

    def handle_data(self, data: str):
        # title data collected even inside <head>, unaffected by ignore_depth
        if self._in_title:
            text = data.strip()
            if text:
                self._title += text
            return
        if self._ignore_depth > 0:
            return
        text = data.strip()
        if text:
            if self._in_title:
                self._title += text
            else:
                self._chunks.append(text)

    @property
    def title(self) -> str:
        return self._title.strip()

    @property
    def links(self) -> List[str]:
        return list(dict.fromkeys(self._links))  # deduplicate order-preserving

    def get_text(self) -> str:
        """Get denoised body text."""
        raw = "".join(self._chunks)
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        # denoise: discard short lines containing noise keywords
        cleaned = []
        for line in lines:
            if len(line) < 20 and any(kw in line for kw in self.NOISE_KEYWORDS):
                continue
            cleaned.append(line)
        # merge consecutive blank lines
        text = "\n".join(cleaned)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

# ============ Encoding detection ============

def _detect_encoding(headers, raw: bytes) -> str:
    """Progressive encoding detection: Content-Type → meta charset → UTF-8 → GBK → latin-1."""
    # 1) Content-Type header
    ct = headers.get("Content-Type", "")
    m = re.search(r'charset=([^\s;]+)', ct, re.I)
    if m:
        enc = m.group(1).strip('"\'')
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            pass
    # 2) <meta charset>
    head = raw[:8192].decode("ascii", errors="ignore")
    m = re.search(r'<meta[^>]+charset=["\']?([^"\'\\s;>]+)', head, re.I)
    if m:
        enc = m.group(1)
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            pass
    # 3) UTF-8 → GBK → latin-1
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"  # fallback, using errors='replace'

# ============ Core primitive ============

def web_fetch(url: str, timeout: int = FETCH_TIMEOUT,
              max_content: int = FETCH_MAX_CONTENT) -> dict:
    """Web-sense primitive: fetch a URL and extract structured body text.

    Args:
        url: target URL
        timeout: timeout in seconds
        max_content: maximum bytes to read

    Returns:
        {title, url, content, links, content_length, fetch_time_ms, status}
        status: ok / truncated / error / forbidden / blocked_by_robots / redirect_loop
    """
    start = time.monotonic()

    # SSRF check
    try:
        _check_ssrf(url)
    except ValueError as e:
        return {"title": "", "url": url, "content": "", "links": [],
                "content_length": 0, "fetch_time_ms": 0, "status": "forbidden",
                "error": str(e)}

    # robots.txt check
    if not _check_robots(url):
        return {"title": "", "url": url, "content": "", "links": [],
                "content_length": 0, "fetch_time_ms": 0, "status": "blocked_by_robots"}

    headers = {
        "User-Agent": FETCH_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    visited = set()
    current_url = url
    last_err = None

    for attempt in range(FETCH_MAX_RETRIES):
        try:
            req = urllib.request.Request(current_url, headers=headers, method="GET")
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:

                # redirect handling
                redirects = 0
                while redirects < FETCH_MAX_REDIRECTS:
                    final_url = resp.url if hasattr(resp, 'url') else resp.geturl()
                    if final_url == current_url:
                        break
                    if final_url in visited:
                        elapsed = int((time.monotonic() - start) * 1000)
                        return {"title": "", "url": url, "content": "", "links": [],
                                "content_length": 0, "fetch_time_ms": elapsed,
                                "status": "redirect_loop"}
                    visited.add(current_url)
                    current_url = final_url
                    req = urllib.request.Request(current_url, headers=headers, method="GET")
                    resp.close()  # close previous response to prevent leak
                    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
                    redirects += 1

                # read content (truncate if needed)
                raw = resp.read(max_content + 1)
                truncated = len(raw) > max_content
                if truncated:
                    raw = raw[:max_content]

                # gzip decompress
                if resp.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass  # non-gzip data - use as-is

                # encoding detection
                encoding = _detect_encoding(resp.headers, raw)
                content = raw.decode(encoding, errors="replace")

                # extract body text
                extractor = _TextExtractor()
                try:
                    extractor.feed(content)
                    text = extractor.get_text()
                except Exception:
                    text = re.sub(r'<[^>]+>', '', content)  # fallback: simple tag stripping

                elapsed = int((time.monotonic() - start) * 1000)

                return {
                    "title": extractor.title,
                    "url": current_url,
                    "content": text,
                    "links": extractor.links,
                    "content_length": len(text),
                    "fetch_time_ms": elapsed,
                    "status": "truncated" if truncated else "ok",
                }

        except urllib.error.HTTPError as e:
            if str(e.code).startswith("4"):
                elapsed = int((time.monotonic() - start) * 1000)
                return {"title": "", "url": url, "content": "", "links": [],
                        "content_length": 0, "fetch_time_ms": elapsed,
                        "status": "error", "error": f"HTTP {e.code}"}
            last_err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e

        # retry (exponential backoff)
        if attempt < FETCH_MAX_RETRIES - 1:
            wait = min(2 ** attempt, 8)
            time.sleep(wait)

    elapsed = int((time.monotonic() - start) * 1000)
    return {"title": "", "url": url, "content": "", "links": [],
            "content_length": 0, "fetch_time_ms": elapsed,
            "status": "error", "error": str(last_err)}
