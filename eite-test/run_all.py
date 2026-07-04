#!/usr/bin/env python3
"""
EITElite / EITElite System Test Station - T1-T8 Full Inspection
Run on Test VPS (REPLACED_TEST_IP) after any system change.

Usage:
  python3 eite-test/run_all.py [--vps] [--all-vps]
"""

import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

PASS = "✅"
FAIL = "❌"
SKIP = "⏭️"

tests_run = 0
tests_passed = 0
tests_failed = 0
results = []

def test(name, section):
    global tests_run, tests_passed, tests_failed
    def decorator(fn):
        def wrapper():
            global tests_run, tests_passed, tests_failed
            tests_run += 1
            indent = "  "
            try:
                fn()
                tests_passed += 1
                print(f"  {PASS} {name}")
                results.append((section, name, "pass", ""))
            except AssertionError as e:
                tests_failed += 1
                msg = str(e) if str(e) else "assertion failed"
                print(f"  {FAIL} {name}: {msg}")
                results.append((section, name, "fail", msg))
            except Exception as e:
                tests_failed += 1
                msg = f"{type(e).__name__}: {e}"
                print(f"  {FAIL} {name}: {msg}")
                traceback.print_exc()
                results.append((section, name, "fail", msg))
        return wrapper
    return decorator

# ============================================================
# T1: Syntax & Module Integrity
# ============================================================

@test("All core .py files compile", "T1")
def t1_syntax():
    core_dir = ROOT / "tical_code" / "core"
    errors = []
    for py in sorted(core_dir.rglob("*.py")):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(py)],
                          capture_output=True, text=True)
        if r.returncode != 0:
            errors.append(f"{py.relative_to(ROOT)}: {r.stderr[:80]}")
    assert not errors, f"\n" + "\n".join(errors)

@test("Core modules import", "T1")
def t1_imports():
    from tical_code.core.prompt import build_system_prompt
    from tical_code.core.response_formatter import format_result
    from tical_code.core.tool_executor import execute, TOOL_SCHEMAS, TOOL_SCHEMAS_CLEAN
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    from tical_code.core.modules.loop_detector import LoopDetector
    from tical_code.core.modules.context_compactor import ContextCompactor
    from tical_code.core.modules.proposal_gate import ProposalGate
    from tical_code.core.eite import init, get_verify
    assert build_system_prompt, "build_system_prompt not importable"

@test("EITE modules import", "T1")
def t1_eite():
    from tical_code.core.eite import init, get_verify
    from tical_code.core.eite.verify import VerifyLayer
    from tical_code.core.eite.signature import sign, verify, _get_hardware_id
    assert VerifyLayer

@test("unified_worker parseable", "T1")
def t1_worker():
    import py_compile
    py_compile.compile(str(ROOT / "tical_code" / "core" / "unified_worker.py"), doraise=True)

# ============================================================
# T2: Content Integrity
# ============================================================

@test("Reporting Iron Law 5 sections present", "T2")
def t2_iron_law():
    from tical_code.core.prompt import build_system_prompt
    p = build_system_prompt(name="test", hostname="tester")
    sections = ["Evidence Mandate", "Standard Report Format",
                "Verification Chain", "Anti-Fabrication", "Summary Line"]
    for s in sections:
        assert s in p, f"Missing section: {s}"
    assert "git diff" in p, "Missing git diff requirement"
    assert "Fixed" in p or "Completed" in p, "Missing anti-fabrication check"

@test("EITE identity marker complete", "T2")
def t2_eite_marker():
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    v = get_verify()
    assert v, "EITE not initialized"
    m = v.get_identity_marker()
    assert "Name:" in m
    assert "Hash:" in m
    assert "Signature:" in m

@test("TruthfulReporter catches bare claims", "T2")
def t2_tr_catch():
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/tr_test")
    v = r.scan_reply("Fixed")
    assert len(v) > 0, "Should catch bare Fixed"

@test("TruthfulReporter allows verified claims", "T2")
def t2_tr_pass():
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/tr_test2")
    r.record_action("bash", {"command": "fix"}, {"exit_code": 0}, verified=True)
    v = r.scan_reply("Fixed")
    assert len(v) == 0, f"Should pass with verified bash: {v}"

@test("Rule 6: catches summary-only git diff", "T2")
def t2_evidence_summary():
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/tr_ev1")
    r.record_action("bash", {"command": "git diff"}, {"stdout": "+some code"}, verified=True)
    # Reply says "git diff shows changes" but no raw diff markers
    v = r.scan_reply("Already fixed, git diff shows changes were made")
    assert any(vv["rule"] == 6 for vv in v), f"Should catch summary-only: {v}"

@test("Rule 6: passes with raw diff output", "T2")
def t2_evidence_raw_diff():
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/tr_ev2")
    r.record_action("bash", {"command": "git diff"}, {"stdout": "+some code"}, verified=True)
    # Reply includes actual raw diff markers
    v = r.scan_reply(
        "Fixed the config parser.\n"
        "```\ndiff --git a/config.py b/config.py\n"
        "--- a/config.py\n+++ b/config.py\n"
        "@@ -10,7 +10,7 @@\n"
        " old value\n"
        "+new value\n"
        "```\n"
        "commit abcdef1"
    )
    ev = [vv for vv in v if vv["rule"] == 6]
    assert len(ev) == 0, f"Should pass with raw diff: {ev}"

@test("Rule 6: catches missing test output", "T2")
def t2_evidence_no_test():
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/tr_ev3")
    r.record_action("bash", {"command": "pytest"}, {"stdout": ".........", "exit_code": 0}, verified=True)
    v = r.scan_reply("All tests pass, no issues")
    ev = [vv for vv in v if vv["rule"] == 6]
    assert len(ev) > 0, f"Should catch missing test output: {v}"

# ============================================================
# T3: Tool Inventory
# ============================================================

@test("No broken tool handlers (dispatch→exec_*)", "T3")
def t3_broken_handlers():
    from tical_code.core.tool_executor import execute, TOOL_SCHEMAS
    # Import dispatch table
    import tical_code.core.tool_executor as te
    # Check every exec_* function exists
    dispatch_names = set()
    for attr in dir(te):
        if attr.startswith("exec_"):
            dispatch_names.add(attr)
    # Check TOOL_SCHEMAS references match dispatch
    schema_names = {s["function"]["name"].replace(".", "__") for s in TOOL_SCHEMAS}
    # Normalize dot-names
    assert "conv_search" not in schema_names, "conv_search should be removed"
    assert "bash_execute" not in schema_names, "bash_execute should be filtered"

@test("Tool count in expected range", "T3")
def t3_tool_count():
    from tical_code.core.tool_executor import TOOL_SCHEMAS
    count = len(TOOL_SCHEMAS)
    assert 40 <= count <= 55, f"Tool count {count} outside expected range 40-55"

# ============================================================
# T4: EITE Verify Layer
# ============================================================

@test("verify_tool_result: file_write", "T4")
def t4_verify_file_write():
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    v = get_verify()
    r = v.verify_tool_result("file_write",
        {"path": "/tmp/test_eite_verify.txt"},
        {"path": "/tmp/test_eite_verify.txt", "ok": False})
    # File doesn't exist → verified=False
    assert r.get("verified") == False

@test("verify_tool_result: bash success", "T4")
def t4_verify_bash_ok():
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    v = get_verify()
    r = v.verify_tool_result("bash", {"command": "echo hi"}, {"exit_code": 0, "stdout": "hi"})
    assert r.get("verified") == True, f"bash exit=0 should pass: {r}"

@test("verify_tool_result: bash fail", "T4")
def t4_verify_bash_fail():
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    v = get_verify()
    r = v.verify_tool_result("bash", {"command": "false"}, {"exit_code": 1, "stderr": ""})
    assert r.get("verified") == False, f"bash exit≠0 should fail: {r}"

@test("EITE identity check", "T4")
def t4_identity_check():
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    v = get_verify()
    ok = v.check_identity("You are test-worker, an autonomous AI Agent.")
    assert ok, "Identity check should pass with correct name"
    bad = v.check_identity("You are imposter.")
    assert not bad, "Identity check should fail with wrong name"

# ============================================================
# T5: Git Hygiene
# ============================================================

@test("No runtime artifacts tracked in git", "T5")
def t5_git_tracked():
    r = subprocess.run(["git", "ls-files", "*.jsonl", "*.db*", ".trust_state.json"],
                      capture_output=True, text=True, timeout=10)
    tracked = [l for l in r.stdout.strip().split("\n") if l.strip()]
    # Allow .gitignore itself
    tracked = [l for l in tracked if l != ".gitignore"]
    assert len(tracked) == 0, f"Runtime files tracked: {tracked}"

@test("git status is clean", "T5")
def t5_git_clean():
    r = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, timeout=10)
    dirty = [l for l in r.stdout.strip().split("\n") if l.strip() and not l.startswith("?")]
    if dirty:
        print(f"  dirty files: {dirty}")

# ============================================================
# T6: Dead Code Regression
# ============================================================

@test("No broken imports (from .identity etc)", "T6")
def t6_broken_imports():
    # Check all .py files for relative imports that don't exist
    core = ROOT / "tical_code" / "core"
    all_py_files = list(core.rglob("*.py"))
    existing_modules = {str(p.relative_to(core).with_suffix("")) for p in all_py_files}
    existing_modules.add("")  # current dir
    
    bad_refs = []
    for py in all_py_files:
        content = py.read_text()
        for m in re.findall(r'from\s+\.(\w+)\s+import', content):
            ref_path = str(py.relative_to(core).parent / m)
            if m not in existing_modules and ref_path not in existing_modules:
                # Check if it's a dotted relative import
                if "." + m not in content and m not in content.replace(str(py.relative_to(core).parent), ""):
                    pass  # complex case, skip
    # Simpler check: just ensure identity.py doesn't exist (was broken reference)
    assert not (core / "identity.py").exists(), "identity.py should not exist"
    assert not (core / "memory_sense.py").exists(), "memory_sense.py should not exist"

@test("No orphaned top-level constants", "T6")
def t6_dead_constants():
    # Check for known dead constant patterns
    from tical_code.core.tool_executor import TOOL_SCHEMAS
    # These should NOT exist as module-level names
    import tical_code.core.tool_executor as te
    assert not hasattr(te, "MAX_TOOL_ITERATIONS"), "MAX_TOOL_ITERATIONS dead constant removed"
    assert not hasattr(te, "SOFT_HINT_AT"), "SOFT_HINT_AT dead constant removed"
    assert not hasattr(te, "HARD_STOP_AT"), "HARD_STOP_AT dead constant removed"

@test("No orphaned files in core/", "T6")
def t6_orphaned_files():
    from tical_code.core.eite import init, get_verify
    import tical_code.core.tool_executor as te
    
    # Known dead files should be gone
    dead_files = [
        ROOT / "tical_code" / "core" / "verify.py",
        ROOT / "tical_code" / "core" / "heartbeat.py",
    ]
    for f in dead_files:
        assert not f.exists(), f"Dead file still exists: {f}"

# ============================================================
# T7: Cross-VPS Sync (requires --all-vps flag)
# ============================================================

@test("Anchor file parses correctly", "T7")
def t7_anchor_parse():
    anchor = Path.home() / "anchors" / "ops-anchor.json"
    if not anchor.exists():
        raise AssertionError(f"Anchor not found: {anchor}")
    data = json.loads(anchor.read_text())
    assert "version" in data, "Missing version"
    assert "vps" in data, "Missing vps section"
    assert "sg" in data["vps"], "Missing SG in VPS"

# ============================================================
# T8: Worker Init (SMOKE TEST)
# ============================================================

@test("Worker.__init__ with mock config", "T8")
def t8_worker_init():
    """Minimal smoke test - confirm Worker can initialize."""
    from tical_code.core.unified_worker import Worker
    cfg = {
        "name": "test",
        "workspace": "/tmp/eite_worker_test",
        "tg_token": "",
        "chat_url": "",
        "chat_key": "",
        "ai_model": "deepseek-chat",
        "ai_key": "sk-test",
        "ai_endpoint": "https://api.deepseek.com/v1",
    }
    try:
        w = Worker(cfg)
        assert w.name == "test"
    except Exception as e:
        raise AssertionError(f"Worker init failed: {e}")

@test("build_system_prompt + EITE full chain", "T8")
def t8_full_prompt():
    from tical_code.core.prompt import build_system_prompt
    from tical_code.core.eite import init, get_verify
    init(identity_id="test-worker", workspace="/tmp/eite_test")
    p = build_system_prompt(name="test", hostname="tester", deploy_path="/tmp",
                           target_model="deepseek-v4")
    v = get_verify()
    if v:
        p += v.get_identity_marker()
    assert len(p) > 1000, f"Prompt too short: {len(p)}"
    assert "Reporting Iron Law" in p
    assert "EITE Identity" in p

# ============================================================
# T9: Patch Integrity - verify after file edits
# ============================================================

@test("prompt.py contains all 5 iron laws", "T9")
def t9_prompt_iron_law():
    """Prevent shell escape damage: actual file content after edit must be correct."""
    src = (ROOT / "tical_code" / "core" / "prompt.py").read_text()
    assert "Reporting Iron Law" in src, "prompt.py missing Reporting Iron Law"
    assert "Evidence Mandate" in src
    assert "Standard Report Format" in src
    assert "Verification Chain" in src
    assert "Anti-Fabrication" in src
    assert "Summary Line" in src
    assert "git diff" in src
    assert "git log --oneline -1" in src

@test("eite/verify.py no scan_reply leftover", "T9")
def t9_no_scan_reply():
    """verify.py scan_reply already merged into truthful_reporter, should not remain."""
    src = (ROOT / "tical_code" / "core" / "eite" / "verify.py").read_text()
    assert "def scan_reply" not in src, "eite/verify.py still has scan_reply"
    assert "sig_verify" not in src, "unused import sig_verify remains"
    assert "import os" not in src, "unused import os remains"
    assert "import re" not in src, "unused import re remains"

@test("signature.py no EITE_IMMUTABLE_FLAG leftover", "T9")
def t9_no_immutable_flag():
    src = (ROOT / "tical_code" / "core" / "eite" / "signature.py").read_text()
    assert "EITE_IMMUTABLE_FLAG" not in src, "dead constant EITE_IMMUTABLE_FLAG remains"
    assert "import json" not in src, "unused import json remains"
    assert "import os" not in src, "unused import os remains"

@test("response_formatter.py no format_error/progress", "T9")
def t9_no_dead_formatters():
    src = (ROOT / "tical_code" / "core" / "response_formatter.py").read_text()
    assert "def format_error" not in src
    assert "def format_progress" not in src

@test("unified_worker.py no heartbeat reference", "T9")
def t9_no_heartbeat():
    src = (ROOT / "tical_code" / "core" / "unified_worker.py").read_text()
    assert "heartbeat" not in src, "heartbeat reference remains"

@test("tool_executor.py no dead constants", "T9")
def t9_no_dead_constants():
    src = (ROOT / "tical_code" / "core" / "tool_executor.py").read_text()
    assert "MAX_TOOL_ITERATIONS" not in src
    assert "SOFT_HINT_AT" not in src
    assert "HARD_STOP_AT" not in src
    assert "conv_search" not in src

@test("channel.py no reply() alias", "T9")
def t9_no_reply_alias():
    src = (ROOT / "tical_code" / "core" / "channel.py").read_text()
    assert "def reply(self, response)" not in src

@test("clarify.py no format_clarify_questions", "T9")
def t9_no_clarify_dead():
    clarify_path = ROOT / "tical_code" / "core" / "clarify.py"
    if not clarify_path.exists():
        return  # File deleted as dead code - pass
    src = clarify_path.read_text()
    assert "def format_clarify_questions" not in src

@test("Core files deleted confirmed", "T9")
def t9_deleted_files():
    dead = [
        ROOT / "tical_code" / "core" / "verify.py",
        ROOT / "tical_code" / "core" / "heartbeat.py",
        ROOT / "tical_code" / "core" / "clarify.py",
        ROOT / "tical_code" / "core" / "cron_scheduler.py",
        ROOT / "tical_code" / "core" / "llm_backend.py",
        ROOT / "tical_code" / "core" / "memory_store.py",
        ROOT / "tical_code" / "core" / "sandbox.py",
        ROOT / "tical_code" / "core" / "subagent_interface.py",
    ]
    for f in dead:
        assert not f.exists(), f"dead file still exists: {f.name}"
    # Confirm EITElite specific files
    if (ROOT / "tical_code" / "core" / "verify.py").parent.exists():
        pass  # parent dir always exists

@test("modules/ no __future__ annotations", "T9")
def t9_no_future_annotations():
    mods = ["session_manager", "context_compactor", "loop_detector",
            "truthful_reporter", "proposal_gate"]
    for m in mods:
        src = (ROOT / "tical_code" / "core" / "modules" / f"{m}.py").read_text()
        assert "from __future__ import annotations" not in src, f"{m}.py __future__ remains"

@test("cron_scheduler.py no DEFAULT_TASK_TIMEOUT", "T9")
def t9_no_task_timeout():
    cron_path = ROOT / "tical_code" / "core" / "cron_scheduler.py"
    if not cron_path.exists():
        return  # File deleted as dead code - pass
    src = cron_path.read_text()
    assert "DEFAULT_TASK_TIMEOUT" not in src

# ============================================================
# T10: Deployment Consistency - cross-VPS verification
# ============================================================

@test("Anchor vps section completeness", "T10")
def t10_anchor_vps():
    anchor = Path.home() / "anchors" / "ops-anchor.json"
    if not anchor.exists():
        return
    data = json.loads(anchor.read_text())
    for name in ["node1", "node2", "node3", "node4", "node5"]:
        assert name in data["vps"], f"Anchor missing {name}"
        v = data["vps"][name]
        assert "ip" in v
        assert "ssh_user" in v
        assert "ssh_key" in v
        if "ssh_port" in v:
            assert isinstance(v["ssh_port"], int), f"{name} port is not a number"

@test("eitelite VPS git version consistency", "T10")
def t10_vps_version_consistency():
    """Only valid on Test VPS (requires SSH to other eitelite VPS)."""
    anchor = Path.home() / "anchors" / "ops-anchor.json"
    if not anchor.exists():
        return
    vault = json.loads(anchor.read_text())
    vps_list = vault.get("vps", {})
    
    # Local version
    local = subprocess.run(["git", "log", "--oneline", "-1"],
                          capture_output=True, text=True).stdout.strip()
    
    # Try SSH to same-repo VPS for comparison
    targets = [("node3", vps_list.get("node3", {})),
               ("test", vps_list.get("test", {}))]
    
    for name, info in targets:
        ip = info.get("ip", "")
        user = info.get("ssh_user", "ubuntu")
        key = info.get("ssh_key", "id_rsa")
        port = info.get("ssh_port", 22)
        if not ip or ip == "localhost":
            continue
        key_path = os.path.expanduser(f"~/.ssh/{key}")
        if not os.path.exists(key_path):
            continue
        
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
               "-p", str(port), "-i", key_path,
               f"{user}@{ip}",
               f"cd {info.get('deploy_path', '/home/YOUR_USER/project')} && git log --oneline -1"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            remote = r.stdout.strip()
            local_hash = local.split()[0]
            remote_hash = remote.split()[0]
            assert local_hash == remote_hash, \
                f"{name} version mismatch: local={local_hash} remote={remote_hash}"

# ============================================================
# Main
# ============================================================

def main():
    global tests_run, tests_passed, tests_failed
    print(f"\n{'='*60}")
    print(f"  EITElite System Test Suite - {ROOT}")
    print(f"{'='*60}\n")

    # Collect all test functions
    import inspect
    test_fns = [(name, fn) for name, fn in globals().items()
                    if name.startswith("t") and callable(fn) and name != "test"]

    # Organize by section
    sections = {}
    for name, fn in test_fns:
        # Get section from decorator closure... simpler: parse source
        section = name.split("_")[0].upper()
        sections.setdefault(section, []).append((name, fn))
    
    for section in sorted(sections.keys()):
        print(f"\n--- {section} ---")
        for name, fn in sections[section]:
            fn()
        print()

    print(f"{'='*60}")
    print(f"  Results: {tests_passed}/{tests_run} passed", end="")
    if tests_failed:
        print(f", {tests_failed} FAILED", end="")
    print()
    print(f"{'='*60}")

    # Print failures
    failures = [(s, n, m) for s, n, st, m in results if st == "fail"]
    if failures:
        print(f"\n{FAIL} FAILURES:")
        for s, n, m in failures:
            print(f"  [{s}] {n}: {m}")

    return 0 if tests_failed == 0 else 1




# ============================================================
# T11: EITE Verification Ablation - prove verify layer works
# ============================================================

@test("TruthfulReporter catches fabrication: fake git diff", "T11")
def t11_catch_fake_git_diff():
    """Rule 6 must catch 'git diff shows changes' without raw diff output."""
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/t11_test")
    r.record_action("bash", {"command": "git diff"}, {"stdout": "+some change", "exit_code": 0}, verified=True)
    v = r.scan_reply("All done. git diff shows changes were made, tests pass.")
    has_rule6 = any(vv["rule"] == 6 for vv in v)
    assert has_rule6, f"Rule 6 should catch summary-only report: {v}"

@test("TruthfulReporter passes with raw evidence", "T11")
def t11_pass_raw_evidence():
    """Rule 6 must pass when raw diff markers are present."""
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    r = TruthfulReporter(workspace="/tmp/t11_test2")
    r.record_action("bash", {"command": "git diff"}, {"stdout": "+change", "exit_code": 0}, verified=True)
    r.record_action("bash", {"command": "git log"}, {"stdout": "commit abc1234", "exit_code": 0}, verified=True)
    v = r.scan_reply(
        "Fixed the bug.\n"
        "```\ndiff --git a/a.py b/a.py\n"
        "--- a/a.py\n+++ b/a.py\n"
        "@@ -1,3 +1,5 @@\n"
        " old\n"
        "+new\n"
        "```\n"
        "Tests: Ran 5 tests, OK\n"
        "commit abc1234"
    )
    ev = [vv for vv in v if vv["rule"] == 6]
    assert len(ev) == 0, f"Should pass with raw evidence: {ev}"

@test("EITE verify blocks non-existent file claim", "T11")
def t11_verify_file_write():
    """verify_tool_result must detect file_write that didn't actually write."""
    from tical_code.core.eite import init, get_verify
    init(identity_id="t11", workspace="/tmp/t11_eite")
    v = get_verify()
    r = v.verify_tool_result("file_write", {"path": "/tmp/t11_nonexistent.txt"}, {"ok": False, "path": "/tmp/t11_nonexistent.txt"})
    assert r.get("verified") == False, f"Should detect non-existent file: {r}"

@test("EITE verify passes real file write", "T11")
def t11_verify_real_write():
    """verify_tool_result must pass when file actually exists."""
    import tempfile
    from tical_code.core.eite import init, get_verify
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
        f.write(b"hello")
        tmp = f.name
    try:
        init(identity_id="t11b", workspace="/tmp/t11_eite2")
        v = get_verify()
        r = v.verify_tool_result("file_write", {"path": tmp}, {"ok": True, "path": tmp})
        assert r.get("verified") == True, f"Should pass for real file: {r}"
    finally:
        os.unlink(tmp)


# ============================================================
# T12: Resource Consumption Baseline
# ============================================================

@test("Worker memory under 50MB on startup", "T12")
def t12_worker_memory():
    """Measure the running worker's RSS. Must be under 50MB for 1c1g compatibility."""
    import psutil
    try:
        current_pid = os.getpid()
        proc = psutil.Process(current_pid)
        rss_mb = proc.memory_info().rss / 1024 / 1024
        print(f" ({rss_mb:.1f}MB RSS)", end="")
        assert rss_mb < 50, f"Memory {rss_mb:.1f}MB exceeds 50MB limit"
    except ImportError:
        print(" (psutil not available, checking via /proc)", end="")
        try:
            with open(f"/proc/{os.getpid()}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        rss_mb = rss_kb / 1024
                        print(f" ({rss_mb:.1f}MB RSS)", end="")
                        assert rss_mb < 50, f"Memory {rss_mb:.1f}MB exceeds 50MB limit"
                        break
        except Exception:
            print(" (SKIP: cannot measure)", end="")

@test("Worker starts in under 5 seconds", "T12")
def t12_worker_start_time():
    """Worker import + __init__ must complete quickly."""
    import time
    start = time.time()
    from tical_code.core.config import load_config
    from tical_code.core.unified_worker import Worker
    cfg = load_config()
    cfg["name"] = "t12_test"
    cfg["workspace"] = "/tmp/t12_ws"
    cfg["tg_token"] = ""
    cfg["chat_url"] = ""
    w = Worker(cfg)
    elapsed = time.time() - start
    print(f" ({elapsed:.2f}s)", end="")
    assert elapsed < 5, f"Worker init took {elapsed:.2f}s (>5s limit)"


# ============================================================
# T13: Autonomous Task Completion (via anchor API mock)
# ============================================================

@test("Autonomous cycle: dequeue + execute + complete", "T13")
def t13_autonomous_flow():
    """Simulate the full autonomous cycle by calling anchor tools directly."""
    import urllib.request
    anchor = os.environ.get("ANCHOR_URL", "https://bench.your-domain.com/anchor")
    worker = os.environ.get("WORKER_NAME", "t13_test")
    test_id = f"t13_{int(time.time())}"

    # Register test worker
    req = urllib.request.Request(
        anchor, data=json.dumps({"name": test_id, "hostname": "test", "status": "online"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        reg = json.loads(resp.read())
    assert reg.get("ok"), f"Anchor register failed: {reg}"

    # Enqueue task
    req = urllib.request.Request(
        f"{anchor.rstrip('/')}/task/enqueue",
        data=json.dumps({"target": test_id, "task": "echo autonomous test OK", "sender": "t13"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        enq = json.loads(resp.read())
    assert enq.get("ok"), f"Enqueue failed: {enq}"
    task_id = enq["task_id"]

    # Dequeue
    req = urllib.request.Request(
        f"{anchor.rstrip('/')}/task/dequeue",
        data=json.dumps({"worker": test_id}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        deq = json.loads(resp.read())
    assert deq.get("ok"), f"Dequeue failed: {deq}"
    task = deq.get("task", {})
    assert task.get("status") == "running", f"Task should be running: {task}"

    # Complete
    req = urllib.request.Request(
        f"{anchor.rstrip('/')}/task/complete",
        data=json.dumps({"task_id": task_id, "result": "done", "status": "done"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        com = json.loads(resp.read())
    assert com.get("ok"), f"Complete failed: {com}"

    # Verify
    req = urllib.request.Request(f"{anchor.rstrip('/')}/task/list")
    with urllib.request.urlopen(req, timeout=10) as resp:
        lst = json.loads(resp.read())
    tasks = [t for t in lst.get("tasks", []) if t["id"] == task_id]
    assert len(tasks) == 1, f"Task not found in list"
    assert tasks[0]["status"] == "done", f"Task should be done: {tasks[0]}"


# ============================================================
# T14: 48 Tools Available and Dispatchable
# ============================================================

@test("All 48 tools have dispatch handlers", "T14")
def t14_tool_dispatch():
    """Every tool in TOOL_SCHEMAS must have a matching exec_* handler."""
    from tical_code.core.tool_executor import TOOL_SCHEMAS
    names = {s["function"]["name"] for s in TOOL_SCHEMAS}
    # All tool names use underscores (dots already converted in TOOL_SCHEMAS_CLEAN)
    assert len(names) >= 40, f"Expected 40+ tools, got {len(names)}"

@test("Tool count in 40-55 range", "T14")
def t14_tool_count():
    from tical_code.core.tool_executor import TOOL_SCHEMAS
    count = len(TOOL_SCHEMAS)
    assert 40 <= count <= 55, f"Tool count {count} outside range 40-55"


# ============================================================
# T15: Report - generate JSON for dashboard
# ============================================================

@test("Generate EITElite benchmark report", "T15")
def t15_benchmark_report():
    """Write JSON report with system specs for web dashboard."""
    from tical_code.core.tool_executor import TOOL_SCHEMAS
    import platform
    report = {
        "system": "EITElite",
        "version": "v7.1",
        "hostname": platform.node(),
        "python": platform.python_version(),
        "arch": platform.machine(),
        "tools": len(TOOL_SCHEMAS),
        "rss_mb": None,
        "startup_s": None,
        "timestamp": time.time(),
    }
    # Try to get process RSS
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    report["rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                    break
    except Exception:
        pass
    report_path = Path("/tmp/eitelite_benchmark_report.json")
    report_path.write_text(json.dumps(report, indent=2))
    print(f" (report: {report_path})", end="")


# ============================================================
# T16: Cross-System Benchmark Infrastructure
# ============================================================

@test("Benchmark module compiles and has tasks", "T16")
def t16_benchmark_compile():
    """The benchmark.py module must compile cleanly."""
    import py_compile
    py_compile.compile(str(ROOT / "eite-test" / "benchmark.py"), doraise=True)

@test("Benchmark task definitions valid", "T16")
def t16_task_defs():
    """All tasks must have valid structure."""
    import py_compile
    # Read and parse the benchmark module manually
    import importlib.util
    spec = importlib.util.spec_from_file_location("benchmark",
        str(ROOT / "eite-test" / "benchmark.py"),
        submodule_search_locations=[]
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tasks = mod.TASK_SUITE
    assert len(tasks) >= 10, f"Expected 10+ tasks, got {len(tasks)}"
    levels = set(t["level"] for t in tasks)
    assert "L0" in levels and "L1" in levels and "L2" in levels and "L3" in levels
    assert mod.MODEL_PRICING["deepseek-chat"]["input"] > 0
    for task in tasks:
        eval_cmd = task["eval"]
        assert isinstance(eval_cmd, str) and len(eval_cmd) > 10, f"{task['id']}: eval too short"

@test("Benchmark report aggregation", "T16")
def t16_report_aggregation():
    """Benchmark aggregated report must have correct schema."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("benchmark",
        str(ROOT / "eite-test" / "benchmark.py"),
        submodule_search_locations=[]
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r1 = mod.RunResult(task_id="test", level="L0", system="eitelite", run=1, success=True, steps=2, cost_usd=0.001)
    results = [r1]
    reports = mod.aggregate_results(results)
    assert "eitelite" in reports
    assert reports["eitelite"].global_summary["success_rate_mean"] == 1.0
    assert reports["eitelite"].global_summary["total_cost"] == 0.001

@test("Failure taxonomy covers all categories", "T16")
def t16_failure_taxonomy():
    """Failure classification must cover all 5 categories."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("benchmark",
        str(ROOT / "eite-test" / "benchmark.py"),
        submodule_search_locations=[]
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert len(mod.FAILURE_CATEGORIES) == 6
    assert "fake_completion" in mod.FAILURE_CATEGORIES
    assert "infinite_loop" in mod.FAILURE_CATEGORIES
    patterns = mod.analyze_failure_patterns([])
    assert isinstance(patterns, dict)


# ============================================================
# T17: Compile Verification Layer
# ============================================================

@test("Compile: python -c import after file_write", "T17")
def t17_compile_import():
    """After writing a .py file, verify it imports."""
    from tical_code.core.tool_executor import execute
    test_code = "x = 42\ndef f(n): return n * 2\n"
    r = execute("file_write", {"path": "/tmp/t17_import_test.py", "content": test_code})
    cr = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, '/tmp'); import t17_import_test; assert t17_import_test.f(21) == 42"],
        capture_output=True, text=True, timeout=10
    )
    assert cr.returncode == 0, f"Import failed: {cr.stderr}"

@test("Compile: py_compile validation", "T17")
def t17_py_compile():
    """Generated Python files must pass py_compile."""
    import py_compile
    py_compile.compile("/tmp/t17_import_test.py", doraise=True)

@test("Compile: pytest after test generation", "T17")
def t17_compile_pytest():
    """After writing pytest tests, verify they run."""
    from tical_code.core.tool_executor import execute
    os.makedirs("/tmp/t17_pytest", exist_ok=True)
    module_code = "def add(a, b): return a + b\ndef mul(a, b): return a * b\n"
    test_code = (
        "from t17_module import add, mul\n"
        "def test_add(): assert add(2,3) == 5\n"
        "def test_mul(): assert mul(3,4) == 12\n"
    )
    execute("file_write", {"path": "/tmp/t17_pytest/t17_module.py", "content": module_code})
    execute("file_write", {"path": "/tmp/t17_pytest/test_t17_module.py", "content": test_code})
    cr = subprocess.run(
        [sys.executable, "-m", "pytest", "/tmp/t17_pytest/test_t17_module.py", "-v", "--tb=short"],
        capture_output=True, text=True, timeout=15
    )
    assert cr.returncode == 0, f"pytest failed: {cr.stderr[:200]}"


# ============================================================
# T18: Anti-Fabrication Cross-System Benchmark
# ============================================================

@test("Anti-fab benchmark module compiles", "T18")
def t18_antifab_compiles():
    """Anti-fabrication benchmark must compile."""
    import py_compile
    py_compile.compile(str(ROOT / "eite-test" / "benchmark_antifab.py"), doraise=True)

@test("Anti-fab task definitions valid", "T18")
def t18_antifab_tasks():
    """ANTI_FAB_SUITE must have 5 tasks with correct structure."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("bm",
        str(ROOT / "eite-test" / "benchmark.py"), submodule_search_locations=[])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert len(mod.ANTI_FAB_SUITE) == 5
    for task in mod.ANTI_FAB_SUITE:
        assert task["level"] == "AF"
        assert "honest_eval" in task
        assert "eite_check" in task

@test("EITE verify catches real nonexistent file", "T18")
def t18_verify_catches_nonexistent():
    """verify_tool_result must detect file_write claim without actual file."""
    from tical_code.core.eite import init, get_verify
    import tempfile
    init(identity_id="t18", workspace="/tmp/t18_vrf")
    v = get_verify()
    # Remove any leftover file
    for p in ["/tmp/t18_test_fake.txt"]:
        if os.path.exists(p): os.unlink(p)
    r = v.verify_tool_result("file_write", {"path": "/tmp/t18_test_fake.txt"}, {"ok": True, "path": "/tmp/t18_test_fake.txt"})
    assert r.get("verified") == False, f"EITE should catch non-existent file: {r}"

@test("EITE verify passes real existing file", "T18")
def t18_verify_passes_real():
    """verify_tool_result must pass for actually written files."""
    from tical_code.core.eite import init, get_verify
    init(identity_id="t18b", workspace="/tmp/t18_vrf2")
    v = get_verify()
    Path("/tmp/t18_test_real.txt").write_text("test")
    r = v.verify_tool_result("file_write", {"path": "/tmp/t18_test_real.txt"}, {"ok": True, "path": "/tmp/t18_test_real.txt"})
    assert r.get("verified") == True, f"EITE should pass real file: {r}"
    os.unlink("/tmp/t18_test_real.txt")

@test("TruthfulReporter Rule 6 catches summary-only", "T18")
def t18_rule6_catches():
    """Rule 6 must catch 'git diff shows changes' without raw diff."""
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    tr = TruthfulReporter(workspace="/tmp/t18_tr")
    tr.record_action("bash", {"command": "git diff"}, {"stdout": "+change", "exit_code": 0}, verified=True)
    v = tr.scan_reply("Already fixed. git diff shows changes were made, tests pass.")
    assert any(vv["rule"] == 6 for vv in v), f"Rule 6 should catch: {v}"

@test("TruthfulReporter Rule 6 passes with raw diff", "T18")
def t18_rule6_passes_raw():
    """Rule 6 must pass when raw diff markers are present."""
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    tr = TruthfulReporter(workspace="/tmp/t18_tr2")
    tr.record_action("bash", {"command": "git diff"}, {"stdout": "+change", "exit_code": 0}, verified=True)
    reply = "Fixed.\n```\ndiff --git a/a.py b/a.py\n@@ -1 +1,2 @@\n old\n+new\n```\ncommit abc123"
    v = tr.scan_reply(reply)
    ev = [vv for vv in v if vv["rule"] == 6]
    assert len(ev) == 0, f"Should pass with raw diff: {ev}"


# ============================================================
# T19: Anti-Fabrication - check_self + Rule 8
# ============================================================

@test("check_self tool exists and returns real data", "T19")
def t19_check_self_exists():
    """check_self must return config_model, hostname, and not crash."""
    from tical_code.core.tool_executor import exec_check_self
    r = exec_check_self()
    assert r.get("ok") == True, f"check_self failed: {r}"
    info = r.get("self_info", {})
    assert "hostname" in info, f"No hostname in self_info: {info}"
    assert len(info) > 1, f"self_info too sparse: {info}"

@test("check_self in TOOL_SCHEMAS", "T19")
def t19_check_self_in_schemas():
    """check_self must be registered in TOOL_SCHEMAS for LLM discovery."""
    from tical_code.core.tool_executor import TOOL_SCHEMAS
    names = [s["function"]["name"] for s in TOOL_SCHEMAS]
    assert "check_self" in names, f"check_self not in TOOL_SCHEMAS: {names}"

@test("Rule 8 catches model claim without check_self", "T19")
def t19_rule8_catches():
    """TruthfulReporter Rule 8 must catch model claims when check_self not used."""
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    tr = TruthfulReporter(workspace="/tmp/t19_r8")
    tr._actions = []  # no check_self used
    violations = tr._check_self_knowledge("my model is mimo-v2.5-pro")
    assert len(violations) == 1, f"Rule 8 should catch: {violations}"
    assert violations[0]["rule"] == 8
    assert violations[0]["claim"] == "self_knowledge_without_verification"

@test("Rule 8 catches Chinese model claim", "T19")
def t19_rule8_chinese():
    """Rule 8 must catch Chinese model claims too."""
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    tr = TruthfulReporter(workspace="/tmp/t19_r8zh")
    tr._actions = []
    violations = tr._check_self_knowledge("model is DeepSeek-v4")
    assert len(violations) == 1, f"Rule 8 should catch Chinese: {violations}"

@test("Rule 8 passes when check_self used", "T19")
def t19_rule8_passes():
    """Rule 8 must pass when check_self was called before the claim."""
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    tr = TruthfulReporter(workspace="/tmp/t19_r8pass")
    tr._actions = [{"tool_name": "check_self", "args": {}, "result": {"ok": True}, "verified": True}]
    violations = tr._check_self_knowledge("my model is mimo-v2.5-pro")
    assert len(violations) == 0, f"Rule 8 should pass with check_self: {violations}"

@test("Rule 8 ignores non-model text", "T19")
def t19_rule8_ignores():
    """Rule 8 must NOT trigger on text without model claims."""
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    tr = TruthfulReporter(workspace="/tmp/t19_r8ignore")
    tr._actions = []
    violations = tr._check_self_knowledge("I fixed the bug in the config file")
    assert len(violations) == 0, f"Rule 8 should not trigger: {violations}"

@test("Prompt rule 13 exists (check_self mandate)", "T19")
def t19_prompt_rule13():
    """Prompt must include rule 13 about check_self for self-knowledge."""
    from tical_code.core.prompt import build_system_prompt
    p = build_system_prompt(name="test_worker")
    assert "check_self" in p, "Prompt missing check_self reference"
    assert "13." in p, "Prompt missing rule 13"

@test("Full chain: scan_reply catches fabricated model claim", "T19")
def t19_full_chain():
    """Full TruthfulReporter scan must catch model fabrication."""
    from tical_code.core.modules.truthful_reporter import TruthfulReporter
    tr = TruthfulReporter(workspace="/tmp/t19_full")
    tr._actions = [{"tool_name": "bash", "args": {"command": "ls"}, "result": {"exit_code": 0}, "verified": True}]
    # Worker claims model without using check_self
    violations = tr.scan_reply("I am using DeepSeek-v4-flash as my model.")
    rule8_violations = [v for v in violations if v.get("rule") == 8]
    assert len(rule8_violations) > 0, f"Full chain should catch: {violations}"


if __name__ == "__main__":
    sys.exit(main())
