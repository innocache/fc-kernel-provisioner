#!/bin/bash
# ============================================================================
# Remote Integration Test Runner
#
# Syncs code to a remote KVM host, sets up the environment, runs the full
# test suite (unit + smoke + integration), and tears down services afterward.
#
# Usage:
#   scripts/remote-test.sh user@host [options]
#   scripts/remote-test.sh fc-test [options]     (using SSH config alias)
#
# Options:
#   --skip-setup      Skip host setup and rootfs build (fast re-run)
#   --keep-services   Don't tear down services after tests (for debugging)
#   --unit-only       Only run unit tests on the remote host
#   --help            Show this help message
#
# Prerequisites:
#   - Key-based SSH access (password auth not supported)
#   - SSH key via ~/.ssh/config or default key files
#   - Remote user must have passwordless sudo
#
# Teardown behavior (after tests, unless --keep-services):
#   - Kills pool manager and Kernel Gateway processes
#   - Removes Unix socket (/var/run/fc-pool.sock)
#   - Kills leftover jailer processes and removes TAP devices
#   - Deletes jail directories under /srv/jailer/
#
# What persists between runs (not torn down):
#   - Firecracker binaries, Linux kernel (vmlinux)
#   - Rootfs image (/opt/firecracker/rootfs.ext4)
#   - Network bridge (fcbr0)
#   - System deps (apt packages), Python venv
#
# To fully remove host setup:
#   ssh user@host 'sudo ~/fc-kernel-provisioner/scripts/setup-host.sh teardown'
# ============================================================================
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
step()  { echo -e "${CYAN}[STEP]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*" >&2; }

# ── Argument parsing ─────────────────────────────────────────────────────────

usage() {
    sed -n '/^# Usage:/,/^# =====/p' "$0" | sed 's/^# \?//'
    exit 0
}

HOST=""
SKIP_SETUP=false
KEEP_SERVICES=false
UNIT_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-setup)    SKIP_SETUP=true; shift ;;
        --keep-services) KEEP_SERVICES=true; shift ;;
        --unit-only)     UNIT_ONLY=true; shift ;;
        --help|-h)       usage ;;
        -*)              fail "Unknown option: $1"; usage ;;
        *)
            if [[ -z "$HOST" ]]; then
                HOST="$1"
            else
                fail "Unexpected argument: $1"; usage
            fi
            shift ;;
    esac
done

if [[ -z "$HOST" ]]; then
    fail "Missing required argument: user@host"
    echo "Usage: $0 user@host [--skip-setup] [--keep-services] [--unit-only]"
    exit 1
fi

REMOTE_DIR="~/fc-kernel-provisioner"

# ── Verify SSH connectivity ──────────────────────────────────────────────────

step "Verifying SSH connectivity to $HOST..."
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "echo ok" &>/dev/null; then
    fail "Cannot connect to $HOST via SSH"
    echo "  Check your SSH config (~/.ssh/config) and key setup."
    echo "  Test with: ssh $HOST 'echo hello'"
    exit 1
fi
info "SSH connection verified ✓"

# ── Sync code ────────────────────────────────────────────────────────────────

step "Syncing project to $HOST:$REMOTE_DIR..."
rsync -az --delete \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '.git/' \
    --exclude '.worktrees/' \
    --exclude '.pytest_cache/' \
    --exclude '*.pyc' \
    "$PROJECT_DIR/" "$HOST:$REMOTE_DIR/"
info "Code synced ✓"

# ── Remote setup ─────────────────────────────────────────────────────────────

if [[ "$SKIP_SETUP" == "false" ]]; then
    step "Running host setup (setup-host.sh)..."
    ssh "$HOST" "cd $REMOTE_DIR && sudo ./scripts/setup-host.sh"

    step "Installing Python dependencies..."
    ssh "$HOST" "cd $REMOTE_DIR && uv sync --group dev"

    step "Building rootfs..."
    ssh "$HOST" "cd $REMOTE_DIR && sudo ./guest/build_rootfs.sh"

    step "Setting up network..."
    ssh "$HOST" "cd $REMOTE_DIR && sudo ./config/setup_network.sh"

    info "Remote setup complete ✓"
else
    info "Skipping setup (--skip-setup)"
fi

# ── Teardown function ────────────────────────────────────────────────────────

teardown() {
    if [[ "$KEEP_SERVICES" == "true" ]]; then
        warn "Skipping teardown (--keep-services)"
        return 0
    fi

    step "Tearing down services..."

    ssh "$HOST" "bash -s" <<'TEARDOWN_EOF'
set +e  # Don't exit on errors during teardown

# Kill pool manager
if pgrep -f "fc_pool_manager.server" >/dev/null 2>&1; then
    sudo pkill -f "fc_pool_manager.server" 2>/dev/null
    echo "  Killed pool manager"
fi

# Kill Kernel Gateway
if pgrep -f "jupyter-kernelgateway" >/dev/null 2>&1; then
    sudo pkill -f "jupyter-kernelgateway" 2>/dev/null
    echo "  Killed Kernel Gateway"
fi

# Remove socket
sudo rm -f /var/run/fc-pool.sock
echo "  Removed socket"

# Kill leftover jailer/firecracker processes
if pgrep -f "firecracker --id" >/dev/null 2>&1; then
    sudo pkill -f "firecracker --id" 2>/dev/null
    echo "  Killed leftover firecracker processes"
fi

# Remove TAP devices
for tap in $(ip link show 2>/dev/null | grep -oP 'tap-\w+'); do
    sudo ip link delete "$tap" 2>/dev/null
    echo "  Removed TAP device $tap"
done

# Remove jail directories
if [[ -d /srv/jailer/firecracker ]]; then
    sudo rm -rf /srv/jailer/firecracker/*/
    echo "  Cleaned jail directories"
fi

echo "  Teardown complete"
TEARDOWN_EOF
}

# ── Start services and run tests ─────────────────────────────────────────────

# Set up trap to preserve test exit code through teardown
TEST_RC=0
trap 'teardown; exit $TEST_RC' EXIT

if [[ "$UNIT_ONLY" == "true" ]]; then
    step "Running unit tests..."
    ssh "$HOST" "cd $REMOTE_DIR && uv run pytest tests/ -v -m 'not integration' --tb=short" || TEST_RC=$?

    if [[ $TEST_RC -eq 0 ]]; then
        info "Unit tests passed ✓"
    else
        fail "Unit tests failed (exit code $TEST_RC)"
    fi
    exit 0  # Triggers trap → teardown → exit $TEST_RC
fi

# Install kernelspec
step "Installing kernelspec..."
ssh "$HOST" "cd $REMOTE_DIR && sudo uv run jupyter kernelspec install config/kernelspec/ --name python3-firecracker --sys-prefix"

# Start pool manager in background
step "Starting pool manager..."
ssh "$HOST" "cd $REMOTE_DIR && sudo uv run python -m fc_pool_manager.server --config config/fc-pool.yaml --socket /var/run/fc-pool.sock -v &>/tmp/fc-pool-manager.log &"

# Start Kernel Gateway in background
step "Starting Kernel Gateway..."
ssh "$HOST" "cd $REMOTE_DIR && uv run jupyter kernelgateway --KernelGatewayApp.default_kernel_name=python3-firecracker --KernelGatewayApp.port=8888 &>/tmp/fc-kernel-gateway.log &"

# Poll until services are ready (timeout 120s)
step "Waiting for services to be ready (timeout 120s)..."
TIMEOUT=120
ELAPSED=0
POOL_READY=false
GW_READY=false

while [[ $ELAPSED -lt $TIMEOUT ]]; do
    if [[ "$POOL_READY" == "false" ]]; then
        if ssh "$HOST" "sudo curl -sf --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status" &>/dev/null; then
            POOL_READY=true
            info "Pool manager is ready ✓"
        fi
    fi

    if [[ "$GW_READY" == "false" ]]; then
        if ssh "$HOST" "curl -sf http://localhost:8888/api/kernels" &>/dev/null; then
            GW_READY=true
            info "Kernel Gateway is ready ✓"
        fi
    fi

    if [[ "$POOL_READY" == "true" && "$GW_READY" == "true" ]]; then
        break
    fi

    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

if [[ "$POOL_READY" != "true" || "$GW_READY" != "true" ]]; then
    fail "Services did not become ready within ${TIMEOUT}s"
    [[ "$POOL_READY" != "true" ]] && fail "Pool manager not ready — check /tmp/fc-pool-manager.log on remote host"
    [[ "$GW_READY" != "true" ]] && fail "Kernel Gateway not ready — check /tmp/fc-kernel-gateway.log on remote host"
    TEST_RC=1
    exit 1  # Triggers trap → teardown → exit $TEST_RC
fi

# ── Run tests ────────────────────────────────────────────────────────────────

step "Running unit tests..."
ssh "$HOST" "cd $REMOTE_DIR && uv run pytest tests/ -v -m 'not integration' --tb=short" || TEST_RC=$?

if [[ $TEST_RC -ne 0 ]]; then
    fail "Unit tests failed (exit code $TEST_RC)"
    exit 1  # Triggers trap → teardown → exit $TEST_RC
fi
info "Unit tests passed ✓"

step "Running smoke test..."
ssh "$HOST" "cd $REMOTE_DIR && ./scripts/run-tests.sh smoke" || TEST_RC=$?

if [[ $TEST_RC -ne 0 ]]; then
    fail "Smoke test failed (exit code $TEST_RC)"
    exit 1
fi
info "Smoke test passed ✓"

step "Running integration tests..."
ssh "$HOST" "cd $REMOTE_DIR && uv run pytest tests/test_integration.py -v -m integration --tb=long -s" || TEST_RC=$?

if [[ $TEST_RC -ne 0 ]]; then
    fail "Integration tests failed (exit code $TEST_RC)"
else
    info "Integration tests passed ✓"
fi

exit 0  # Triggers trap → teardown → exit $TEST_RC
