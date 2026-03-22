#!/bin/bash
# Build the guest rootfs ext4 image for Firecracker microVMs.
# Must be run as root on an x86_64 Linux host.
#
# Usage: sudo ./build_rootfs.sh [output_path]
# Default output: /opt/firecracker/rootfs.ext4
set -euo pipefail

ROOTFS_DIR=$(mktemp -d)
IMAGE=${1:-/opt/firecracker/rootfs.ext4}
IMAGE_SIZE_MB=512
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

cleanup() {
    echo "==> Cleaning up"
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
