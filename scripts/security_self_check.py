#!/usr/bin/env python3

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

"""Security self-check script for tical-code system.

Performs 9 independent security and structural checks on the codebase.
Outputs JSON-line results. Must be executed from the eite-agent workspace root.
No third-party dependencies.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile


WORKSPACE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)

TICAL_CODE = os.path.join(WORKSPACE, "tical_code")
EITE_DIR = os.path.join(TICAL_CODE, "core", "eite")
CORE_DIR = os.path.join(TICAL_CODE, "core")


def _exists(path: str) -> bool:
    return os.path.exists(path)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _ast_parse(path: str) -> ast.AST:
    return ast.parse(_read(path))


def _grep_count(pattern: str, path: str, file_glob: str = "*.py") -> int:
    """Run grep and return match count. Returns -1 on error."""
    try:
        result = subprocess.run(
            ["grep", "-rn", pattern, "--include=" + file_glob, path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Count non-empty lines
        output = result.stdout.strip()
        if not output:
            return 0
        lines = [l for l in output.split("\n") if l.strip()]
        return len(lines)
    except Exception:
        return -1


def check_engine_check() -> dict:
    """Check 1: engine.py contains def check(request)."""
    engine_path = os.path.join(EITE_DIR, "engine.py")
    if not _exists(engine_path):
        return {"check": "engine.py check()", "status": "FAIL", "detail": "File not found: " + engine_path}
    try:
        tree = _ast_parse(engine_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "check":
                args = [a.arg for a in node.args.args]
                return {"check": "engine.py check()", "status": "PASS", "detail": f"def check({', '.join(args)}) found at line {node.lineno}"}
        return {"check": "engine.py check()", "status": "FAIL", "detail": "def check not found in engine.py"}
    except SyntaxError as e:
        return {"check": "engine.py check()", "status": "FAIL", "detail": f"Syntax error: {e}"}


def check_signature_fallback() -> dict:
    """Check 2: signature.py contains logger.warning call."""
    sig_path = os.path.join(EITE_DIR, "signature.py")
    if not _exists(sig_path):
        return {"check": "signature.py logger.warning", "status": "FAIL", "detail": "File not found: " + sig_path}
    try:
        content = _read(sig_path)
        if "logger.warning" in content:
            return {"check": "signature.py logger.warning", "status": "PASS", "detail": "logger.warning call found in signature.py"}
        else:
            return {"check": "signature.py logger.warning", "status": "FAIL", "detail": "logger.warning not found in signature.py"}
    except Exception as e:
        return {"check": "signature.py logger.warning", "status": "FAIL", "detail": f"Error reading file: {e}"}


def check_bare_except() -> dict:
    """Check 3: no bare 'except:' clauses in tical_code/."""
    try:
        result = subprocess.run(
            [
                "bash", "-c",
                "grep -rn 'except:' --include='*.py' " + TICAL_CODE
                + " | grep -v 'except Exception' | grep -v 'except (' | wc -l"
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        count = int(result.stdout.strip())
        if count == 0:
            return {"check": "bare except", "status": "PASS", "detail": "No bare except clauses found"}
        else:
            return {"check": "bare except", "status": "FAIL", "detail": f"Found {count} bare except clause(s)"}
    except Exception as e:
        return {"check": "bare except", "status": "FAIL", "detail": f"Grep error: {e}"}


def check_cjk_chars() -> dict:
    """Check 4: scan for CJK characters (U+4E00-U+9FFF) in .py files."""
    try:
        result = subprocess.run(
            [
                "bash", "-c",
                "grep -rPn '[\\x{4e00}-\\x{9fff}]' --include='*.py' " + TICAL_CODE
                + " | head -20"
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip()
        lines = [l for l in output.split("\n") if l.strip()]
        if not lines:
            return {"check": "CJK characters", "status": "PASS", "detail": "No CJK characters found in .py files"}
        else:
            files_with_cjk = set(l.split(":")[0] for l in lines if ":" in l)
            return {"check": "CJK characters", "status": "FAIL", "detail": f"Found CJK in {len(files_with_cjk)} file(s): {', '.join(sorted(files_with_cjk)[:10])}"}
    except Exception as e:
        return {"check": "CJK characters", "status": "FAIL", "detail": f"Grep error: {e}"}


def check_shell_true() -> dict:
    """Check 5: no subprocess.run.*shell=True patterns."""
    try:
        result = subprocess.run(
            ["bash", "-c",
             "grep -rn 'shell=True' --include='*.py' " + TICAL_CODE
             + " | grep -c subprocess || true"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        count = int(result.stdout.strip())
        if count == 0:
            return {"check": "shell=True usage", "status": "PASS", "detail": "No subprocess shell=True patterns found"}
        else:
            return {"check": "shell=True usage", "status": "FAIL", "detail": f"Found {count} subprocess shell=True usage(s)"}
    except Exception as e:
        return {"check": "shell=True usage", "status": "FAIL", "detail": f"Grep error: {e}"}


def check_threadpool_usage() -> dict:
    """Check 6: unified_worker.py uses ThreadPoolExecutor."""
    uw_path = os.path.join(CORE_DIR, "unified_worker.py")
    if not _exists(uw_path):
        return {"check": "ThreadPoolExecutor usage", "status": "FAIL", "detail": "File not found: " + uw_path}
    try:
        content = _read(uw_path)
        if "ThreadPoolExecutor" in content:
            return {"check": "ThreadPoolExecutor usage", "status": "PASS", "detail": "ThreadPoolExecutor found in unified_worker.py"}
        else:
            return {"check": "ThreadPoolExecutor usage", "status": "FAIL", "detail": "ThreadPoolExecutor not found in unified_worker.py"}
    except Exception as e:
        return {"check": "ThreadPoolExecutor usage", "status": "FAIL", "detail": f"Error: {e}"}


def check_subagent_alive() -> dict:
    """Check 7: subagent.py must not contain 'DESIGNED-NOT-DEAD' string."""
    sa_path = os.path.join(CORE_DIR, "subagent.py")
    if not _exists(sa_path):
        return {"check": "subagent not-dead", "status": "FAIL", "detail": "File not found: " + sa_path}
    try:
        content = _read(sa_path)
        if "DESIGNED-NOT-DEAD" in content:
            return {"check": "subagent not-dead", "status": "FAIL", "detail": "DESIGNED-NOT-DEAD marker found in subagent.py"}
        else:
            return {"check": "subagent not-dead", "status": "PASS", "detail": "No DESIGNED-NOT-DEAD marker found"}
    except Exception as e:
        return {"check": "subagent not-dead", "status": "FAIL", "detail": f"Error: {e}"}


def check_molecule_engine() -> dict:
    """Check 8: molecule.py contains class MoleculeEngine."""
    mol_path = os.path.join(CORE_DIR, "molecule.py")
    if not _exists(mol_path):
        return {"check": "MoleculeEngine class", "status": "FAIL", "detail": "File not found: " + mol_path}
    try:
        tree = _ast_parse(mol_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "MoleculeEngine":
                return {"check": "MoleculeEngine class", "status": "PASS", "detail": f"class MoleculeEngine found at line {node.lineno}"}
        return {"check": "MoleculeEngine class", "status": "FAIL", "detail": "class MoleculeEngine not found"}
    except SyntaxError as e:
        return {"check": "MoleculeEngine class", "status": "FAIL", "detail": f"Syntax error: {e}"}


def check_orthos_chain_call() -> dict:
    """Check 9: orthos_chain.py contains def chain_call."""
    ort_path = os.path.join(CORE_DIR, "orthos_chain.py")
    if not _exists(ort_path):
        return {"check": "orthos_chain.chain_call", "status": "FAIL", "detail": "File not found: " + ort_path}
    try:
        tree = _ast_parse(ort_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "chain_call":
                return {"check": "orthos_chain.chain_call", "status": "PASS", "detail": f"def chain_call found at line {node.lineno}"}
        return {"check": "orthos_chain.chain_call", "status": "FAIL", "detail": "def chain_call not found"}
    except SyntaxError as e:
        return {"check": "orthos_chain.chain_call", "status": "FAIL", "detail": f"Syntax error: {e}"}


def check_self_ast() -> dict:
    """Check 10 (bonus): self-check that this script itself passes ast.parse."""
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            ast.parse(f.read())
        return {"check": "self AST parse", "status": "PASS", "detail": f"{os.path.basename(__file__)} passes ast.parse"}
    except SyntaxError as e:
        return {"check": "self AST parse", "status": "FAIL", "detail": f"AST parse error: {e}"}


def main():
    """Run all checks and output JSON lines."""
    checks = [
        check_engine_check,
        check_signature_fallback,
        check_bare_except,
        check_cjk_chars,
        check_shell_true,
        check_threadpool_usage,
        check_subagent_alive,
        check_molecule_engine,
        check_orthos_chain_call,
        check_self_ast,
    ]

    results = []
    failed = 0
    for check_fn in checks:
        try:
            result = check_fn()
        except Exception as e:
            result = {"check": check_fn.__name__, "status": "FAIL", "detail": f"Unhandled exception: {e}"}
        results.append(result)
        if result["status"] == "FAIL":
            failed += 1
        print(json.dumps(result, ensure_ascii=False))

    # Summary line
    summary = {"check": "__summary__", "status": "PASS" if failed == 0 else "FAIL", "detail": f"{len(results) - failed}/{len(results)} checks passed"}
    print(json.dumps(summary, ensure_ascii=False))

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
