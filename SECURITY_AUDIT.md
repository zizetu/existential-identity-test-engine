# Security Audit Report: eite-agent (LITE Profile)

**Audit date:** 2026-07-10
**Codebase:** 118 .py files, ~57K LOC
**Repository:** /home/ubuntu/eite-agent

---

## 1. SSRF (Server-Side Request Forgery)

### ✅ Strengths
- **security_baseline.py** (`_check_ssrf`, `validate_url`): Excellent — full CIDR-based private IP blocking, DNS rebinding detection, dangerous scheme blocking (file://, gopher://, dict://, etc.), domain allow/block lists, configurable.
- **builtin_tools.py**: Inline SSRF checks in `http_get_handler`/`http_post_handler` using regex patterns for private IP ranges.
- **channel.py** (`_ssrf_guard`): Delegates to `security_baseline._check_ssrf()`.

### ⚠️ Gaps
1. **channel.py explicitly allows localhost** (lines 51-53): `_ssrf_guard()` permits `localhost`, `127.0.0.1`, `::1` unconditionally for internal service communication (TicalChat). If an attacker can control URLs processed by the Telegram channel, they could probe internal services.
2. **builtin_tools.py SSRF is weaker than security_baseline.py**: The inline checks in `http_get_handler`/`http_post_handler` (lines 871-876, 955-960) use prefix string matching (`hostname.startswith('127.')`) instead of proper CIDR. **No DNS rebinding protection** — they don't resolve hostnames to check for private IPs behind domain names. No dangerous scheme blocking.
3. **No mutual exclusion**: builtin_tools.py has its own SSRF code that does NOT call security_baseline's `_check_ssrf()`. Two different SSRF implementations with different rigor levels.

---

## 2. Path Traversal

### ✅ Strengths
- **security_baseline.py**: `validate_path_safety()` and `resolve_and_validate()` — full TOCTOU protection with `_path_lock` (threading.Lock), symlink detection via `_contains_symlink()`, realpath resolution, allowed directory boundary checks, max path length enforcement.
- **builtin_tools.py**: `SecurityContext.is_dir_allowed()` uses `os.path.realpath()` to resolve symlinks. `SYSTEM_DIRS_BLOCKED` protects critical system paths.
- **tool_executor.py**: `_workspace_path()` resolves paths and validates workspace boundaries. `_path_allowed()` checks against workspace + emergency memory dirs.

### ⚠️ Gaps
1. **builtin_tools.py `read_file_handler` has TOCTOU window** (lines 591-603): `is_dir_allowed()` is called BEFORE opening the file, but no lock prevents a symlink swap between the check and the `open()` call.
2. **builtin_tools.py `write_file_handler` same issue** (lines 648-677): Security check then file write are not atomic.
3. **builtin_tools.py has TWO path validation functions** (`_is_safe_path` and `SecurityContext.is_dir_allowed`), used inconsistently across handlers — potential for one to be bypassed if a handler uses the wrong one.

---

## 3. Auth Bypass

### ⚠️ Critical Issues

1. **[CMD] Protocol: No auth by default** (`message_handler.py` line 489-491):
   ```python
   _CMD_AUTH_SECRET = os.environ.get("CMD_AUTH_SECRET", "")
   if not _CMD_AUTH_SECRET:
       logger.warning("[CMD] CMD_AUTH_SECRET not set — all [CMD] messages accepted without HMAC authentication")
   ```
   **Without CMD_AUTH_SECRET, any sender can issue [CMD] commands.** The only protection is sender ID matching.

2. **[CMD] Telegram/WeChat auto-level** (`message_handler.py` lines 552-553):
   ```python
   if msg.source in ("telegram", "weixin"):
       return CMD_LEVEL_WORKER
   ```
   **Any Telegram or WeChat user automatically gets WORKER permission level** — can run `ping`, `help`, `escalate`, `restart`, `log`, `context` commands.

3. **[CMD] Default MASTER_IDS** (`message_handler.py` line 467):
   ```python
   MASTER_IDS = set(_MASTER_IDS_ENV.split(",")) if _MASTER_IDS_ENV else {"admin"}
   ```
   Default master ID is `"admin"` — trivial to guess.

4. **[CMD] `permission` command allows mode toggling at MASTER level:** The `permission` command can put the system into `bypassPermissions` mode (line 791-800), disabling all permission checking.

5. **API server auth (eite_api.py):** Properly fail-secure — if `EITE_API_KEY` is unset, all requests are rejected. Server binds to 127.0.0.1:8080 only.

---

## 4. TOCTOU (Time-of-Check Time-of-Use)

### ✅ Strengths
- **security_baseline.py**: Threading lock guards path validation.
- **tool_executor.py**: `_atomic_write_json()` uses tempfile + os.rename. `exec_file_write()` uses atomic write for .py files.
- **message_handler.py**: Atomic write via `os.replace()` for password file saves.

### ⚠️ Gaps
1. **builtin_tools.py**: `read_file_handler` (line 592-603) and `write_file_handler` (line 649-677) have TOCTOU windows between path check and I/O operation, as noted above.
2. **search_files_handler** (lines 1037-1058): Directory access check, then `os.walk()` — no lock between check and traversal.

---

## 5. SQL Injection

**Not applicable.** No SQL databases in the LITE profile. All state is JSON-file based.

---

## 6. Command Injection

### ✅ Strengths
- **builtin_tools.py** `shell_exec_handler()`: Command whitelist (`SHELL_ALLOWED_COMMANDS`), dangerous pattern blocking (`SHELL_BLOCKED_PATTERNS`), `shlex.split()` + `shell=False`, shell operator blocking (`;`, `||`, `` ` ``, `$()`).
- **tool_executor.py** `_bash_safety_check()`: `BASH_BLACKLIST` with 20+ regex patterns, workspace boundary checks, env-var-leak detection, admin command exceptions.
- Both paths use `shlex.split()` then `subprocess.run(cmd_parts, shell=False)` for simple commands.

### ⚠️ Critical Gap
1. **tool_executor.py `_run_cmd()` temp file execution** (lines 508-529):
   ```python
   if _needs_shell:
       fd, sh_path = tempfile.mkstemp(suffix='.sh', prefix='tc_')
       with os.fdopen(fd, 'w') as sh_f:
           sh_f.write('#!/bin/sh\n')
           sh_f.write(cmd + '\n')
       os.chmod(sh_path, 0o700)
       r = subprocess.run([sh_path], ...)
   ```
   Commands containing shell operators (`|`, `&&`, `||`, `<`, `>`, `;`, `$()`, backticks) are written to a temp shell script and executed as a subprocess. **This is effectively shell execution** with all the risks of shell metacharacters. While the blacklist provides some protection, any regex bypass in `BASH_BLACKLIST` leads directly to arbitrary shell execution.

2. **builtin_tools.py** `_validate_shell_command()` command whitelist (line 1170) checks only the **first word** of the command. An attacker could use `ls; rm -rf /` — the `;` check happens before the whitelist check (lines 1139-1142), so `ls;...` is caught. But `ls ; rm -rf /` (spaces around `;`) is also caught.

3. **builtin_tools.py whitelist includes dangerous commands**: `curl`, `wget`, `python3`, `docker`, `npm`, `node`, `pip3` are in the whitelist. While patterns block `curl|sh`, `pip install` etc., there are edge cases (e.g., `python3 -c "..."` could execute arbitrary code).

---

## 7. Race Conditions

### ⚠️ Issues
1. **Global mutable singletons**: `_security_context`, `_rate_limiter`, `_cron_manager`, `_memory_store` in builtin_tools.py are module-level globals modifiable by any concurrent session. No thread-level isolation between parallel tool executions.
2. **RateLimiter**: Uses `asyncio.Lock()` (builtin_tools.py) or `threading.Lock()` (tool_executor.py) — correct but only at the individual limiter level.
3. **Concurrent tool execution**: tool_executor.py has a `TOOL_CONCURRENCY_MAP` (line 206-222) that correctly classifies read vs write tools. Read tools execute in parallel, write tools flush and run sequentially. This is correct.
4. **No file-level locking**: Multiple worker instances (mesh nodes) could write to shared JSON files without inter-process locking.

---

## 8. WebSocket Origin Validation

**Not applicable.** No WebSocket server, client, or connection code exists in this LITE profile. The aiohttp API server (server.py) is HTTP-only.

---

## 9. Hardcoded Secrets

### ✅ No hardcoded credentials found
- All API keys, tokens, and passwords are read from environment variables at runtime.
- Config files (providers.json, default.json, mesh.json, mcp_servers.json) contain only templates/placeholders (${VAR} syntax), no actual secrets.
- The password system in message_handler.py uses SHA-256 + salted hashes stored in a JSON file (not git-tracked).
- No actual TG_BOT_TOKEN, DEEPSEEK_API_KEY, or other credential values appear in any source file.

### Minor
- **orthos_chain.py** (line 72): `DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")` — hardcoded env var name, but that's expected.

---

## 10. Additional Findings

### A. Three Inconsistent Security Layers
The codebase has **four** separate security implementations that don't always agree:
1. `security_baseline.py` (canonical, most thorough) — used by `channel.py`
2. `builtin_tools.py` inline checks (weaker SSRF) — used by legacy tools
3. `tool_executor.py` inline checks (blacklist-based) — used by primary executor
4. `tool_sandbox.py` (additional regex-based) — used as pre-check by `_run_cmd()`

**Recommendation**: Consolidate all security checks through `security_baseline.py` as a single authority.

### B. Chinese/English Mixed Comments in security_baseline.py
Docstrings and comments contain intermixed Chinese and English (e.g., `pathsecurityCheck(TOCTOUProtection)`, `allowroot-ofdirectorylist`). While functionally identical, this violates the project's "no Chinese" rule and creates code review opacity.

### C. Guardian Module Runs `sudo` Commands
`guardian/healer.py` executes `sudo systemctl restart`, `sudo swapon`, `sudo fallocate`, `sudo chmod 600`, `sudo mkswap`, `sudo swapon`. These are hardcoded command arrays (no user input injection risk), but the guardian module has high system-level access.

### D. `exec` CMD Path (message_handler.py lines 882-896)
The `[CMD] exec` handler checks for dangerous chars (`;&|$()`<>`) client-side and then routes through `exec_bash()` for blacklist checking. This is a good defense-in-depth pattern.

### E. No TLS on API Server
The API server (server.py) binds to 127.0.0.1:8080 with no TLS. For local-only use this is acceptable, but if exposed, all traffic including API keys would be in plaintext.

---

## Summary Risk Matrix

| Vulnerability | Severity | Status | Notes |
|---|---|---|---|
| SSRF (weaker inline impl) | **Medium** | Existing gap | builtin_tools.py lacks DNS rebinding check |
| Path traversal TOCTOU | **Low** | Existing gap | Small race window in builtin_tools handlers |
| Auth bypass ([CMD] default) | **High** | Existing gap | No HMAC auth by default, Telegram auto-level |
| Auth bypass (MASTER_IDS default) | **Medium** | Existing gap | Defaults to `"admin"` |
| Command injection (temp shell) | **Medium** | Existing gap | Temp file execution bypasses shell=False |
| Hardcoded secrets | **None** | Clean | All env-var based |
| Race conditions | **Low** | Existing gap | Global state lacks isolation |
| WebSocket validation | **N/A** | No WS | Not present |
| SQL injection | **N/A** | No SQL | Not present |

---

## Top Recommendations

1. **Set CMD_AUTH_SECRET as mandatory** — Remove the fallback that allows unauthenticated [CMD] messages. Reject all [CMD] without a valid HMAC signature by default.
2. **Consolidate SSRF checks** — Remove the weaker inline SSRF in builtin_tools.py and route all URL checks through security_baseline.validate_url().
3. **Fix TOCTOU in builtin_tools.py** — Add a lock around the path-check → file-open sequence in read_file_handler/write_file_handler.
4. **Reconsider temp-file shell execution** — Avoid writing attacker-controlled command strings to executable temp scripts. Consider using `shlex.split()` with safe alternatives for shell operators.
5. **Change default MASTER_IDS** — Generate a random value or fail closed with no default.
6. **Consolidate to single security layer** — Route all tools through security_baseline.py as the canonical security module.
