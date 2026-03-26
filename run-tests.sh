#!/usr/bin/env bash
#
# NASty Integration Test Runner
#
# Runs the test suite inside the Colima Linux VM so that NFS/SMB/iSCSI/NVMe-oF
# client operations work from macOS. The VM connects to the NASty appliance
# over the network, exercising the full stack including networking.
#
# Prerequisites:
#   - Colima installed and running: colima start
#
# Usage:
#   ./tests/run-tests.sh --host <nasty-ip> [options]
#   ./tests/run-tests.sh --host 10.10.10.50
#   ./tests/run-tests.sh --host 10.10.10.50 --pool tank --skip-nvmeof
#
# All arguments after the script name are forwarded to run_tests.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Colours ─────────────────────────────────────────────────────
GREEN="\033[92m"
RED="\033[91m"
CYAN="\033[96m"
YELLOW="\033[93m"
RESET="\033[0m"

info()  { echo -e "${CYAN}→${RESET} $1"; }
ok()    { echo -e "  ${GREEN}✓${RESET} $1"; }
fail()  { echo -e "  ${RED}✗${RESET} $1"; }
warn()  { echo -e "  ${YELLOW}!${RESET} $1"; }

# ── Preflight checks ────────────────────────────────────────────

if ! command -v colima &>/dev/null; then
    fail "Colima not found. Install with: brew install colima"
    exit 1
fi

if ! colima status &>/dev/null; then
    fail "Colima is not running. Start with: colima start"
    exit 1
fi

if [[ ! -f "$SCRIPT_DIR/run_tests.py" ]]; then
    fail "run_tests.py not found in $SCRIPT_DIR"
    exit 1
fi

# Require --host
if [[ "$*" != *"--host"* ]]; then
    echo -e "${RED}Usage:${RESET} $0 --host <nasty-ip> [options]"
    echo ""
    echo "Options (forwarded to run_tests.py):"
    echo "  --host HOST        NASty appliance IP (required)"
    echo "  --port PORT        WebUI HTTPS port (default 443)"
    echo "  --password PW      Admin password (default 'admin')"
    echo "  --pool POOL        Pool name (auto-detected if omitted)"
  echo "  --create-pool      Auto-create pool on available devices if it doesn't exist"
    echo "  --skip-subvolume   Skip subvolume lifecycle/properties/block tests"
    echo "  --skip-snapshots   Skip snapshot read_only and clone tests"
    echo "  --skip-storage     Skip compression and snapshot integrity tests"
    echo "  --skip-nfs         Skip NFS tests"
    echo "  --skip-smb         Skip SMB tests"
    echo "  --skip-iscsi       Skip iSCSI tests"
    echo "  --skip-nvmeof      Skip NVMe-oF tests"
    echo "  --skip-delete      Leave subvolumes/shares behind after tests"
    echo "  --delete-only      Delete test-* leftovers from a prior --skip-delete run"
    echo "  --tag TAG          Reuse tag from a prior --skip-delete run"
    echo "  --remount          Skip creation, mount existing shares and verify (use with --tag)"
    exit 1
fi

# ── Provision VM ─────────────────────────────────────────────────

MARKER="/tmp/.nasty-test-provisioned"

info "Checking Colima VM provisioning..."

if ! colima ssh -- test -f "$MARKER" 2>/dev/null; then
    info "Installing test dependencies in Colima VM (one-time setup)..."

    colima ssh -- sudo bash -c '
        set -e
        export DEBIAN_FRONTEND=noninteractive

        apt-get update -qq

        # NFS client
        apt-get install -y -qq nfs-common 2>/dev/null

        # SMB/CIFS client
        apt-get install -y -qq cifs-utils 2>/dev/null

        # iSCSI initiator
        apt-get install -y -qq open-iscsi 2>/dev/null

        # NVMe-oF client
        apt-get install -y -qq nvme-cli 2>/dev/null

        # Extra kernel modules (nvme-tcp lives in linux-modules-extra on Ubuntu)
        KVER=$(dpkg -l | grep -oP "linux-modules-\K[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-generic" | head -1)
        if [ -n "$KVER" ]; then
            apt-get install -y -qq "linux-modules-extra-${KVER}" 2>/dev/null || true
        fi

        # Python + websockets
        apt-get install -y -qq python3 python3-pip python3-venv 2>/dev/null

        # Load kernel modules needed for NVMe-oF client
        modprobe nvme-tcp 2>/dev/null || true
        modprobe nvme-fabrics 2>/dev/null || true

        # Start iscsid if installed
        systemctl start iscsid 2>/dev/null || true
    '

    # Create a venv with websockets inside the VM
    colima ssh -- bash -c '
        python3 -m venv /tmp/nasty-test-venv
        /tmp/nasty-test-venv/bin/pip install -q websockets
    '

    colima ssh -- touch "$MARKER"
    ok "VM provisioned"
else
    ok "VM already provisioned"

    # Modules don't persist across VM restarts — ensure they're loaded
    colima ssh -- sudo bash -c '
        modprobe nvme-tcp 2>/dev/null || true
        modprobe nvme-fabrics 2>/dev/null || true
        systemctl start iscsid 2>/dev/null || true
    '
fi

# ── Copy test suite & run ────────────────────────────────────────

info "Copying test suite to VM..."
tar -C "$SCRIPT_DIR" -czf - \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='run-tests.sh' \
    . | colima ssh -- sudo bash -c '
        rm -rf /tmp/nasty-tests
        mkdir /tmp/nasty-tests
        tar -C /tmp/nasty-tests -xzf -
    '
ok "Test suite copied"

LOG="$SCRIPT_DIR/last-run.log"

info "Running tests inside Colima VM..."
echo ""

# The VM needs root for mount/iscsi/nvme operations
colima ssh -- sudo bash -c \
    "cd /tmp/nasty-tests && /tmp/nasty-test-venv/bin/python3 run_tests.py $*" \
    | tee "$LOG"
EXIT_CODE=${PIPESTATUS[0]}

# Strip ANSI escape codes and show failures summary
FAILURES=$(sed 's/\x1b\[[0-9;]*m//g' "$LOG" | grep '^\s*\[FAIL\]' || true)
if [[ -n "$FAILURES" ]]; then
    echo ""
    echo -e "${RED}══ Failures ══${RESET}"
    echo "$FAILURES"
fi

echo ""
info "Full log saved to: $LOG"

exit $EXIT_CODE
