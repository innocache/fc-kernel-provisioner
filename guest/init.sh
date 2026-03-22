#!/bin/sh
# Minimal /init for Firecracker microVM.
# Static IP is configured via kernel boot args (ip= parameter).
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mkdir -p /run /tmp

# Bring up loopback
ip link set lo up

# eth0 is configured by kernel boot args: ip=<ip>::<gw>:<mask>::eth0:off
# Just need to bring the link up
ip link set eth0 up

# Supervisor loop: restart guest agent if it crashes.
# PID 1 must not exit or the kernel panics.
while true; do
    python3 /usr/local/bin/fc-guest-agent
    echo "[init] guest agent exited ($?), restarting in 1s..." >&2
    sleep 1
done
