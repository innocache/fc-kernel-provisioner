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
IMAGE_SIZE_MB=1024
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ALPINE_VERSION="3.19"
ALPINE_MIRROR="https://dl-cdn.alpinelinux.org/alpine"

# Skip rebuild if rootfs already exists
if [[ -f "$IMAGE" ]]; then
    echo "==> Rootfs already exists at $IMAGE ($(du -h "$IMAGE" | cut -f1)), skipping build"
    echo "    To force rebuild: sudo rm $IMAGE && sudo $0"
    exit 0
fi

cleanup() {
    echo "==> Cleaning up temp dirs"
    if mountpoint -q "${MOUNT_DIR:-/nonexistent}" 2>/dev/null; then
        umount "$MOUNT_DIR"
    fi
    rm -rf "$ROOTFS_DIR" "${MOUNT_DIR:-}" "${APK_STATIC_DIR:-}"
}
trap cleanup EXIT

# Ensure apk is available (download static binary if not on Alpine)
APK_BIN="apk"
if ! command -v apk &>/dev/null; then
    echo "==> apk not found, downloading apk-tools-static..."
    APK_STATIC_DIR=$(mktemp -d)
    APK_TAR="$APK_STATIC_DIR/apk-tools-static.apk"
    curl -sSL "${ALPINE_MIRROR}/v${ALPINE_VERSION}/main/x86_64/APKINDEX.tar.gz" \
        -o "$APK_STATIC_DIR/APKINDEX.tar.gz"
    # Find the apk-tools-static package version from the index
    # Extract to file first (piping tar | awk fails with pipefail due to SIGPIPE)
    tar -xzf "$APK_STATIC_DIR/APKINDEX.tar.gz" -C "$APK_STATIC_DIR" APKINDEX
    APK_STATIC_PKG=$(awk '/^P:apk-tools-static/{found=1} found && /^V:/{print; exit}' \
        "$APK_STATIC_DIR/APKINDEX" | cut -d: -f2)
    curl -sSL "${ALPINE_MIRROR}/v${ALPINE_VERSION}/main/x86_64/apk-tools-static-${APK_STATIC_PKG}.apk" \
        -o "$APK_TAR"
    tar -xzf "$APK_TAR" -C "$APK_STATIC_DIR" sbin/apk.static 2>/dev/null
    APK_BIN="$APK_STATIC_DIR/sbin/apk.static"
    chmod +x "$APK_BIN"
    echo "==> Using static apk from $APK_BIN"
fi

echo "==> Bootstrapping Alpine into $ROOTFS_DIR"
"$APK_BIN" --root "$ROOTFS_DIR" --initdb --arch x86_64 \
    --repository "${ALPINE_MIRROR}/v${ALPINE_VERSION}/main" \
    --repository "${ALPINE_MIRROR}/v${ALPINE_VERSION}/community" \
    --allow-untrusted \
    add alpine-base python3 py3-pip py3-numpy py3-scipy \
        py3-matplotlib py3-pandas iproute2 haveged

# Set up DNS resolution for chroot (needed for pip downloads)
cp /etc/resolv.conf "$ROOTFS_DIR/etc/resolv.conf" 2>/dev/null || true

echo "==> Installing Python packages"
chroot "$ROOTFS_DIR" pip3 install --break-system-packages \
    ipykernel jupyter-client \
    plotly seaborn bokeh panel hvplot

echo "==> Installing guest agent"
cp "$SCRIPT_DIR/fc_guest_agent.py" "$ROOTFS_DIR/usr/local/bin/fc-guest-agent"
chmod +x "$ROOTFS_DIR/usr/local/bin/fc-guest-agent"

echo "==> Installing Panel dispatcher"
mkdir -p "$ROOTFS_DIR/opt/agent"
cp "$SCRIPT_DIR/dispatcher.py" "$ROOTFS_DIR/opt/agent/dispatcher.py"

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
