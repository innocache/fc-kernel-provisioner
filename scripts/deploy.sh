#!/bin/bash
# ============================================================================
# Production Deployment Manager
#
# Deploys and manages the pool manager and Kernel Gateway as systemd services
# on a remote Linux host.
#
# Usage:
#   scripts/deploy.sh user@host <command>
#
# Commands:
#   deploy    Full setup: sync code, install deps, build rootfs, configure
#             network, install + start systemd services
#   update    Fast update: sync code, rebuild rootfs if guest/ changed,
#             restart services
#   stop      Stop systemd services
#   start     Start systemd services
#   restart   Restart systemd services
#   status    Show service status, pool stats, host resources
#   logs      Tail service logs (journalctl -f)
#   teardown  Remove everything and restore host to pre-deployment state
#
# Prerequisites:
#   - Key-based SSH access (password auth not supported)
#   - SSH key via ~/.ssh/config or default key files
#   - Remote user must have passwordless sudo
#   - Remote host: Ubuntu 24.04, 8+ cores, 16+ GB RAM, KVM enabled
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
COMMAND=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) usage ;;
        -*)        fail "Unknown option: $1"; usage ;;
        *)
            if [[ -z "$HOST" ]]; then
                HOST="$1"
            elif [[ -z "$COMMAND" ]]; then
                COMMAND="$1"
            else
                fail "Unexpected argument: $1"; usage
            fi
            shift ;;
    esac
done

if [[ -z "$HOST" || -z "$COMMAND" ]]; then
    fail "Missing required arguments"
    echo "Usage: $0 user@host <command>"
    echo "Commands: deploy | update | stop | start | restart | status | logs | teardown"
    exit 1
fi

REMOTE_DIR="~/fc-kernel-provisioner"
OPT_DIR="/opt/fc-kernel-provisioner"
SERVICES="fc-pool-manager fc-kernel-gateway"

# ── Helpers ──────────────────────────────────────────────────────────────────

verify_ssh() {
    step "Verifying SSH connectivity to $HOST..."
    if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "echo ok" &>/dev/null; then
        fail "Cannot connect to $HOST via SSH"
        echo "  Check your SSH config (~/.ssh/config) and key setup."
        exit 1
    fi
    info "SSH connection verified ✓"
}

sync_code() {
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
}

wait_for_services() {
    step "Waiting for services to be ready (timeout 120s)..."
    local timeout=120 elapsed=0 pool_ready=false gw_ready=false

    while [[ $elapsed -lt $timeout ]]; do
        if [[ "$pool_ready" == "false" ]]; then
            if ssh "$HOST" "sudo curl -sf --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status" &>/dev/null; then
                pool_ready=true
                info "Pool manager is ready ✓"
            fi
        fi

        if [[ "$gw_ready" == "false" ]]; then
            if ssh "$HOST" "curl -sf http://localhost:8888/api/kernels" &>/dev/null; then
                gw_ready=true
                info "Kernel Gateway is ready ✓"
            fi
        fi

        if [[ "$pool_ready" == "true" && "$gw_ready" == "true" ]]; then
            return 0
        fi

        sleep 2
        elapsed=$((elapsed + 2))
    done

    fail "Services did not become ready within ${timeout}s"
    [[ "$pool_ready" != "true" ]] && fail "Pool manager not ready"
    [[ "$gw_ready" != "true" ]] && fail "Kernel Gateway not ready"
    echo "  Check logs: $0 $HOST logs"
    return 1
}

# ── Commands ─────────────────────────────────────────────────────────────────

cmd_deploy() {
    verify_ssh
    sync_code

    step "Creating symlink $OPT_DIR → $REMOTE_DIR..."
    ssh "$HOST" "sudo ln -sfn \$(eval echo $REMOTE_DIR) $OPT_DIR"

    step "Running host setup..."
    ssh "$HOST" "cd $REMOTE_DIR && sudo ./scripts/setup-host.sh"

    step "Installing Python dependencies..."
    ssh "$HOST" "cd $REMOTE_DIR && uv sync --group dev"

    step "Building rootfs..."
    ssh "$HOST" "cd $REMOTE_DIR && sudo ./guest/build_rootfs.sh"

    step "Recording guest checksum..."
    ssh "$HOST" "cd $REMOTE_DIR && find guest/ -type f | sort | xargs sha256sum > .guest-checksum"

    step "Setting up network..."
    ssh "$HOST" "cd $REMOTE_DIR && sudo ./config/setup_network.sh"

    step "Installing systemd units..."
    ssh "$HOST" "bash -s" <<INSTALL_EOF
sudo cp $OPT_DIR/config/fc-pool-manager.service /etc/systemd/system/
sudo cp $OPT_DIR/config/fc-kernel-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
INSTALL_EOF

    step "Enabling and starting services..."
    ssh "$HOST" "sudo systemctl enable --now $SERVICES"

    wait_for_services
    info "Deployment complete ✓"
}

cmd_update() {
    verify_ssh
    sync_code

    # Check if rootfs needs rebuild
    step "Checking if rootfs rebuild is needed..."
    NEEDS_REBUILD=$(ssh "$HOST" "bash -s" <<'CHECK_EOF'
cd ~/fc-kernel-provisioner
if [[ ! -f .guest-checksum ]]; then
    echo "yes"
    exit 0
fi
CURRENT=$(find guest/ -type f | sort | xargs sha256sum)
SAVED=$(cat .guest-checksum)
if [[ "$CURRENT" != "$SAVED" ]]; then
    echo "yes"
else
    echo "no"
fi
CHECK_EOF
)

    if [[ "$NEEDS_REBUILD" == "yes" ]]; then
        step "Guest files changed — rebuilding rootfs..."
        ssh "$HOST" "cd $REMOTE_DIR && sudo ./guest/build_rootfs.sh"
        ssh "$HOST" "cd $REMOTE_DIR && find guest/ -type f | sort | xargs sha256sum > .guest-checksum"
        info "Rootfs rebuilt ✓"
    else
        info "Guest files unchanged — skipping rootfs rebuild"
    fi

    step "Restarting services..."
    ssh "$HOST" "sudo systemctl restart $SERVICES"

    wait_for_services
    info "Update complete ✓"
}

cmd_stop() {
    verify_ssh
    step "Stopping services..."
    ssh "$HOST" "sudo systemctl stop $SERVICES"
    info "Services stopped ✓"
}

cmd_start() {
    verify_ssh
    step "Starting services..."
    ssh "$HOST" "sudo systemctl start $SERVICES"
    wait_for_services
    info "Services started ✓"
}

cmd_restart() {
    verify_ssh
    step "Restarting services..."
    ssh "$HOST" "sudo systemctl restart $SERVICES"
    wait_for_services
    info "Services restarted ✓"
}

cmd_status() {
    verify_ssh

    echo ""
    step "Service status:"
    ssh "$HOST" "sudo systemctl status $SERVICES --no-pager" || true

    echo ""
    step "Pool status:"
    ssh "$HOST" "sudo curl -sf --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status 2>/dev/null | python3 -m json.tool" || warn "Pool manager not responding"

    echo ""
    step "Host resources:"
    ssh "$HOST" "echo '  CPUs: '\$(nproc); free -h | head -2; echo '  Active VMs: '\$(ls /srv/jailer/firecracker/ 2>/dev/null | wc -l)"
}

cmd_logs() {
    verify_ssh
    step "Tailing service logs (Ctrl-C to stop)..."
    ssh "$HOST" "sudo journalctl -u fc-pool-manager -u fc-kernel-gateway -f --no-pager"
}

cmd_teardown() {
    verify_ssh

    echo ""
    warn "This will remove ALL deployed services, data, and host setup from $HOST."
    echo ""
    read -rp "Type 'yes' to confirm teardown: " CONFIRM
    if [[ "$CONFIRM" != "yes" ]]; then
        info "Teardown cancelled"
        exit 0
    fi

    step "Stopping and disabling services..."
    ssh "$HOST" "sudo systemctl stop $SERVICES 2>/dev/null || true"
    ssh "$HOST" "sudo systemctl disable $SERVICES 2>/dev/null || true"

    step "Removing systemd unit files..."
    ssh "$HOST" "bash -s" <<'TEARDOWN_UNITS_EOF'
sudo rm -f /etc/systemd/system/fc-pool-manager.service
sudo rm -f /etc/systemd/system/fc-kernel-gateway.service
sudo systemctl daemon-reload
TEARDOWN_UNITS_EOF

    step "Cleaning up VMs..."
    ssh "$HOST" "bash -s" <<'TEARDOWN_VMS_EOF'
set +e
sudo pkill -f "firecracker --id" 2>/dev/null
for tap in $(ip link show 2>/dev/null | grep -oP 'tap-\w+'); do
    sudo ip link delete "$tap" 2>/dev/null
done
sudo rm -rf /srv/jailer/firecracker/*/
sudo rm -f /var/run/fc-pool.sock
TEARDOWN_VMS_EOF

    step "Tearing down network..."
    ssh "$HOST" "cd $REMOTE_DIR && sudo ./config/setup_network.sh teardown" || true

    step "Removing rootfs..."
    ssh "$HOST" "cd $REMOTE_DIR && sudo ./guest/build_rootfs.sh --clean" || true

    step "Running host teardown..."
    ssh "$HOST" "cd $REMOTE_DIR && sudo ./scripts/setup-host.sh teardown" || true

    step "Removing symlink and deployed code..."
    ssh "$HOST" "sudo rm -f $OPT_DIR"
    ssh "$HOST" "rm -rf $REMOTE_DIR"

    info "Teardown complete — host restored ✓"
}

# ── Dispatch ─────────────────────────────────────────────────────────────────

case "$COMMAND" in
    deploy)   cmd_deploy ;;
    update)   cmd_update ;;
    stop)     cmd_stop ;;
    start)    cmd_start ;;
    restart)  cmd_restart ;;
    status)   cmd_status ;;
    logs)     cmd_logs ;;
    teardown) cmd_teardown ;;
    *)
        fail "Unknown command: $COMMAND"
        echo "Commands: deploy | update | stop | start | restart | status | logs | teardown"
        exit 1 ;;
esac
