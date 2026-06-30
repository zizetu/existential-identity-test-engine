#!/usr/bin/env bash
# ==============================================================
# tical-code Full Project Self-Check Script (P3: Self-Check)
# ==============================================================
# Checks:
#   1. Whether worker exits in all screen sessions have logs
#   2. Whether all exit logic records the reason
#   3. Whether watchdog script covers all workers
#   4. Whether systemd service is configured correctly
#   5. Snapshot and death-log directory permissions
#
# Author: Tical
# ==============================================================

set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0
ISSUES=()

check_pass() { PASS=$((PASS + 1)); echo -e "  ${GREEN}✓ PASS${NC}: $1"; }
check_fail() { FAIL=$((FAIL + 1)); echo -e "  ${RED}✗ FAIL${NC}: $1"; ISSUES+=("$1"); }
check_warn() { WARN=$((WARN + 1)); echo -e "  ${YELLOW}⚠ WARN${NC}: $1"; }

TICAL_HOME="${TICAL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CORE_DIR="${TICAL_HOME}/tical_code/core"

echo "========================================="
echo " tical-code Full Project Self-Check"
echo "========================================="
echo " Project path: ${TICAL_HOME}"
echo " Core dir: ${CORE_DIR}"
echo "========================================="
echo ""

# ==============================================================
# Self-Check 1: Whether worker exits in all screen sessions have logs
# ==============================================================
echo -e "${BLUE}[Check 1]${NC} Screen session worker exit log check"

if command -v screen &> /dev/null; then
    SCREEN_SESSIONS=$(screen -ls 2>/dev/null | grep -c "tical\|worker" || true)
    if [ "$SCREEN_SESSIONS" -gt 0 ]; then
        check_warn "Found ${SCREEN_SESSIONS} screen sessions running workers, recommend migrating to systemd"
    else
        check_pass "No screen sessions running workers (systemd recommended)"
    fi
else
    check_pass "screen command not available (using systemd)"
fi

# Check if death log is registered in run_worker.py
if [ -f "${TICAL_HOME}/scripts/run_worker.py" ]; then
    if grep -q "record_death" "${TICAL_HOME}/scripts/run_worker.py" 2>/dev/null; then
        check_pass "run_worker.py has death log registered (signal exit + exception exit)"
    else
        check_fail "run_worker.py missing death log registration"
    fi
    
    if grep -q "sys.excepthook" "${TICAL_HOME}/scripts/run_worker.py" 2>/dev/null; then
        check_pass "run_worker.py has uncaught exception handler registered"
    else
        check_warn "run_worker.py missing uncaught exception handler"
    fi
else
    check_fail "scripts/run_worker.py not found"
fi

echo ""

# ==============================================================
# Self-Check 2: Whether all exit logic records the reason
# ==============================================================
echo -e "${BLUE}[Check 2]${NC} Exit logic log record check"

# Check worker_framework.py shutdown method
if [ -f "${CORE_DIR}/worker_framework.py" ]; then
    # Check death log in shutdown
    if grep -q "record_death" "${CORE_DIR}/worker_framework.py" 2>/dev/null; then
        check_pass "worker_framework.py shutdown() integrates death log"
    else
        check_fail "worker_framework.py shutdown() missing death log"
    fi
    
    # Check snapshot save
    if grep -q "save_snapshot" "${CORE_DIR}/worker_framework.py" 2>/dev/null; then
        check_pass "worker_framework.py shutdown() integrates snapshot save"
    else
        check_fail "worker_framework.py shutdown() missing snapshot save"
    fi
    
    # Check snapshot restore in bootstrap
    if grep -q "try_restore_snapshot" "${CORE_DIR}/worker_framework.py" 2>/dev/null; then
        check_pass "worker_framework.py bootstrap() integrates snapshot restore"
    else
        check_fail "worker_framework.py bootstrap() missing snapshot restore"
    fi
    
    # Check self-heal module initialization
    if grep -q "_init_selfheal_modules" "${CORE_DIR}/worker_framework.py" 2>/dev/null; then
        check_pass "worker_framework.py integrates self-heal module initialization"
    else
        check_fail "worker_framework.py missing self-heal module initialization"
    fi
else
    check_fail "worker_framework.py not found"
fi

# Check session_snapshot.py
if [ -f "${CORE_DIR}/session_snapshot.py" ]; then
    check_pass "session_snapshot.py exists"
    
    # Check atomic write
    if grep -q "os.rename" "${CORE_DIR}/session_snapshot.py" 2>/dev/null; then
        check_pass "session_snapshot.py uses atomic write (os.rename)"
    else
        check_fail "session_snapshot.py does not use atomic write"
    fi
    
    # Check key functions
    for func in save_snapshot load_latest_snapshot record_death cleanup_old_snapshots; do
        if grep -q "def ${func}" "${CORE_DIR}/session_snapshot.py" 2>/dev/null; then
            check_pass "session_snapshot.py contains ${func}()"
        else
            check_fail "session_snapshot.py missing ${func}()"
        fi
    done
else
    check_fail "session_snapshot.py not found"
fi

echo ""

# ==============================================================
# Self-Check 3: Whether watchdog/heartbeat covers all workers
# ==============================================================
echo -e "${BLUE}[Check 3]${NC} Watchdog/heartbeat coverage check"

if [ -f "${CORE_DIR}/heartbeat.py" ]; then
    check_pass "heartbeat.py exists"
    
    # Check key class
    if grep -q "class HeartbeatManager" "${CORE_DIR}/heartbeat.py" 2>/dev/null; then
        check_pass "HeartbeatManager class defined"
    else
        check_fail "HeartbeatManager class not defined"
    fi
    
    # Check zombie detection
    if grep -q "ZOMBIE\|zombie" "${CORE_DIR}/heartbeat.py" 2>/dev/null; then
        check_pass "heartbeat.py includes zombie detection logic"
    else
        check_warn "heartbeat.py may be missing zombie detection"
    fi
else
    check_fail "heartbeat.py not found"
fi

# Check heartbeat integration in worker_framework.py
if [ -f "${CORE_DIR}/worker_framework.py" ]; then
    if grep -q "_init_heartbeat_manager" "${CORE_DIR}/worker_framework.py" 2>/dev/null; then
        check_pass "worker_framework.py integrates HeartbeatManager"
    else
        check_fail "worker_framework.py does not integrate HeartbeatManager"
    fi
fi

echo ""

# ==============================================================
# Self-Check 4: systemd service configuration check
# ==============================================================
echo -e "${BLUE}[Check 4]${NC} systemd service configuration check"

SERVICE_TEMPLATE="${TICAL_HOME}/deploy/tical-code-worker@.service"
if [ -f "$SERVICE_TEMPLATE" ]; then
    check_pass "systemd service template exists"
    
    # Check key configuration
    for key in "Restart=always" "RestartSec=" "KillSignal=SIGTERM" "StandardOutput=journal"; do
        if grep -q "$key" "$SERVICE_TEMPLATE" 2>/dev/null; then
            check_pass "service template includes ${key}"
        else
            check_fail "service template missing ${key}"
        fi
    done
else
    check_fail "systemd service template not found: ${SERVICE_TEMPLATE}"
fi

# Check install script
INSTALL_SCRIPT="${TICAL_HOME}/deploy/install-service.sh"
if [ -f "$INSTALL_SCRIPT" ]; then
    check_pass "install-service.sh exists"
    if [ -x "$INSTALL_SCRIPT" ]; then
        check_pass "install-service.sh is executable"
    else
        check_warn "install-service.sh not executable (needs chmod +x)"
    fi
else
    check_fail "install-service.sh not found"
fi

# Check installed services
if command -v systemctl &> /dev/null; then
    INSTALLED_SERVICES=$(systemctl list-unit-files 2>/dev/null | grep -c "tical-code-worker@" || true)
    if [ "$INSTALLED_SERVICES" -gt 0 ]; then
        check_pass "Installed ${INSTALLED_SERVICES} tical-code-worker services"
    else
        check_warn "No tical-code-worker services installed yet"
    fi
fi

echo ""

# ==============================================================
# Self-Check 5: Snapshot and death-log directory permissions
# ==============================================================
echo -e "${BLUE}[Check 5]${NC} Snapshot and death-log directory permissions"

SNAPSHOT_DIR="${HOME}/.tical-code/snapshots"
DEATH_LOG_DIR="${HOME}/.tical-code/death-log"
DATA_DIR="${HOME}/.tical-code"

# Check main directory
if [ -d "$DATA_DIR" ]; then
    check_pass "~/.tical-code directory exists"
else
    check_warn "~/.tical-code directory does not exist (will be auto-created on first run)"
fi

# Check/create snapshot directory
mkdir -p "$SNAPSHOT_DIR" 2>/dev/null || true
if [ -d "$SNAPSHOT_DIR" ] && [ -w "$SNAPSHOT_DIR" ]; then
    check_pass "snapshot directory writable: ${SNAPSHOT_DIR}"
else
    check_fail "snapshot directory not writable: ${SNAPSHOT_DIR}"
fi

# Check/create death-log directory
mkdir -p "$DEATH_LOG_DIR" 2>/dev/null || true
if [ -d "$DEATH_LOG_DIR" ] && [ -w "$DEATH_LOG_DIR" ]; then
    check_pass "death-log directory writable: ${DEATH_LOG_DIR}"
else
    check_fail "death-log directory not writable: ${DEATH_LOG_DIR}"
fi

# Check existing death logs and snapshots
DEATH_LOGS=$(find "$DEATH_LOG_DIR" -name "*-death.json" 2>/dev/null | wc -l || true)
SNAPSHOTS=$(find "$SNAPSHOT_DIR" -name "*.json" ! -name "*.tmp" ! -name "*.recovered" 2>/dev/null | wc -l || true)
echo -e "  Existing death logs: ${DEATH_LOGS}"
echo -e "  Existing snapshots:  ${SNAPSHOTS}"

echo ""

# ==============================================================
# Self-Check: Module integration check (BUG-2/BUG-3 supplement)
# ==============================================================
echo -e "${BLUE}[Supplementary Check]${NC} Module integration check"

# Check module exports in __init__.py
if [ -f "${CORE_DIR}/__init__.py" ]; then
    for module in "ConstitutionEnforcer" "clarify_goal" "HeartbeatManager" "validate_path_safety" "save_snapshot" "record_death"; do
        if grep -q "$module" "${CORE_DIR}/__init__.py" 2>/dev/null; then
            check_pass "__init__.py exports ${module}"
        else
            check_fail "__init__.py missing ${module} export"
        fi
    done
else
    check_fail "__init__.py not found"
fi

# Check integration in worker_loop.py
if [ -f "${CORE_DIR}/worker_loop.py" ]; then
    for hook in "constitution_check\|clarify_check\|doom_loop_check\|reflect_on_response\|compact_context"; do
        if grep -q "$hook" "${CORE_DIR}/worker_loop.py" 2>/dev/null; then
            check_pass "worker_loop.py integrates ${hook}"
        else
            check_fail "worker_loop.py missing ${hook} integration"
        fi
    done
else
    check_fail "worker_loop.py not found"
fi

echo ""

# ==============================================================
# Summary
# ==============================================================
TOTAL=$((PASS + FAIL + WARN))
echo "========================================="
echo " Self-Check Results Summary"
echo "========================================="
echo -e " Pass: ${GREEN}${PASS}${NC}"
echo -e " Fail: ${RED}${FAIL}${NC}"
echo -e " Warn: ${YELLOW}${WARN}${NC}"
echo -e " Total: ${TOTAL}"
echo ""

if [ ${#ISSUES[@]} -gt 0 ]; then
    echo -e "${RED}Issues needing fixes:${NC}"
    for issue in "${ISSUES[@]}"; do
        echo -e "  - ${RED}${issue}${NC}"
    done
    echo ""
fi

if [ $FAIL -eq 0 ]; then
    echo -e "${GREEN}All critical checks passed! Worker self-heal system is ready.${NC}"
    exit 0
else
    echo -e "${RED}${FAIL} failures remaining. Please fix and re-run self-check.${NC}"
    exit 1
fi
