#!/usr/bin/env python3

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

"""Self-heal executor for EITElite system.

Standalone script that runs security_self_check.py and auto-fixes
FAIL items where safe automated remediation is possible.
"""

import json
import os
import subprocess
import sys
import shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
SELF_CHECK_PATH = os.path.join(SCRIPT_DIR, "security_self_check.py")
TICAL_CODE = os.path.join(WORKSPACE, "tical_code")
CORE_DIR = os.path.join(TICAL_CODE, "core")


def run_self_check() -> list[dict]:
    """Run security_self_check.py and return parsed results."""
    if not os.path.exists(SELF_CHECK_PATH):
        print(json.dumps({"action": "run_check", "status": "FAIL", "detail": "security_self_check.py not found"}))
        return []

    result = subprocess.run(
        [sys.executable, SELF_CHECK_PATH],
        capture_output=True, text=True, timeout=120,
        cwd=WORKSPACE,
    )

    checks = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            checks.append(parsed)
        except json.JSONDecodeError:
            continue

    return checks


def auto_fix(check_name: str) -> dict:
    """Execute fix for a failed check. Returns {"status": "fixed"|"failed", "detail": "..."}."""
    fixes = {
        "bare except": _fix_bare_except,
        "CJK characters": _fix_cjk_chars,
        "shell=True usage": _fix_shell_true,
    }

    fix_fn = fixes.get(check_name)
    if fix_fn is None:
        return {"status": "skipped", "detail": f"Check '{check_name}' requires manual review; no automated fix available"}

    try:
        result = fix_fn()
        return result
    except Exception as e:
        return {"status": "failed", "detail": f"Fix execution error: {e}"}


def _fix_bare_except() -> dict:
    """Replace bare 'except:' with 'except Exception:' in all .py files under tical_code/."""
    if not os.path.isdir(TICAL_CODE):
        return {"status": "failed", "detail": f"tical_code directory not found: {TICAL_CODE}"}

    fixed_files = []
    errors = []

    for root, dirs, files in os.walk(TICAL_CODE):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()

                # Only match lines with bare 'except:' (not 'except Exception:', 'except (', etc.)
                new_content = _replace_bare_except(content)
                if new_content != content:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    fixed_files.append(fpath)
            except Exception as e:
                errors.append(f"{fpath}: {e}")

    if errors:
        return {"status": "failed", "detail": f"Fixed {len(fixed_files)} file(s), but {len(errors)} error(s): {'; '.join(errors[:5])}"}

    return {"status": "fixed", "detail": f"Replaced bare except in {len(fixed_files)} file(s)"}


def _replace_bare_except(content: str) -> str:
    """Replace bare 'except:' lines with 'except Exception:'."""
    lines = content.split("\n")
    new_lines = []
    for line in lines:
        stripped = line.strip()
        # Match patterns like: except: | except : |  except:
        # But NOT: except Exception: | except ( | except ValueError:
        if stripped.startswith("except") and stripped.endswith(":"):
            # Check it's truly bare (no exception type specified)
            after_except = stripped[6:].strip()
            # Remove the trailing colon
            after_except = after_except.rstrip(":").strip()
            if after_except == "" or after_except == ":":
                # This is a bare except: line
                indent = line[:len(line) - len(line.lstrip())]
                new_lines.append(indent + "except Exception:")
                continue
        new_lines.append(line)
    return "\n".join(new_lines)


def _fix_cjk_chars() -> dict:
    """Find and report CJK characters; automated replacement requires human review."""
    try:
        result = subprocess.run(
            [
                "bash", "-c",
                "grep -rPn '[\\x{4e00}-\\x{9fff}]' --include='*.py' " + TICAL_CODE
                + " | head -30"
            ],
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()
        if not output:
            return {"status": "noop", "detail": "No CJK characters found; nothing to fix"}

        return {"status": "skipped", "detail": "CJK character fix requires manual review. Files with CJK:\n" + output[:2000]}
    except Exception as e:
        return {"status": "failed", "detail": f"Error scanning for CJK: {e}"}


def _fix_shell_true() -> dict:
    """Find and report subprocess shell=True usage; automated fix is risky."""
    try:
        result = subprocess.run(
            ["bash", "-c",
             "grep -rn 'shell=True' --include='*.py' " + TICAL_CODE
             + " 2>/dev/null | grep subprocess || true"],
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()
        if not output:
            return {"status": "noop", "detail": "No subprocess shell=True found; nothing to fix"}

        return {"status": "skipped", "detail": "shell=True fix requires manual review of each usage. Matches:\n" + output[:2000]}
    except Exception as e:
        return {"status": "failed", "detail": f"Error scanning for shell=True: {e}"}


def main():
    """Run self-check, then auto-fix any FAIL items."""
    print(json.dumps({"action": "start", "detail": "Running security self-check..."}))

    checks = run_self_check()
    if not checks:
        print(json.dumps({"action": "abort", "status": "FAIL", "detail": "No check results received"}))
        return 1

    # Report all checks
    for check in checks:
        print(json.dumps({"action": "check_result", "check": check.get("check", "?"), "status": check.get("status", "?"), "detail": check.get("detail", "")}))

    # Fix failures
    failures = [c for c in checks if c.get("status") == "FAIL"]
    if not failures:
        print(json.dumps({"action": "complete", "status": "PASS", "detail": "All checks passed; no fixes needed"}))
        return 0

    print(json.dumps({"action": "fixing", "detail": f"Attempting auto-fix for {len(failures)} failed check(s)"}))

    fix_results = []
    for failure in failures:
        check_name = failure.get("check", "unknown")
        detail = failure.get("detail", "")
        print(json.dumps({"action": "fix_start", "check": check_name, "original_error": detail}))
        fix_result = auto_fix(check_name)
        fix_results.append({"check": check_name, "fix": fix_result})
        print(json.dumps({"action": "fix_result", "check": check_name, "status": fix_result["status"], "detail": fix_result["detail"]}))

    # Re-run self-check after fixes
    print(json.dumps({"action": "recheck", "detail": "Re-running security self-check after fixes..."}))
    final_checks = run_self_check()
    final_failures = [c for c in final_checks if c.get("status") == "FAIL"]

    if final_failures:
        print(json.dumps({"action": "complete", "status": "PARTIAL", "detail": f"{len(failures) - len(final_failures)}/{len(failures)} failures fixed; {len(final_failures)} remaining"}))
        return 1
    else:
        print(json.dumps({"action": "complete", "status": "PASS", "detail": "All failures resolved after auto-fix"}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
