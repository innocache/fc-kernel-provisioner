# Remote Integration Testing & Production Deployment — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [x]`) syntax for tracking.

**Status:** All tasks complete. Implemented in PR #26 (merged).

| Chunk | Status | PR |
|-------|--------|----|
| 1: Config File Restructuring (Tasks 1-3) | DONE | #26 |
| 2: Remote Integration Test Script (Task 4) | DONE | #26 |
| 3: Production Deployment Script (Task 5) | DONE | #26 |
| 4: Documentation (Tasks 6-7) | DONE | #26 |
| 5: Final Verification (Task 8) | DONE | #26 |

**Goal:** Two independent shell scripts — `remote-test.sh` for one-command remote integration testing on a KVM host, and `deploy.sh` for production deployment lifecycle management via systemd.

**Architecture:** Both scripts use SSH + rsync to sync code to a remote Linux host and call existing setup scripts (`setup-host.sh`, `build_rootfs.sh`, `setup_network.sh`). The test script runs services in the foreground and tears them down afterward. The deploy script installs systemd units for persistent service management. A new `fc-kernel-gateway.service` systemd unit is created, the existing `fc-pool-manager.service` is updated, and `kernel.json` is moved to a dedicated `config/kernelspec/` directory.

**Tech Stack:** Bash, SSH, rsync, systemd, curl

**Spec:** `docs/superpowers/specs/2026-03-22-remote-testing-and-deployment-design.md`

---

## Chunk 1: Config File Restructuring

These tasks restructure existing config files as prerequisites for the scripts.

### Task 1: Move kernel.json to config/kernelspec/

Move `config/kernel.json` into a dedicated `config/kernelspec/` subdirectory so `jupyter kernelspec install` only copies the kernelspec file, not unrelated config files (YAML, systemd units, shell scripts).

**Files:**
- Create: `config/kernelspec/kernel.json` (moved from `config/kernel.json`)
- Delete: `config/kernel.json`

- [x] **Step 1: Create the kernelspec directory and move the file**

```bash
mkdir -p config/kernelspec
git mv config/kernel.json config/kernelspec/kernel.json
```

- [x] **Step 2: Update README.md references**

In `README.md`, change the kernelspec install command from:
```bash
uv run jupyter kernelspec install config/ --name python3-firecracker --user
```
to:
```bash
uv run jupyter kernelspec install config/kernelspec/ --name python3-firecracker --user
```

Also update the Architecture tree — change `config/kernel.json` to `config/kernelspec/kernel.json` (with the `kernelspec/` subdirectory).

- [x] **Step 3: Update docs/testing.md references**

In `docs/testing.md`, change:
```bash
uv run jupyter kernelspec install config/ \
    --name python3-firecracker \
    --user
```
to:
```bash
uv run jupyter kernelspec install config/kernelspec/ \
    --name python3-firecracker \
    --user
```

- [x] **Step 4: Verify no other files reference the old path**

```bash
grep -r "kernelspec install config/" --include="*.sh" --include="*.md" --include="*.py" --include="*.service" .
```

Should return zero matches for `config/` (all should now reference `config/kernelspec/`).

- [x] **Step 5: Commit**

```bash
git add config/kernelspec/kernel.json README.md docs/testing.md
git commit -m "refactor: move kernel.json to config/kernelspec/ subdirectory

Isolates the kernelspec file so 'jupyter kernelspec install' only copies
kernel.json, not unrelated config files (YAML, systemd units, scripts)."
```

---

### Task 2: Update fc-pool-manager.service to use uv

Update the existing systemd unit to use `uv run` instead of bare `/usr/bin/python3`, and add `WorkingDirectory` so `uv` can find the project's `.venv`.

**Files:**
- Modify: `config/fc-pool-manager.service`

- [x] **Step 1: Update ExecStart and add WorkingDirectory**

Replace the current content of `config/fc-pool-manager.service` with:

```ini
[Unit]
Description=Firecracker VM Pool Manager
After=network.target

[Service]
Type=simple
ExecStartPre=/bin/bash /opt/fc-kernel-provisioner/config/setup_network.sh
ExecStart=/usr/bin/env uv run python -m fc_pool_manager.server --config /opt/fc-kernel-provisioner/config/fc-pool.yaml --socket /var/run/fc-pool.sock
Restart=on-failure
RestartSec=5
# Root required: jailer needs CAP_SYS_ADMIN for cgroup setup and /dev/kvm access
User=root
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/opt/fc-kernel-provisioner

[Install]
WantedBy=multi-user.target
```

Changes from original:
- `ExecStart`: `/usr/bin/python3 -m` → `/usr/bin/env uv run python -m` (uses project venv)
- `ExecStart`: Added `--socket /var/run/fc-pool.sock` (explicit socket path)
- Added: `WorkingDirectory=/opt/fc-kernel-provisioner` (so `uv` finds `.venv`)

- [x] **Step 2: Commit**

```bash
git add config/fc-pool-manager.service
git commit -m "fix: update fc-pool-manager.service to use uv run

Switches from bare /usr/bin/python3 to 'uv run python' so the service
uses the project venv. Adds WorkingDirectory and explicit --socket flag."
```

---

### Task 3: Create fc-kernel-gateway.service

Create a new systemd unit for the Jupyter Kernel Gateway, which depends on the pool manager.

**Files:**
- Create: `config/fc-kernel-gateway.service`

- [x] **Step 1: Create the service file**

Create `config/fc-kernel-gateway.service`:

```ini
[Unit]
Description=Jupyter Kernel Gateway (Firecracker)
After=fc-pool-manager.service
Requires=fc-pool-manager.service

[Service]
Type=simple
ExecStartPre=/usr/bin/env uv run jupyter kernelspec install /opt/fc-kernel-provisioner/config/kernelspec/ --name python3-firecracker --sys-prefix
ExecStart=/usr/bin/env uv run jupyter kernelgateway --KernelGatewayApp.default_kernel_name=python3-firecracker --KernelGatewayApp.port=8888
Restart=on-failure
RestartSec=5
# Root required: pool manager socket is root-owned, and Kernel Gateway
# needs to start kernels via the provisioner which talks to the pool socket
User=root
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/opt/fc-kernel-provisioner

[Install]
WantedBy=multi-user.target
```

Key points:
- `Requires=fc-pool-manager.service` — systemd ensures pool manager starts first
- `ExecStartPre` installs kernelspec with `--sys-prefix` (into active Python env, not root's home)
- `WorkingDirectory` matches pool manager — both use the symlink at `/opt/fc-kernel-provisioner`

- [x] **Step 2: Commit**

```bash
git add config/fc-kernel-gateway.service
git commit -m "feat: add Kernel Gateway systemd unit

Depends on fc-pool-manager.service. Installs kernelspec on start via
ExecStartPre, runs gateway on port 8888."
```

---

## Chunk 2: Remote Integration Test Script

### Task 4: Create scripts/remote-test.sh

One-command remote integration test runner: syncs code, sets up environment, runs tests, tears down services.

**Files:**
- Create: `scripts/remote-test.sh`

- [x] **Step 1: Create the script with argument parsing and help text**

Create `scripts/remote-test.sh`:

```bash
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
```

- [x] **Step 2: Make the script executable**

```bash
chmod +x scripts/remote-test.sh
```

- [x] **Step 3: Run shellcheck**

```bash
shellcheck scripts/remote-test.sh
```

Fix any issues found.

- [x] **Step 4: Commit**

```bash
git add scripts/remote-test.sh
git commit -m "feat: add remote integration test script

One-command remote test runner: syncs code via rsync, sets up host
environment, starts services, runs unit + smoke + integration tests,
and tears down afterward. Supports --skip-setup, --keep-services,
and --unit-only flags."
```

---

## Chunk 3: Production Deployment Script

### Task 5: Create scripts/deploy.sh

Production lifecycle manager with deploy, update, stop, start, restart, status, logs, and teardown subcommands.

**Files:**
- Create: `scripts/deploy.sh`

- [x] **Step 1: Create the script with argument parsing and common functions**

Create `scripts/deploy.sh`:

```bash
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
```

- [x] **Step 2: Make the script executable**

```bash
chmod +x scripts/deploy.sh
```

- [x] **Step 3: Run shellcheck**

```bash
shellcheck scripts/deploy.sh
```

Fix any issues found.

- [x] **Step 4: Commit**

```bash
git add scripts/deploy.sh
git commit -m "feat: add production deployment script

Lifecycle manager for remote hosts with subcommands: deploy, update,
stop, start, restart, status, logs, teardown. Uses systemd for service
management, rsync for code sync, and .guest-checksum for rootfs
rebuild detection."
```

---

## Chunk 4: Documentation

### Task 6: Update docs/testing.md

Add sections for remote integration testing and production deployment.

**Files:**
- Modify: `docs/testing.md`

- [x] **Step 1: Add Remote Integration Testing section**

After the "CI Considerations" section (end of the file), add:

```markdown
---

## Remote Integration Testing

Run the full test suite on a remote KVM host from your local machine with a single command.

### Prerequisites

- **SSH access**: Key-based SSH to the remote host (password auth not supported)
- **SSH config**: Configure your key via `~/.ssh/config` or use default key files
- **Remote user**: Must have passwordless `sudo`
- **Remote host**: Ubuntu 24.04, 8+ cores, 16+ GB RAM, KVM enabled (`/dev/kvm`)

Example `~/.ssh/config`:

```
Host fc-test
    HostName 203.0.113.10
    User ubuntu
    IdentityFile ~/.ssh/my-key.pem
```

### Usage

```bash
# Full run: setup + unit + smoke + integration + teardown
./scripts/remote-test.sh fc-test

# Fast re-run after code changes (skip host setup)
./scripts/remote-test.sh fc-test --skip-setup

# Unit tests only (no services needed)
./scripts/remote-test.sh fc-test --unit-only

# Keep services running after tests (for debugging)
./scripts/remote-test.sh fc-test --keep-services
```

### What happens

1. **Sync** — rsync project to `~/fc-kernel-provisioner` on remote host
2. **Setup** — install Firecracker, Python deps, rootfs, network bridge (skipped with `--skip-setup`)
3. **Start services** — pool manager + Kernel Gateway + Execution API in background, poll until ready (120s timeout)
4. **Run tests** — unit → smoke → integration (or just unit with `--unit-only`)
5. **Teardown** — kill services, remove socket, clean up leftover VMs

### What persists between runs

| Persists | Torn down |
|----------|-----------|
| Firecracker binaries | Pool manager process |
| Linux kernel (`vmlinux`) | Kernel Gateway process |
| Rootfs image | Unix socket file |
| Network bridge (`fcbr0`) | Running VMs / jailer processes |
| System deps (apt packages) | TAP devices |
| Python venv | Jail directories |

### Troubleshooting

**SSH connection fails:**
```bash
# Test connectivity
ssh fc-test 'echo hello'

# Check key permissions
ls -la ~/.ssh/my-key.pem  # should be 600

# Verbose SSH for debugging
ssh -vv fc-test 'echo hello'
```

**Services don't start within 120s:**
- Check remote logs: `ssh fc-test 'cat /tmp/fc-pool-manager.log'`
- Check KVM: `ssh fc-test 'ls -la /dev/kvm'`
- First run takes longer due to pool pre-warming

**Tests fail but services are fine:**
- Re-run with `--keep-services` to debug interactively
- SSH in and run tests manually: `ssh fc-test 'cd ~/fc-kernel-provisioner && uv run pytest tests/test_integration.py -v -m integration -s'`

---

## Production Deployment

Deploy the pool manager and Kernel Gateway as systemd services on a remote host.

### Prerequisites

Same SSH and host requirements as remote testing (see above).

### Usage

```bash
# Full deployment (idempotent — safe to re-run)
./scripts/deploy.sh fc-prod deploy

# Fast update after code changes
./scripts/deploy.sh fc-prod update

# Service management
./scripts/deploy.sh fc-prod stop
./scripts/deploy.sh fc-prod start
./scripts/deploy.sh fc-prod restart
./scripts/deploy.sh fc-prod status
./scripts/deploy.sh fc-prod logs

# Remove everything (prompts for confirmation)
./scripts/deploy.sh fc-prod teardown
```

### deploy vs update

| | `deploy` | `update` |
|---|---------|----------|
| Sync code | ✓ | ✓ |
| Host setup (Firecracker, deps) | ✓ | — |
| Build rootfs | Always | Only if `guest/` changed |
| Network setup | ✓ | — |
| Install systemd units | ✓ | — |
| Restart services | ✓ | ✓ |

Use `deploy` for first-time setup or after infrastructure changes. Use `update` for code-only changes (faster).

### Systemd services

Two services are installed:

- **`fc-pool-manager`** — VM pool manager daemon (Unix socket API at `/var/run/fc-pool.sock`)
- **`fc-kernel-gateway`** — Jupyter Kernel Gateway on port 8888 (depends on pool manager)

Both run as `root` (required for KVM/jailer access) and auto-restart on failure.

```bash
# Check service status
sudo systemctl status fc-pool-manager fc-kernel-gateway

# View logs
sudo journalctl -u fc-pool-manager -u fc-kernel-gateway -f
```

### Teardown

`teardown` removes everything and restores the host:

1. Stop and disable systemd services
2. Remove systemd unit files
3. Kill leftover VMs, remove TAP devices, clean jail directories
4. Remove network bridge and NAT rules
5. Remove rootfs image
6. Remove Firecracker binaries, kernel, jailer user
7. Remove deployed code and `/opt/fc-kernel-provisioner` symlink

**Confirmation required** — you must type `yes` to proceed.
```

- [x] **Step 2: Verify the markdown renders correctly**

Review the file to ensure no formatting issues with nested code blocks.

- [x] **Step 3: Commit**

```bash
git add docs/testing.md
git commit -m "docs: add remote testing and deployment sections to testing.md

Documents remote-test.sh usage, flags, and troubleshooting.
Documents deploy.sh commands, deploy vs update comparison,
systemd services, and teardown behavior."
```

---

### Task 7: Update README.md

Add brief mentions of remote testing and deployment in the README.

**Files:**
- Modify: `README.md`

- [x] **Step 1: Add remote testing and deployment to the Testing section**

After the existing test commands in the Testing section, add:

```markdown
### Remote Testing & Deployment

```bash
# Run full test suite on a remote KVM host
./scripts/remote-test.sh user@host

# Deploy as systemd services
./scripts/deploy.sh user@host deploy
```

See [docs/testing.md](docs/testing.md) for full details.
```

- [x] **Step 2: Update the Architecture tree**

In the `scripts/` section of the Architecture tree, add the new scripts:

```
├── scripts/
│   ├── setup-host.sh        # Host setup (with teardown + status modes)
│   ├── run-tests.sh         # Test runner (unit/smoke/integration)
│   ├── remote-test.sh       # Remote integration test runner
│   └── deploy.sh            # Production deployment manager
```

In the `config/` section, update to show the kernelspec directory and new service:

```
├── config/
│   ├── fc-pool-manager.service    # systemd unit (pool manager)
│   ├── fc-kernel-gateway.service  # systemd unit (Kernel Gateway)
│   ├── fc-pool.yaml               # Pool manager configuration
│   ├── kernelspec/
│   │   └── kernel.json            # Jupyter kernelspec
│   └── setup_network.sh           # Host bridge + NAT setup
```

- [x] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add remote testing and deployment to README"
```

---

## Chunk 5: Final Verification

### Task 8: End-to-end verification

Verify all files are in place, scripts are executable, and no broken references.

- [x] **Step 1: Verify file structure**

```bash
ls -la scripts/remote-test.sh scripts/deploy.sh
ls -la config/fc-kernel-gateway.service config/fc-pool-manager.service
ls -la config/kernelspec/kernel.json
```

All scripts should be executable (`-rwxr-xr-x`). The old `config/kernel.json` should not exist.

- [x] **Step 2: Verify no broken references to old kernel.json path**

```bash
grep -r "kernelspec install config/" --include="*.sh" --include="*.md" --include="*.py" --include="*.service" .
grep -r "config/kernel\.json" --include="*.sh" --include="*.md" --include="*.py" .
```

Both should return no matches (or only reference the new `config/kernelspec/` path).

- [x] **Step 3: Run shellcheck on both scripts**

```bash
shellcheck scripts/remote-test.sh scripts/deploy.sh
```

Should pass with no errors.

- [x] **Step 4: Run unit tests to verify nothing is broken**

```bash
uv run pytest tests/ -v -m "not integration" --tb=short
```

All tests should pass (the config restructuring doesn't affect unit tests).

- [x] **Step 5: Final commit if any fixes were needed**

Only if previous steps required fixes:

```bash
git add -A
git commit -m "fix: address verification issues in remote testing scripts"
```
