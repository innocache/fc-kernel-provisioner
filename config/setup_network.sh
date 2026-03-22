#!/bin/bash
# Set up the Firecracker network bridge on the host.
# Run once per host boot (before starting the pool manager).
# Must be run as root.
set -euo pipefail

BRIDGE=fcbr0
SUBNET=172.16.0
HOST_IP=${SUBNET}.1
HOST_IFACE=$(ip route | grep default | awk '{print $5}' | head -1)

echo "==> Creating bridge $BRIDGE"
ip link add $BRIDGE type bridge 2>/dev/null || true
ip addr add ${HOST_IP}/24 dev $BRIDGE 2>/dev/null || true
ip link set $BRIDGE up

echo "==> Enabling IP forwarding + NAT"
sysctl -w net.ipv4.ip_forward=1
iptables -t nat -C POSTROUTING -s ${SUBNET}.0/24 -o $HOST_IFACE -j MASQUERADE 2>/dev/null \
    || iptables -t nat -A POSTROUTING -s ${SUBNET}.0/24 -o $HOST_IFACE -j MASQUERADE
iptables -C FORWARD -i $BRIDGE -o $HOST_IFACE -j ACCEPT 2>/dev/null \
    || iptables -A FORWARD -i $BRIDGE -o $HOST_IFACE -j ACCEPT
iptables -C FORWARD -i $HOST_IFACE -o $BRIDGE -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
    || iptables -A FORWARD -i $HOST_IFACE -o $BRIDGE -m state --state RELATED,ESTABLISHED -j ACCEPT

echo "==> Blocking VM-to-VM direct traffic"
ebtables -C FORWARD -i tap-+ -o tap-+ -j DROP 2>/dev/null \
    || ebtables -A FORWARD -i tap-+ -o tap-+ -j DROP

echo "==> Network setup complete"
echo "    Bridge: $BRIDGE ($HOST_IP/24)"
echo "    NAT: ${SUBNET}.0/24 → $HOST_IFACE"
