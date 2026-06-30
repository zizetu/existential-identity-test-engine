#!/bin/bash
# self_check.sh — Self-check runner for project benchmarks
# Usage: bash scripts/self_check.sh [--all|--security|--english|--compile|--version|--wiring|--desensitize]
# Exit: 0 = clean pass, 1 = issues found

set -uo pipefail
ISSUES=0
REPO=$(basename "$(pwd)")
FAIL_LOG=""

banner() { echo "=== $1 ==="; }
fail() { FAIL_LOG+="  FAIL: $1"$'\n'; ISSUES=$((ISSUES + 1)); }
ok()   { echo "  OK: $1"; }
count_lines() { wc -l | tr -d ' '; }

# ── Security ──────────────────────────────────────────────
check_security() {
  banner "Security Gates"
  
  # shell=True (exclude comments/docs)
  hits=$(grep -rn 'shell\s*=\s*True' --include='*.py' tical_code/core/ 2>/dev/null \
    | grep -v '# noqa' | grep -v 'test_' | grep -v '__pycache__' \
    | grep -v 'never uses shell=True' | grep -v 'never shell=True' | count_lines)
  [ "$hits" -eq 0 ] && ok "shell=True" || fail "$hits shell=True usage(s)"
  
  # bare except
  hits=$(grep -rn 'except:' --include='*.py' tical_code/core/ 2>/dev/null \
    | grep -v 'except Exception' | grep -v 'except (' \
    | grep -v 'except BaseException' | grep -v '# noqa' | grep -v '__pycache__' | count_lines)
  [ "$hits" -eq 0 ] && ok "bare except" || fail "$hits bare except(s)"
  
  # .gitignore patterns
  for p in 'credentials*' 'secrets*' '*.pem' '*.key' 'config.local.*'; do
    grep -qF "$p" .gitignore 2>/dev/null || fail ".gitignore missing: $p"
  done
  ok ".gitignore"
  
  # Hardcoded API keys (quick scan)
  hits=$(grep -rn 'sk-[a-zA-Z0-9]\{20,\}' --include='*.py' --include='*.md' . 2>/dev/null \
    | grep -v '.git/' | grep -v '\*\*\*\*' | grep -v '/tests/' | grep -v 'test_' | count_lines)
  [ "$hits" -eq 0 ] && ok "API keys" || fail "$hits hardcoded API key pattern(s)"
}

# ── English-Only ──────────────────────────────────────────
check_english() {
  banner "English-Only Code"
  
  hits=$(grep -rP '[\x{4e00}-\x{9fff}]' --include='*.py' tical_code/core/ 2>/dev/null \
    | grep -v '.git/' | grep -v '__pycache__' | count_lines)
  [ "$hits" -eq 0 ] && ok "0 CJK characters" || fail "$hits CJK character(s)"
}

# ── Compile ───────────────────────────────────────────────
check_compile() {
  banner "Python Compile"
  
  errors=0
  while IFS= read -r f; do
    python3 -m py_compile "$f" 2>/dev/null || errors=$((errors + 1))
  done < <(find tical_code/core -name '*.py' -not -path '*/__pycache__/*' 2>/dev/null)
  [ "$errors" -eq 0 ] && ok "all files compile" || fail "$errors compile error(s)"
}

# ── Version Coherence ─────────────────────────────────────
check_version() {
  banner "Version Coherence"
  
  if [ -f VERSION ]; then
    V=$(cat VERSION)
    grep -qF "$V" CLAUDE.md 2>/dev/null && ok "CLAUDE.md == $V" || fail "CLAUDE.md version mismatch (expected $V)"
  else
    fail "VERSION file missing"
  fi
}

# ── Wiring ────────────────────────────────────────────────
check_wiring() {
  banner "Wiring Integrity"
  
  # Verify expected module files exist  
  expected_modules=(
    session_manager.py context_compactor.py doom_loop.py constitution.py
    truthful_reporting.py security_baseline.py
    trace_recorder.py memory_store.py message_adapter.py memory_profiler.py
    model_failover.py verification_broadcast.py cron.py
    memory_evolve.py
  )
  # Full-profile-only modules
  full_modules=(decision_engine.py checkpoint.py self_repair.py
    task_state.py context_manager.py sandbox.py reflection.py
    subagent.py)
  
  # Verify expected module files exist (recursive find — files may be in subdirectories)
  issues=0
  for mf in "${expected_modules[@]}"; do
    if ! find tical_code/core -name "$mf" -not -path '*/__pycache__/*' 2>/dev/null | grep -q .; then
      echo "  FAIL: $mf missing"
      issues=$((issues + 1))
    fi
  done
  # Full-profile-only modules
  if [ "$REPO" = "tical-code" ]; then
    for mf in "${full_modules[@]}"; do
      if ! find tical_code/core -name "$mf" -not -path '*/__pycache__/*' 2>/dev/null | grep -q .; then
        echo "  FAIL: $mf missing (full profile)"
        issues=$((issues + 1))
      fi
    done
  fi
  
  [ "$issues" -eq 0 ] && ok "all expected module files present"
  [ "$issues" -gt 0 ] && ISSUES=$((ISSUES + issues))
  
  # Light profile integrity (eite-project only)
  if [ "$REPO" = "eite-project" ]; then
    for f in task_state.py context_manager.py checkpoint.py self_repair.py decision_engine.py; do
      [ -f "tical_code/core/$f" ] && fail "forbidden: $f in light deployment" || ok "no $f"
    done
  fi
}

# ── Desensitization ───────────────────────────────────────
check_desensitize() {
  banner "Docs Desensitization"
  
  # Real IPs in docs (exclude RFC-reserved + Cloudflare CDN)
  hits=0
  while IFS= read -r line; do
    ip=$(echo "$line" | grep -oP '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -1)
    if [ -n "$ip" ]; then
      # Skip RFC-reserved
      [[ "$ip" =~ ^127\. ]] && continue
      [[ "$ip" =~ ^10\. ]] && continue
      [[ "$ip" =~ ^172\.(1[6-9]|2[0-9]|3[0-1])\. ]] && continue
      [[ "$ip" =~ ^192\.168\. ]] && continue
      [[ "$ip" =~ ^169\.254\. ]] && continue
      # Skip Cloudflare public CDN IPs (dynamic; only the most common ones listed)
      [[ "$ip" == "172.67.196.250" ]] && continue
      [[ "$ip" == "104.21.34.42" ]] && continue
      hits=$((hits + 1))
    fi
  done < <(grep -rnP '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' docs/ --include='*.md' 2>/dev/null | grep -v '\[REDACTED\]' | grep -v 'MESH_.*_IP')
  [ "$hits" -eq 0 ] && ok "no real IPs in docs" || fail "$hits real IP(s) in docs"
  
  # HTML purpose comments in key docs
  for f in docs/full-asset-inventory.md docs/MAINTENANCE_CHECKLIST.md; do
    if [ -f "$f" ]; then
      head -5 "$f" | grep -q '<!--' && ok "HTML comment: $f" || fail "missing HTML comment: $f"
    fi
  done
}

# ── Performance ───────────────────────────────────────────
check_perf() {
  banner "Performance"
  
  if [ -f tical_code/core/model_failover.py ]; then
    pt=$(grep -oP 'PER_CALL_TIMEOUT\s*=\s*\K\d+' tical_code/core/model_failover.py 2>/dev/null | head -1)
    if [ -n "$pt" ] && [ "$pt" -le 15 ] 2>/dev/null; then
      ok "PER_CALL_TIMEOUT=$pt"
    else
      fail "PER_CALL_TIMEOUT=${pt:-NOT_FOUND} (should ≤15)"
    fi
  fi
  
  if [ -f tical_code/core/llm_backend.py ]; then
    grep -q 'timeout=60' tical_code/core/llm_backend.py 2>/dev/null \
      && fail "llm_backend timeout=60 (should ≤15)" \
      || ok "llm_backend timeout OK"
  fi
}

# ── Main ──────────────────────────────────────────────────
case "${1:-}" in
  --security)    check_security ;;
  --english)     check_english ;;
  --compile)     check_compile ;;
  --version)     check_version ;;
  --wiring)      check_wiring ;;
  --desensitize) check_desensitize ;;
  --perf)        check_perf ;;
  --all|"")
    check_security
    check_english
    check_compile
    check_version
    check_wiring
    check_desensitize
    check_perf
    ;;
  *)
    echo "Usage: $0 [--all|--security|--english|--compile|--version|--wiring|--desensitize|--perf]"
    exit 2
    ;;
esac

echo ""
echo "$FAIL_LOG"
echo "════════════════════════════════"
echo "Total issues: $ISSUES"
[ "$ISSUES" -eq 0 ] && echo "Status: CLEAN ✓" || echo "Status: NEEDS FIX ✗"
exit $ISSUES
