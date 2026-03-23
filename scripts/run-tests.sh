#!/bin/bash
# ============================================================================
# Test Runner — runs the appropriate test level based on environment
#
# Usage:
#   ./scripts/run-tests.sh              # auto-detect: unit or full
#   ./scripts/run-tests.sh unit         # unit tests only (no KVM needed)
#   ./scripts/run-tests.sh integration  # integration tests (requires running services)
#   ./scripts/run-tests.sh smoke        # manual smoke test with step-by-step output
#   ./scripts/run-tests.sh all          # everything
# ============================================================================
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
step()  { echo -e "${CYAN}[STEP]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*" >&2; }

LEVEL="${1:-auto}"

# ── Helpers ──────────────────────────────────────────────────────────────────

has_kvm()       { [[ -e /dev/kvm ]]; }
has_firecracker() { command -v firecracker &>/dev/null; }
has_rootfs()    { [[ -f /opt/firecracker/rootfs.ext4 ]]; }
has_bridge()    { ip link show fcbr0 &>/dev/null 2>&1; }

pool_running() {
    [[ -S /var/run/fc-pool.sock ]] && \
    curl -s --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status &>/dev/null
}

gateway_running() {
    curl -sf http://localhost:8888/api &>/dev/null 2>&1
}

# ── Auto-detect ──────────────────────────────────────────────────────────────

if [[ "$LEVEL" == "auto" ]]; then
    if has_kvm && has_firecracker && has_rootfs && has_bridge; then
        if pool_running && gateway_running; then
            LEVEL="all"
            info "All services detected — running full test suite"
        else
            LEVEL="unit"
            info "KVM available but services not running — running unit tests only"
            echo "  To run integration tests, start the pool manager and Kernel Gateway first."
        fi
    else
        LEVEL="unit"
        info "No KVM or Firecracker — running unit tests only"
    fi
fi

# ── Unit Tests ───────────────────────────────────────────────────────────────

run_unit() {
    step "Running unit tests..."
    uv run pytest tests/ -v -m "not integration" --tb=short
    echo ""
    info "Unit tests complete ✓"
}

# ── Integration Tests ────────────────────────────────────────────────────────

run_integration() {
    step "Checking prerequisites..."

    local ok=true
    if ! has_kvm; then
        fail "KVM not available (/dev/kvm missing)"
        ok=false
    fi
    if ! has_rootfs; then
        fail "Rootfs not built (run: sudo ./guest/build_rootfs.sh)"
        ok=false
    fi
    if ! has_bridge; then
        fail "Network bridge not configured (run: sudo ./config/setup_network.sh)"
        ok=false
    fi
    if ! pool_running; then
        fail "Pool manager not running (run: uv run python -m fc_pool_manager.server --config config/fc-pool.yaml)"
        ok=false
    fi
    if ! gateway_running; then
        fail "Kernel Gateway not running (run: uv run jupyter kernelgateway --default_kernel_name=python3-firecracker)"
        ok=false
    fi

    if [[ "$ok" != "true" ]]; then
        echo ""
        fail "Prerequisites not met. See above for details."
        echo "  Run: ./scripts/run-tests.sh smoke  (for step-by-step setup guidance)"
        exit 1
    fi

    step "Running integration tests..."
    uv run pytest tests/test_integration.py -v -m integration --tb=long -s
    echo ""
    info "Integration tests complete ✓"
}

# ── Smoke Test (manual, step-by-step) ────────────────────────────────────────

run_smoke() {
    echo ""
    echo "=========================================="
    echo " Manual Smoke Test — Step by Step"
    echo "=========================================="
    echo ""

    # Step 1: Check pool manager
    step "1/6 — Checking pool manager..."
    if ! pool_running; then
        fail "Pool manager not responding on /var/run/fc-pool.sock"
        echo "  Start it: uv run python -m fc_pool_manager.server --config config/fc-pool.yaml -v"
        exit 1
    fi
    POOL_STATUS=$(curl -s --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status)
    echo "  Pool status: $POOL_STATUS"
    IDLE=$(echo "$POOL_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('idle',0))")
    if [[ "$IDLE" -eq 0 ]]; then
        warn "No idle VMs in pool — acquire will boot on demand (slower)"
    else
        info "Pool has $IDLE idle VMs ✓"
    fi
    echo ""

    # Step 2: Acquire a VM
    step "2/6 — Acquiring a VM from pool..."
    ACQUIRE_RESP=$(curl -s --unix-socket /var/run/fc-pool.sock \
        -X POST http://localhost/api/vms/acquire \
        -H 'Content-Type: application/json' \
        -d '{"vcpu": 1, "mem_mib": 512}')
    echo "  Response: $ACQUIRE_RESP"
    VM_ID=$(echo "$ACQUIRE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
    VM_IP=$(echo "$ACQUIRE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['ip'])")
    info "Acquired VM ${VM_ID} at ${VM_IP} ✓"
    echo ""

    # Step 3: Ping the VM
    step "3/6 — Pinging VM via network..."
    if ping -c 1 -W 2 "$VM_IP" &>/dev/null; then
        info "VM responds to ping ✓"
    else
        warn "VM did not respond to ping (may be normal if ICMP is blocked)"
    fi
    echo ""

    # Step 4: Health check via pool manager
    step "4/6 — Health check via pool API..."
    HEALTH=$(curl -s --unix-socket /var/run/fc-pool.sock \
        "http://localhost/api/vms/${VM_ID}/health")
    echo "  Health: $HEALTH"
    ALIVE=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('alive', False))")
    if [[ "$ALIVE" == "True" ]]; then
        info "Guest agent is alive ✓"
    else
        fail "Guest agent not responding"
    fi
    echo ""

    # Step 5: Release the VM
    step "5/6 — Releasing VM..."
    curl -s --unix-socket /var/run/fc-pool.sock \
        -X POST "http://localhost/api/vms/${VM_ID}/release" \
        -H 'Content-Type: application/json' \
        -d '{"destroy": true}' >/dev/null
    info "VM released ✓"
    echo ""

    # Step 6: Verify pool replenished
    step "6/6 — Checking pool replenished..."
    sleep 2
    POOL_STATUS=$(curl -s --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status)
    echo "  Pool status: $POOL_STATUS"
    info "Smoke test complete ✓"

    echo ""
    echo "=========================================="
    echo " Next: Run the full integration test"
    echo "  uv run pytest tests/test_integration.py -v -m integration"
    echo "=========================================="
}

# ── Dispatch ─────────────────────────────────────────────────────────────────

case "$LEVEL" in
    unit)
        run_unit
        ;;
    integration)
        run_integration
        ;;
    smoke)
        run_smoke
        ;;
    all)
        run_unit
        echo ""
        echo "────────────────────────────────────────"
        echo ""
        run_integration
        ;;
    *)
        echo "Usage: $0 {unit|integration|smoke|all|auto}"
        exit 1
        ;;
esac
