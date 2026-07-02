#!/usr/bin/env python3
"""Apply EITE v3 patches to prompt.py and verify_engine_v2.py"""
import re, sys

# ===== PATCH 1: prompt.py =====
prompt_path = "tical_code/core/prompt.py"
with open(prompt_path) as f:
    content = f.read()

lines = content.split("\n")

# Find Rule 13 line index
rule13_idx = None
for i, line in enumerate(lines):
    if "13. SELF-KNOWLEDGE RULE" in line:
        rule13_idx = i
        break

if rule13_idx is None:
    print("ERROR: Could not find Rule 13 in prompt.py")
    sys.exit(1)

print(f"Found Rule 13 at line {rule13_idx + 1}")

# Find where the rules list ends (the closing bracket ']')
rules_end = None
for i in range(rule13_idx + 1, min(rule13_idx + 20, len(lines))):
    if lines[i].strip() == "]":
        rules_end = i
        break

if rules_end is None:
    print("ERROR: Could not find end of rules list")
    sys.exit(1)

print(f"Rules list ends at line {rules_end + 1}")

# Remove existing Rule 14-15 lines (between rule13 and rules_end)
old_rules = lines[rule13_idx + 1:rules_end]
print(f"Removing {len(old_rules)} existing rule lines:")
for r in old_rules:
    print(f"  {r.strip()[:80]}")

# New rules to insert
new_rules = [
    '        "14. THINK BEFORE CODING: Before writing any code, you MUST first state your understanding of the problem and your proposed solution. Say \\"I understand: ...\\" and \\"My plan: ...\\" before any code block.",',
    '        "15. SIMPLICITY CHECK: Keep code blocks under 200 lines. If your code exceeds 200 lines, consider breaking it into smaller functions or modules.",',
    '        "16. EVIDENCE VERIFICATION: After modifying a system file (nginx config, server.py, etc.), you MUST re-read it to confirm the change took effect. Do not claim success without verification.",',
]

# Reconstruct
new_lines = lines[:rule13_idx + 1] + new_rules + lines[rules_end:]
with open(prompt_path, "w") as f:
    f.write("\n".join(new_lines))

print(f"\nprompt.py patched: removed {len(old_rules)} lines, added {len(new_rules)} lines")

# ===== PATCH 2: verify_engine_v2.py =====
vpath = "tical_code/core/eite/verify_engine_v2.py"
with open(vpath) as f:
    vcontent = f.read()

# 2a: Add regex patterns after _COMPLETION_RE block
old_2a = '''_COMPLETION_RE = re.compile(
    r"\\b(done|completed?|finished|resolved|accomplished)\\b",
    re.I,
)
_PROGRESS_RE = re.compile('''

new_2a = '''_COMPLETION_RE = re.compile(
    r"\\b(done|completed?|finished|resolved|accomplished)\\b",
    re.I,
)
_CODE_BLOCK_RE = re.compile(r'```|`[^`]+`', re.I)
_PLAN_KEYWORDS_RE = re.compile(r'\\b(understand|plan|propose|approach|task)\\b', re.I)
_CODE_LINE_COUNT_RE = re.compile(r'^', re.MULTILINE)
_PROGRESS_RE = re.compile('''

if old_2a in vcontent:
    vcontent = vcontent.replace(old_2a, new_2a, 1)
    print("\nv2a: Added 3 regex patterns after _COMPLETION_RE")
else:
    print("\nWARNING: v2a pattern not found, checking if already applied...")
    if "_CODE_BLOCK_RE" in vcontent:
        print("  Already applied!")
    else:
        print("  ERROR: Pattern not found and not already applied")
        sys.exit(1)

# 2b: Add methods after the VerificationResult return in verify_reply
old_2b = '''        return VerificationResult(
            passed=len(violations) == 0,
            violations=violations,
            action=action,
            corrections=[v.detail for v in violations],
        )

    # ------------------------------------------------------------------
    # Helpers'''

new_2b = '''        return VerificationResult(
            passed=len(violations) == 0,
            violations=violations,
            action=action,
            corrections=[v.detail for v in violations],
        )

    # ------------------------------------------------------------------
    # EITE v3: Think Before Coding (Rule 9)
    # ------------------------------------------------------------------

    def _check_think_before_code(self, reply: str, conv: list) -> list[tuple]:
        """Rule 9: If reply contains code, check that LLM stated understanding first."""
        corrections = []
        if not _CODE_BLOCK_RE.search(reply):
            return corrections
        # Check if LLM stated understanding in this turn
        stated_plan = False
        for msg in reversed(conv):
            if msg.get("role") in ("user", "system"):
                break
            if msg.get("role") == "assistant" and _PLAN_KEYWORDS_RE.search(msg.get("content", "")):
                stated_plan = True
                break
        if not stated_plan:
            corrections.append("Code provided without stating understanding first")
        return corrections

    # ------------------------------------------------------------------
    # EITE v3: Simplicity Check (Rule 10)
    # ------------------------------------------------------------------

    def _check_code_simplicity(self, reply: str) -> list[tuple]:
        """Rule 10: Warn if code block exceeds 200 lines."""
        corrections = []
        in_block = False
        block_lines = 0
        for line in reply.split("\\n"):
            if line.strip().startswith("```"):
                if in_block:
                    if block_lines > 200:
                        corrections.append(f"Code block too long ({block_lines} lines, max 200)")
                    in_block = False
                    block_lines = 0
                else:
                    in_block = True
            elif in_block:
                block_lines += 1
        return corrections

    # ------------------------------------------------------------------
    # EITE v3: Claimed File Must Be Verified (Rule 11)
    # ------------------------------------------------------------------

    def _check_file_verification(self, reply: str) -> list[tuple]:
        """Rule 11: If LLM claims to have modified a system file,
        check that a read-back was performed for verification."""
        corrections = []
        # Check tool calls for writes to system files followed by reads
        _WRITE_CMDS = re.compile(r'(?:sed\\s+-i|tee\\s+|cp\\s+|mv\\s+|echo\\s+.*>|python3.*open.*[\'"]w)', re.I)
        _READ_CMDS = re.compile(r'(?:cat\\s+|head\\s+|tail\\s+|grep\\s+|less\\s+|more\\s+|python3.*open)', re.I)
        _SYSTEM_FILE_RE = re.compile(r'(/etc/|/etc/)', re.I)
        for action in self._actions:
            cmd = str(action.get("args", {}).get("command", ""))
            if _WRITE_CMDS.search(cmd) and _SYSTEM_FILE_RE.search(cmd):
                # Write to a system file detected - check for read-back later
                has_readback = False
                for later_action in self._actions:
                    later_cmd = str(later_action.get("args", {}).get("command", ""))
                    if _READ_CMDS.search(later_cmd) and _SYSTEM_FILE_RE.search(later_cmd):
                        has_readback = True
                        break
                if not has_readback:
                    corrections.append(f"Modified system file without read-back verification: {cmd[:80]}")
        return corrections

    # ------------------------------------------------------------------
    # Helpers'''

if old_2b in vcontent:
    vcontent = vcontent.replace(old_2b, new_2b, 1)
    print("v2b: Added 3 check methods after verify_reply")
else:
    print("WARNING: v2b pattern not found, checking if already applied...")
    if "_check_think_before_code" in vcontent:
        print("  Already applied!")
    else:
        print("  ERROR: Pattern not found and not already applied")
        sys.exit(1)

# 2c: Add calls inside verify_reply, before the return
old_2c = '''        if has_critical:
            action = "block"
        elif has_high:
            action = "retry"
        elif violations:
            action = "rewrite"
        else:
            action = "allow"

        return VerificationResult('''

new_2c = '''        if has_critical:
            action = "block"
        elif has_high:
            action = "retry"
        elif violations:
            action = "rewrite"
        else:
            action = "allow"

        # EITE v3: Think Before Coding + Simplicity + File Verification
        for detail in self._check_think_before_code(reply, conv):
            violations.append(Violation(rule="think_before_code", detail=detail, severity="high"))
        for detail in self._check_code_simplicity(reply):
            violations.append(Violation(rule="simplicity_check", detail=detail, severity="medium"))
        for detail in self._check_file_verification(reply):
            violations.append(Violation(rule="file_verification", detail=detail, severity="high"))

        if has_critical:
            action = "block"
        elif has_high or any(v.rule in ("think_before_code", "file_verification") for v in violations):
            action = "retry"
        elif violations:
            action = "rewrite"

        return VerificationResult('''

if old_2c in vcontent:
    vcontent = vcontent.replace(old_2c, new_2c, 1)
    print("v2c: Added 3 rule calls before return in verify_reply")
else:
    print("WARNING: v2c pattern not found, checking if already applied...")
    if "think_before_code" in vcontent:
        print("  Already applied!")
    else:
        print("  ERROR: Pattern not found and not already applied")
        sys.exit(1)

with open(vpath, "w") as f:
    f.write(vcontent)

print("\nverify_engine_v2.py patched successfully")

# ===== VERIFY SYNTAX =====
import py_compile
print("\n=== Syntax Verification ===")
try:
    py_compile.compile(prompt_path, doraise=True)
    print(f"  {prompt_path}: OK")
except py_compile.PyCompileError as e:
    print(f"  {prompt_path}: FAILED - {e}")
    sys.exit(1)

try:
    py_compile.compile(vpath, doraise=True)
    print(f"  {vpath}: OK")
except py_compile.PyCompileError as e:
    print(f"  {vpath}: FAILED - {e}")
    sys.exit(1)

print("\n=== All patches applied and verified ===")
