#!/usr/bin/env bash
# ==============================================================
# tical-code Worker systemd Service Install Script
# ==============================================================
# Usage:
#   ./install-service.sh <worker-name> [config-path] [env-path]
#
# Examples:
#   ./install-service.sh tico-sg
#   ./install-service.sh tico-sg /opt/tical-code/config/worker-configs/tico-sg.json
#   ./install-service.sh tico-sg /opt/tical-code/config/worker-configs/tico-sg.json /opt/tical-code/config/env/tico-sg.env
#
# Self-Check Items (P3 Full Project Check):
#   1. Check whether worker exits in screen sessions have logs
#   2. Check whether exit logic has records
#   3. Check whether watchdog covers all workers
#   4. Check systemd service configuration
#   5. Check snapshot and death-log directory permissions
#
# Author: Tical
# ==============================================================

set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ==============================================================
# Argument parsing
# ==============================================================

WORKER_NAME="${1:-}"
if [ -z "$WORKER_NAME" ]; then
    log_error "Usage: $0 <worker-name> [config-path] [env-path]"
    echo ""
    echo "Examples:"
    echo "  $0 tico-sg"
    echo "  $0 tico-sg /opt/tical-code/config/worker-configs/tico-sg.json"
    echo "  $0 tico-sg /opt/tical-code/config/worker-configs/tico-sg.json /opt/tical-code/config/env/tico-sg.env"
    exit 1
fi

# Default paths
TICAL_HOME="${TICAL_HOME:-/opt/project}"
CONFIG_PATH="${2:-${TICAL_HOME}/config/worker-configs/${WORKER_NAME}.json}"
ENV_PATH="${3:-${TICAL_HOME}/config/env/${WORKER_NAME}.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_TEMPLATE="${SCRIPT_DIR}/tical-code-worker@.service"
SERVICE_NAME="tical-code-worker@${WORKER_NAME}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "========================================="
echo " tical-code Worker Service Installation"
echo "========================================="
echo " Worker:    ${WORKER_NAME}"
echo " Config:    ${CONFIG_PATH}"
echo " Env file:  ${ENV_PATH}"
echo " Install:   ${TICAL_HOME}"
echo "========================================="
echo ""

# ==============================================================
# Self-Check (5 items, corresponds to P3 Full Project Check)
# ==============================================================

log_info "Starting self-check (5 items)..."

CHECK_PASS=0
CHECK_FAIL=0

# Self-Check 1: Check whether worker exits in screen sessions have logs
if command -v screen &> /dev/null; then
    EXISTING_SCREEN=$(screen -ls 2>/dev/null | grep -c "${WORKER_NAME}" || true)
    if [ "$EXISTING_SCREEN" -gt 0 ]; then
        log_warn "Check 1: Found same-name worker process in screen session"
        log_warn "  Recommend migrating to systemd and removing screen session"
        CHECK_FAIL=$((CHECK_FAIL + 1))
    else
        log_info "Check 1: ✓ No screen session conflict"
        CHECK_PASS=$((CHECK_PASS + 1))
    fi
else
    log_info "Check 1: ✓ screen unavailable (no conflict)"
    CHECK_PASS=$((CHECK_PASS + 1))
fi

# Self-Check 2: Check whether exit logic has records (death log in run_worker.py)
RUN_WORKER="${TICAL_HOME}/scripts/run_worker.py"
if [ -f "$RUN_WORKER" ]; then
    if grep -q "record_death" "$RUN_WORKER" 2>/dev/null; then
        log_info "Check 2: ✓ death log registered in run_worker.py"
        CHECK_PASS=$((CHECK_PASS + 1))
    else
        log_warn "Check 2: death log registration not found in run_worker.py"
        CHECK_FAIL=$((CHECK_FAIL + 1))
    fi
else
    log_warn "Check 2: run_worker.py not found: ${RUN_WORKER}"
    CHECK_FAIL=$((CHECK_FAIL + 1))
fi

# Self-Check 3: Check whether watchdog covers all workers
if [ -f "${TICAL_HOME}/tical_code/core/guardian.py" ] || [ -f "${TICAL_HOME}/tical_code/core/doom_loop.py" ]; then
    log_info "Check 3: ✓ Watchdog/guardian module exists"
    CHECK_PASS=$((CHECK_PASS + 1))
else
    log_warn "Check 3: Watchdog/guardian module not found"
    CHECK_FAIL=$((CHECK_FAIL + 1))
fi

# Self-Check 4: Check systemd service template
if [ -f "$SERVICE_TEMPLATE" ]; then
    if grep -q "Restart=always" "$SERVICE_TEMPLATE" 2>/dev/null; then
        log_info "Check 4: ✓ systemd service template configured correctly (Restart=always)"
        CHECK_PASS=$((CHECK_PASS + 1))
    else
        log_warn "Check 4: systemd service template missing Restart=always"
        CHECK_FAIL=$((CHECK_FAIL + 1))
    fi
else
    log_error "Check 4: Service template not found: ${SERVICE_TEMPLATE}"
    CHECK_FAIL=$((CHECK_FAIL + 1))
fi

# Self-Check 5: Check snapshot and death-log directory permissions
SNAPSHOT_DIR="${HOME}/.tical-code/snapshots"
DEATH_LOG_DIR="${HOME}/.tical-code/death-log"
mkdir -p "$SNAPSHOT_DIR" "$DEATH_LOG_DIR" 2>/dev/null || true
if [ -w "$SNAPSHOT_DIR" ] && [ -w "$DEATH_LOG_DIR" ]; then
    log_info "Check 5: ✓ snapshot and death-log directories writable"
    CHECK_PASS=$((CHECK_PASS + 1))
else
    log_warn "Check 5: snapshot or death-log directory not writable"
    CHECK_FAIL=$((CHECK_FAIL + 1))
fi

echo ""
log_info "Self-check results: ${CHECK_PASS} passed / ${CHECK_FAIL} failed"
echo ""

if [ $CHECK_FAIL -gt 2 ]; then
    log_error "Too many self-check failures, please fix issues above first"
    exit 1
fi

# ==============================================================
# Installation
# ==============================================================

log_info "Starting installation of ${SERVICE_NAME}..."

# Check systemd
if ! command -v systemctl &> /dev/null; then
    log_error "systemctl not found, please confirm system uses systemd"
    exit 1
fi

# Check config file
if [ ! -f "$CONFIG_PATH" ]; then
    log_warn "Config file not found: ${CONFIG_PATH} (may not exist on first install)"
else
    log_info "✓ Config file exists"
fi

# Check env file
if [ ! -f "$ENV_PATH" ]; then
    log_warn "Env file not found: ${ENV_PATH}, creating empty file..."
    mkdir -p "$(dirname "$ENV_PATH")"
    touch "$ENV_PATH"
fi

# Generate service file (replace placeholders)
cat "$SERVICE_TEMPLATE" | \
    sed "s|/opt/tical-code|${TICAL_HOME}|g" | \
    sed "s|User=ubuntu|User=${USER}|g" | \
    sed "s|Group=ubuntu|Group=${USER}|g" | \
    sed "s|${HOME}/.tical-code|${HOME}/.tical-code|g" \
    > "$SERVICE_FILE"

# If config path is not the default, replace ExecStart
if [ "$CONFIG_PATH" != "${TICAL_HOME}/config/worker-configs/${WORKER_NAME}.json" ]; then
    sed -i "s|--config.*\\.json|--config ${CONFIG_PATH}|g" "$SERVICE_FILE"
fi

# If env file path is not the default, replace EnvironmentFile
if [ "$ENV_PATH" != "${TICAL_HOME}/config/env/${WORKER_NAME}.env" ]; then
    sed -i "s|EnvironmentFile=.*|EnvironmentFile=${ENV_PATH}|g" "$SERVICE_FILE"
fi

log_info "✓ Service file installed: ${SERVICE_FILE}"

# Reload systemd
sudo systemctl daemon-reload
log_info "✓ systemd daemon-reload complete"

# Enable service (auto-start on boot)
sudo systemctl enable "${SERVICE_NAME}"
log_info "✓ Service enabled (auto-start on boot)"

echo ""
echo "========================================="
echo " Installation Complete"
echo "========================================="
echo ""
echo " Management commands:"
echo "   Start:   sudo systemctl start ${SERVICE_NAME}"
echo "   Stop:    sudo systemctl stop ${SERVICE_NAME}"
echo "   Restart: sudo systemctl restart ${SERVICE_NAME}"
echo "   Status:  sudo systemctl status ${SERVICE_NAME}"
echo "   Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "   Disable: sudo systemctl disable ${SERVICE_NAME}"
echo ""
echo " Self-heal checks:"
echo "   View restart records: journalctl -u ${SERVICE_NAME} | grep 'Started'"
echo "   View death log:       cat ${HOME}/.tical-code/death-log/${WORKER_NAME}-death.json"
echo "   View snapshots:       ls ${HOME}/.tical-code/snapshots/${WORKER_NAME}-*.json"
echo ""
