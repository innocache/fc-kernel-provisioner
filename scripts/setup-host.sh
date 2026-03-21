#!/bin/bash
# ============================================================================
# Firecracker Host Setup — Ubuntu 24.04
#
# Prepares a fresh Ubuntu 24.04 host for running Firecracker microVMs.
# Must be run as root. Idempotent — safe to re-run.
#
# Target: Single host, 8 cores, 16GB RAM
# Usage:  sudo ./scripts/setup-host.sh
# ============================================================================
set -euo pipefail

FC_VERSION="1.6.0"
ARCH=$(uname -m)  # x86_64 or aarch64
FC_DIR="/opt/firecracker"
KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/${ARCH}/kernels/vmlinux.bin"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Preflight checks ────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (sudo)"
    exit 1
fi

if ! grep -q 'Ubuntu 24' /etc/os-release 2>/dev/null; then
    warn "This script is designed for Ubuntu 24.04. Proceeding anyway..."
fi

# ── Step 1: Verify KVM ──────────────────────────────────────────────────────

info "Checking KVM support..."
if [[ ! -e /dev/kvm ]]; then
    error "/dev/kvm not found. KVM is required for Firecracker."
    echo "  - If running on bare metal: enable VT-x/AMD-V in BIOS"
    echo "  - If running in a VM: enable nested virtualization"
    echo "  - On AWS: use a .metal instance or Nitro-based instance"
    exit 1
fi

# Ensure kvm group access
if ! getent group kvm >/dev/null; then
    groupadd kvm
fi
info "KVM available ✓"

# ── Step 2: Install system dependencies ──────────────────────────────────────

info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    curl \
    jq \
    iproute2 \
    iptables \
    ebtables \
    bridge-utils \
    python3 \
    python3-pip \
    python3-venv \
    git \
    e2fsprogs \
    util-linux \
    alpine-make-rootfs 2>/dev/null || true

# Install uv if not present
if ! command -v uv &>/dev/null; then
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
info "System packages installed ✓"

# ── Step 3: Download Firecracker + Jailer ────────────────────────────────────

info "Setting up Firecracker v${FC_VERSION}..."
mkdir -p "$FC_DIR"

if [[ ! -x /usr/bin/firecracker ]] || ! /usr/bin/firecracker --version 2>/dev/null | grep -q "$FC_VERSION"; then
    TARBALL="firecracker-v${FC_VERSION}-${ARCH}.tgz"
    DOWNLOAD_URL="https://github.com/firecracker-microvm/firecracker/releases/download/v${FC_VERSION}/${TARBALL}"

    info "Downloading Firecracker from ${DOWNLOAD_URL}..."
    cd /tmp
    curl -fsSL "$DOWNLOAD_URL" -o "$TARBALL"
    tar xzf "$TARBALL"

    RELEASE_DIR=$(find /tmp -maxdepth 1 -name "release-v${FC_VERSION}-*" -type d | head -1)
    cp "${RELEASE_DIR}/firecracker-v${FC_VERSION}-${ARCH}" /usr/bin/firecracker
    cp "${RELEASE_DIR}/jailer-v${FC_VERSION}-${ARCH}" /usr/bin/jailer
    chmod +x /usr/bin/firecracker /usr/bin/jailer

    rm -rf "$TARBALL" "$RELEASE_DIR"
fi

firecracker --version
jailer --version
info "Firecracker installed ✓"

# ── Step 4: Download Linux kernel ────────────────────────────────────────────

KERNEL_PATH="${FC_DIR}/vmlinux"
if [[ ! -f "$KERNEL_PATH" ]]; then
    info "Downloading Linux kernel for Firecracker..."
    curl -fsSL "$KERNEL_URL" -o "$KERNEL_PATH"
fi
info "Kernel available at ${KERNEL_PATH} ✓"

# ── Step 5: Create jailer directory structure ────────────────────────────────

JAILER_BASE="/srv/jailer"
info "Creating jailer directories..."
mkdir -p "$JAILER_BASE"

# Jailer user (unprivileged) — UID/GID 123/100 as per config
if ! id -u fc-jailer &>/dev/null 2>&1; then
    useradd --system --no-create-home --uid 123 --gid 100 --shell /usr/sbin/nologin fc-jailer || true
fi
info "Jailer directories ready ✓"

# ── Step 6: Configure system limits ─────────────────────────────────────────

info "Tuning system for Firecracker..."

# Increase max open files (each VM uses ~10 fds)
if ! grep -q 'fc-kernel-provisioner' /etc/security/limits.d/*.conf 2>/dev/null; then
    cat > /etc/security/limits.d/99-firecracker.conf << 'LIMITS'
# fc-kernel-provisioner: Firecracker VM limits
*    soft    nofile    65536
*    hard    nofile    65536
LIMITS
fi

# Enable IP forwarding persistently
if ! grep -q 'net.ipv4.ip_forward=1' /etc/sysctl.d/*.conf 2>/dev/null; then
    echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-firecracker.conf
    sysctl -p /etc/sysctl.d/99-firecracker.conf
fi

info "System tuned ✓"

# ── Step 7: Resource budget ──────────────────────────────────────────────────

TOTAL_CORES=$(nproc)
TOTAL_MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
VM_MEM_MB=512
# Reserve 2GB for host OS + pool manager + kernel gateway
HOST_RESERVED_MB=2048
MAX_VMS=$(( (TOTAL_MEM_MB - HOST_RESERVED_MB) / VM_MEM_MB ))
# Cap at reasonable concurrency given CPU count
if (( MAX_VMS > TOTAL_CORES * 4 )); then
    MAX_VMS=$(( TOTAL_CORES * 4 ))
fi

info "Resource budget:"
echo "  CPUs:         ${TOTAL_CORES}"
echo "  RAM:          ${TOTAL_MEM_MB} MB"
echo "  Host reserve: ${HOST_RESERVED_MB} MB"
echo "  VM memory:    ${VM_MEM_MB} MB each"
echo "  Max VMs:      ${MAX_VMS}"
echo "  Recommended pool size: $(( TOTAL_CORES / 2 )) (idle pre-warmed)"

# Write computed limits to a file the pool manager config can reference
cat > "${FC_DIR}/host-limits.env" << EOF
# Auto-generated by setup-host.sh — $(date -Iseconds)
TOTAL_CORES=${TOTAL_CORES}
TOTAL_MEM_MB=${TOTAL_MEM_MB}
MAX_VMS=${MAX_VMS}
RECOMMENDED_POOL_SIZE=$(( TOTAL_CORES / 2 ))
VM_MEM_MB=${VM_MEM_MB}
EOF

# ── Step 8: Verify everything ───────────────────────────────────────────────

info "Running verification checks..."
CHECKS_PASSED=0
CHECKS_TOTAL=0

check() {
    CHECKS_TOTAL=$((CHECKS_TOTAL + 1))
    if eval "$2" &>/dev/null; then
        echo "  ✓ $1"
        CHECKS_PASSED=$((CHECKS_PASSED + 1))
    else
        echo "  ✗ $1"
    fi
}

check "KVM available"           "[[ -e /dev/kvm ]]"
check "firecracker binary"      "firecracker --version"
check "jailer binary"           "jailer --version"
check "vmlinux kernel"          "[[ -f ${FC_DIR}/vmlinux ]]"
check "IP forwarding enabled"   "[[ \$(cat /proc/sys/net/ipv4/ip_forward) == 1 ]]"
check "uv installed"            "command -v uv"
check "python3 available"       "python3 --version"

echo ""
info "Checks passed: ${CHECKS_PASSED}/${CHECKS_TOTAL}"

if [[ $CHECKS_PASSED -eq $CHECKS_TOTAL ]]; then
    echo ""
    info "Host setup complete! Next steps:"
    echo ""
    echo "  1. Build the guest rootfs:"
    echo "     sudo ./guest/build_rootfs.sh"
    echo ""
    echo "  2. Setup the network bridge:"
    echo "     sudo ./config/setup_network.sh"
    echo ""
    echo "  3. Run the test suite:"
    echo "     ./scripts/run-tests.sh"
    echo ""
else
    warn "Some checks failed. Review the output above."
fi
