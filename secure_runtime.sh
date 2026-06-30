#!/bin/bash
# tical-code OS-level Security Hardening Script
# 
# Core principle: Self-protection inside a Python process is a false premise
# (AI with shell access can bypass it)
# The real defense is OS-level file permissions — make the runtime user
# read-only for core code
#
# Usage:
#   1. First deploy: bash secure_runtime.sh setup
#   2. Start service: bash secure_runtime.sh start
#   3. Update code: bash secure_runtime.sh unlock && update && bash secure_runtime.sh lock

set -euo pipefail

# Configuration
TICAL_DIR="$(cd "$(dirname "$0")" && pwd)"
CORE_DIR="${TICAL_DIR}/tical_code/core"
RUN_USER="${TICAL_RUNTIME_USER:-ticalai}"
SERVICE_USER="${TICAL_SERVICE_USER:-ticalsvc}"

# Protected file list (aligned with tool_router.py PROTECTED_FILENAMES)
PROTECTED_FILES=(
    "memory_evolve.py"
    "cron_scheduler.py"
    "tool_router.py"
    "sandbox.py"
    "self_repair.py"
    "truthful_reporting.py"
    "identity.py"
    "worker_framework.py"
    "auth.py"
    "__init__.py"
)

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# =============================================================================
# Lock — Set read-only protection
# =============================================================================
lock() {
    log_info "Locking core files to read-only..."

    for f in "${PROTECTED_FILES[@]}"; do
        filepath="${CORE_DIR}/${f}"
        if [ -f "$filepath" ]; then
            # Method 1: chattr +i (Linux immutable — cannot be modified even by root unless chattr -i first)
            if command -v chattr &>/dev/null && [ "$(id -u)" -eq 0 ]; then
                chattr +i "$filepath" 2>/dev/null && log_info "  chattr +i $f" || true
            fi
            # Method 2: chmod 444 (all users read-only)
            chmod 444 "$filepath" 2>/dev/null && log_info "  chmod 444 $f" || true
        fi
    done

    # Lock entire core directory as non-writable (directory itself still readable and executable)
    chmod 555 "$CORE_DIR" 2>/dev/null && log_info "  chmod 555 core/" || true

    log_info "Core files locked ✓"
}

# =============================================================================
# Unlock — Remove read-only protection (use when updating code)
# =============================================================================
unlock() {
    log_warn "Unlocking core files..."

    for f in "${PROTECTED_FILES[@]}"; do
        filepath="${CORE_DIR}/${f}"
        if [ -f "$filepath" ]; then
            # Remove chattr +i
            if command -v chattr &>/dev/null && [ "$(id -u)" -eq 0 ]; then
                chattr -i "$filepath" 2>/dev/null && log_info "  chattr -i $f" || true
            fi
            # Restore write permissions
            chmod 644 "$filepath" 2>/dev/null && log_info "  chmod 644 $f" || true
        fi
    done

    # Restore directory write permissions
    chmod 755 "$CORE_DIR" 2>/dev/null && log_info "  chmod 755 core/" || true

    log_warn "Core files unlocked — re-lock after updates are complete"
}

# =============================================================================
# Setup — Create runtime user and set permissions
# =============================================================================
setup() {
    log_info "Setting up OS-level security hardening..."

    # 1. Create dedicated runtime user (if not exists)
    if ! id "$RUN_USER" &>/dev/null; then
        if [ "$(id -u)" -eq 0 ]; then
            useradd -r -s /bin/false "$RUN_USER" 2>/dev/null || true
            log_info "Created runtime user: $RUN_USER"
        else
            log_warn "Not running as root, skipping runtime user creation. Run setup as root."
        fi
    fi

    # 2. Set file ownership — core files belong to service user, runtime user only has read access
    if [ "$(id -u)" -eq 0 ]; then
        chown -R "${SERVICE_USER}:${SERVICE_USER}" "${CORE_DIR}" 2>/dev/null || true
        log_info "Core file ownership set to $SERVICE_USER"
    fi

    # 3. Lock
    lock

    # 4. Verify
    verify

    log_info "Setup complete ✓"
    log_info ""
    log_info "Start service with: sudo -u $RUN_USER python -m tical_code"
    log_info "Update code with:   bash secure_runtime.sh unlock && update && bash secure_runtime.sh lock"
}

# =============================================================================
# Verify — Check protection status
# =============================================================================
verify() {
    log_info "Verifying protection status..."
    local all_good=true

    for f in "${PROTECTED_FILES[@]}"; do
        filepath="${CORE_DIR}/${f}"
        if [ -f "$filepath" ]; then
            # Check if writable
            if [ -w "$filepath" ]; then
                log_error "  $f is still writable!"
                all_good=false
            else
                log_info "  $f read-only ✓"
            fi
        else
            log_warn "  $f does not exist"
        fi
    done

    if [ "$all_good" = true ]; then
        log_info "All protected files verified ✓"
    else
        log_error "Some files are not protected! Run: bash secure_runtime.sh lock"
        return 1
    fi
}

# =============================================================================
# Start — Launch service as restricted user
# =============================================================================
start() {
    lock  # Ensure lock before starting

    if [ "$(id -u)" -eq 0 ]; then
        log_info "Starting service as restricted user $RUN_USER..."
        exec sudo -u "$RUN_USER" python -m tical_code "$@"
    else
        log_warn "Not running as root, starting directly (no OS-level isolation)..."
        exec python -m tical_code "$@"
    fi
}

# =============================================================================
# Main
# =============================================================================
case "${1:-}" in
    setup)   setup ;;
    lock)    lock ;;
    unlock)  unlock ;;
    verify)  verify ;;
    start)   shift; start "$@" ;;
    *)
        echo "Usage: $0 {setup|lock|unlock|verify|start}"
        echo ""
        echo "  setup  — First deploy: create runtime user + lock core files"
        echo "  lock   — Lock core files to read-only (chattr +i + chmod 444)"
        echo "  unlock — Unlock core files (use when updating code)"
        echo "  verify — Verify protection status"
        echo "  start  — Lock and start service as restricted user"
        exit 1
        ;;
esac
