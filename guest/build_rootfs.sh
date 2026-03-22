#!/bin/bash
# Build the guest rootfs ext4 image for Firecracker microVMs.
# Must be run as root on an x86_64 Linux host.
#
# Usage:
#   sudo ./build_rootfs.sh [output_path]    Build the rootfs image
#   sudo ./build_rootfs.sh --clean [path]   Remove the built image
#
# Default output: /opt/firecracker/rootfs.ext4
#
# Host impact: This script uses temp dirs (auto-cleaned) and writes ONLY
# the output ext4 file. No packages are installed on the host, no system
# config is modified. All package installation happens inside a chroot.
set -euo pipefail

# ── Clean mode ───────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--clean" ]]; then
    IMAGE=${2:-/opt/firecracker/rootfs.ext4}
    if [[ -f "$IMAGE" ]]; then
        echo "==> Removing rootfs image: $IMAGE"
        rm -f "$IMAGE"
        echo "==> Cleaned"
    else
        echo "==> Nothing to clean (no file at $IMAGE)"
    fi
    exit 0
fi

# ── Build mode ───────────────────────────────────────────────────────────────

ROOTFS_DIR=$(mktemp -d)
IMAGE=${1:-/opt/firecracker/rootfs.ext4}
IMAGE_SIZE_MB=512
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

cleanup() {
    echo "==> Cleaning up temp dirs"
    if mountpoint -q "${MOUNT_DIR:-/nonexistent}" 2>/dev/null; then
        umount "$MOUNT_DIR"
    fi
    rm -rf "$ROOTFS_DIR" "${MOUNT_DIR:-}"
}
trap cleanup EXIT

echo "==> Bootstrapping Alpine into $ROOTFS_DIR"
apk --root "$ROOTFS_DIR" --initdb --arch x86_64 \
    --repository https://dl-cdn.alpinelinux.org/alpine/v3.19/main \
    --repository https://dl-cdn.alpinelinux.org/alpine/v3.19/community \
    add alpine-base python3 py3-pip py3-numpy py3-scipy \
        py3-matplotlib py3-pandas iproute2

echo "==> Installing Python packages"
chroot "$ROOTFS_DIR" pip3 install --break-system-packages \
    ipykernel jupyter-client \
    plotly seaborn bokeh panel hvplot

echo "==> Installing guest agent"
cp "$SCRIPT_DIR/fc_guest_agent.py" "$ROOTFS_DIR/usr/local/bin/fc-guest-agent"
chmod +x "$ROOTFS_DIR/usr/local/bin/fc-guest-agent"

echo "==> Installing init script"
cp "$SCRIPT_DIR/init.sh" "$ROOTFS_DIR/init"
chmod +x "$ROOTFS_DIR/init"

echo "==> Creating ext4 image ($IMAGE_SIZE_MB MB)"
mkdir -p "$(dirname "$IMAGE")"
dd if=/dev/zero of="$IMAGE" bs=1M count="$IMAGE_SIZE_MB"
mkfs.ext4 -F "$IMAGE"
MOUNT_DIR=$(mktemp -d)
mount -o loop "$IMAGE" "$MOUNT_DIR"
cp -a "$ROOTFS_DIR"/* "$MOUNT_DIR"/
umount "$MOUNT_DIR"

echo "==> Done: $IMAGE ($(du -h "$IMAGE" | cut -f1))"
