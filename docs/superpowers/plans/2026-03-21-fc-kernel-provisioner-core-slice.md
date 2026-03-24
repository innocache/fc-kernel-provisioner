# Firecracker Kernel Provisioner — Core Slice Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Execute Python code inside a jailed Firecracker microVM and return stdout to the host via the Jupyter kernel protocol.

**Architecture:** Separate-process model. Pool manager daemon boots jailed Firecracker VMs and exposes a Unix socket HTTP API. A Jupyter kernel provisioner plugin claims VMs from the pool and starts ipykernel inside them via vsock. ZMQ traffic flows over TAP networking. The Kernel Gateway connects to the kernel as if it were local.

**Tech Stack:** Python 3.11+, uv, asyncio, aiohttp, jupyter_client, ipykernel, Firecracker + jailer, AF_VSOCK, Alpine Linux rootfs

**Spec:** `docs/superpowers/specs/2026-03-21-fc-kernel-provisioner-design.md`

---

## Status Summary (updated 2026-03-22)

| Phase | Status | Notes |
|-------|--------|-------|
| Chunk 1: Scaffolding + Guest Agent (Tasks 1–3) | **DONE** | All implemented and tested |
| Chunk 2: Networking + Config (Tasks 4–6) | **DONE** | All implemented and tested |
| Chunk 3: Pool Manager Core (Tasks 7–10) | **DONE** | All implemented and tested |
| Chunk 4: Provisioner Plugin (Tasks 11–13) | **DONE** | All implemented and tested |
| Chunk 5: Integration Test (Tasks 14–15) | **DONE** | Integration test written; unit tests all pass |
| Post-plan: Edge case tests | **DONE** | 8 edge case test files added (PRs #10–#12) |
| Post-plan: Code review fixes | **DONE** | 22 issues fixed (PR #13) |
| Post-plan: Reversible host setup | **DONE** | Teardown modes for all scripts (PR #14) |
| Post-plan: Testing docs | **DONE** | docs/testing.md updated (PR #15) |
| Post-plan: Graceful test skips | **DONE** | pytest.importorskip for optional deps (PR #16) |
| Post-plan: README | **DONE** | Project README.md added (PR #24) |
| **Integration testing on KVM host** | **DONE** | 476 unit + 27 integration tests passing (current repo total) |

---

## Chunk 1: Project Scaffolding + Guest Agent

### Task 1: Project scaffolding — pyproject.toml and package structure

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `fc_provisioner/__init__.py`
- Create: `fc_pool_manager/__init__.py`
- Create: `tests/__init__.py`

- [x] **Step 1: Create pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "fc-kernel-provisioner"
version = "0.1.0"
description = "Jupyter kernel provisioner for Firecracker microVMs"
requires-python = ">=3.11"
dependencies = [
    "jupyter_client>=7.0",
    "aiohttp>=3.9",
    "pyyaml>=6.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-aiohttp>=1.0",
    "aioresponses>=0.7",
]

[project.entry-points."jupyter_client.kernel_provisioners"]
firecracker-provisioner = "fc_provisioner:FirecrackerProvisioner"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [x] **Step 2: Create .gitignore**

```gitignore
.venv/
__pycache__/
*.pyc
*.egg-info/
dist/
build/
.pytest_cache/
```

- [x] **Step 3: Create package init files**

`fc_provisioner/__init__.py`:
```python
from .provisioner import FirecrackerProvisioner

__all__ = ["FirecrackerProvisioner"]
```

`fc_pool_manager/__init__.py`:
```python
```

`tests/__init__.py`:
```python
```

- [x] **Step 4: Initialize uv and sync dependencies**

Run: `uv sync --group dev`
Expected: Creates `.venv/`, installs all dependencies. (provisioner import will fail until provisioner.py exists — that's fine)

- [x] **Step 5: Commit**

```bash
git add .gitignore pyproject.toml uv.lock fc_provisioner/__init__.py fc_pool_manager/__init__.py tests/__init__.py
git commit -m "feat: scaffold project structure with pyproject.toml and uv"
```

---

### Task 2: Guest agent — message protocol helpers

**Files:**
- Create: `guest/fc_guest_agent.py`
- Create: `tests/test_guest_agent.py`

The guest agent runs inside the VM (not importable from the host), but we can unit test its message handling logic in isolation.

- [x] **Step 1: Write failing tests for message handling**

`tests/test_guest_agent.py`:
```python
"""Tests for guest agent message handling logic.

The guest agent runs inside the VM, but we can test its pure message
handling functions by importing them directly. We mock subprocess.Popen
since there's no ipykernel available in the test environment.
"""

import json
import struct
import pytest
from unittest.mock import patch, MagicMock

import importlib.util
import os

AGENT_PATH = os.path.join(os.path.dirname(__file__), "..", "guest", "fc_guest_agent.py")


def load_agent_module():
    """Load the guest agent as a module without executing main()."""
    spec = importlib.util.spec_from_file_location("fc_guest_agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWriteConnectionFile:
    def test_writes_valid_json(self, tmp_path):
        agent = load_agent_module()
        path = str(tmp_path / "kernel.json")
        ports = {
            "shell_port": 5555,
            "iopub_port": 5556,
            "stdin_port": 5557,
            "control_port": 5558,
            "hb_port": 5559,
        }
        agent.write_connection_file(path, ports, "test-key-123")

        with open(path) as f:
            conn = json.load(f)

        assert conn["ip"] == "0.0.0.0"
        assert conn["transport"] == "tcp"
        assert conn["key"] == "test-key-123"
        assert conn["signature_scheme"] == "hmac-sha256"
        assert conn["shell_port"] == 5555
        assert conn["iopub_port"] == 5556
        assert conn["hb_port"] == 5559


class TestHandleMessage:
    @patch("subprocess.Popen")
    def test_start_kernel_success(self, mock_popen):
        agent = load_agent_module()
        agent.kernel_proc = None

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        msg = json.dumps({
            "action": "start_kernel",
            "ports": {
                "shell_port": 5555,
                "iopub_port": 5556,
                "stdin_port": 5557,
                "control_port": 5558,
                "hb_port": 5559,
            },
            "key": "abc123",
        }).encode()

        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "ready"
        assert resp["pid"] == 42

    def test_ping_response(self):
        agent = load_agent_module()
        agent.kernel_proc = None

        msg = json.dumps({"action": "ping"}).encode()
        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "alive"
        assert "uptime" in resp
        assert resp["kernel_alive"] is False

    @patch("subprocess.Popen")
    def test_restart_kernel_kills_existing(self, mock_popen):
        agent = load_agent_module()

        # Simulate a running kernel
        old_proc = MagicMock()
        old_proc.poll.return_value = None  # still running
        old_proc.wait.return_value = 0
        agent.kernel_proc = old_proc

        new_proc = MagicMock()
        new_proc.poll.return_value = None
        new_proc.pid = 99
        mock_popen.return_value = new_proc

        msg = json.dumps({
            "action": "restart_kernel",
            "ports": {
                "shell_port": 5555,
                "iopub_port": 5556,
                "stdin_port": 5557,
                "control_port": 5558,
                "hb_port": 5559,
            },
            "key": "newkey",
        }).encode()

        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "ready"
        assert resp["pid"] == 99
        old_proc.terminate.assert_called_once()

    @patch("os.killpg")
    @patch("os.getpgid", return_value=100)
    def test_signal_forwards_to_process_group(self, mock_getpgid, mock_killpg):
        agent = load_agent_module()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # running
        mock_proc.pid = 42
        agent.kernel_proc = mock_proc

        msg = json.dumps({"action": "signal", "signum": 15}).encode()
        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "ok"
        mock_killpg.assert_called_once_with(100, 15)

    def test_signal_no_kernel(self):
        agent = load_agent_module()
        agent.kernel_proc = None

        msg = json.dumps({"action": "signal", "signum": 2}).encode()
        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "error"
        assert "no kernel running" in resp["error"]

    def test_unknown_action(self):
        agent = load_agent_module()
        msg = json.dumps({"action": "explode"}).encode()
        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "error"
        assert "unknown action" in resp["error"]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guest_agent.py -v`
Expected: FAIL — `fc_guest_agent.py` does not exist yet

- [x] **Step 3: Implement the guest agent**

`guest/fc_guest_agent.py`:
```python
#!/usr/bin/env python3
"""Firecracker guest agent — runs as PID 1 inside each microVM.

Listens on AF_VSOCK port 52 for commands from the host.
Protocol: length-prefixed JSON (4-byte big-endian length header + JSON payload).

Commands:
  start_kernel  — start ipykernel with given ZMQ ports and HMAC key
  restart_kernel — kill existing kernel, start a new one
  signal        — forward a signal to the kernel process group
  ping          — health check, returns uptime and kernel status
"""

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time

VSOCK_PORT = 52
VSOCK_CID_ANY = 0xFFFFFFFF  # VMADDR_CID_ANY
HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

kernel_proc = None
boot_time = time.monotonic()


def write_connection_file(path: str, ports: dict, key: str):
    """Write a Jupyter kernel connection file."""
    conn = {
        "ip": "0.0.0.0",
        "transport": "tcp",
        "key": key,
        "signature_scheme": "hmac-sha256",
        "kernel_name": "python3",
        **ports,
    }
    with open(path, "w") as f:
        json.dump(conn, f)


def start_kernel(ports: dict, key: str) -> int:
    """Start ipykernel as a child process. Returns PID."""
    global kernel_proc

    # Kill existing kernel if any
    if kernel_proc and kernel_proc.poll() is None:
        kernel_proc.terminate()
        try:
            kernel_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kernel_proc.kill()
            kernel_proc.wait()

    conn_file = "/tmp/kernel.json"
    write_connection_file(conn_file, ports, key)

    kernel_proc = subprocess.Popen(
        [sys.executable, "-m", "ipykernel_launcher", "-f", conn_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )

    # Wait briefly to check it didn't crash immediately
    time.sleep(0.5)
    if kernel_proc.poll() is not None:
        stderr = kernel_proc.stderr.read().decode()
        raise RuntimeError(f"Kernel exited immediately: {stderr[:500]}")

    return kernel_proc.pid


def handle_message(data: bytes) -> bytes:
    """Process a host command and return a response."""
    global kernel_proc
    msg = json.loads(data)
    action = msg.get("action")

    if action in ("start_kernel", "restart_kernel"):
        try:
            pid = start_kernel(msg["ports"], msg.get("key", ""))
            resp = {"status": "ready", "pid": pid}
        except Exception as e:
            resp = {"status": "error", "error": str(e)}

    elif action == "signal":
        signum = msg.get("signum", signal.SIGINT)
        if kernel_proc and kernel_proc.poll() is None:
            os.killpg(os.getpgid(kernel_proc.pid), signum)
            resp = {"status": "ok"}
        else:
            resp = {"status": "error", "error": "no kernel running"}

    elif action == "ping":
        meminfo = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemFree:"):
                        meminfo["mem_free_mib"] = int(line.split()[1]) // 1024
                        break
        except Exception:
            pass
        resp = {
            "status": "alive",
            "uptime": int(time.monotonic() - boot_time),
            "kernel_alive": kernel_proc is not None and kernel_proc.poll() is None,
            **meminfo,
        }

    else:
        resp = {"status": "error", "error": f"unknown action: {action}"}

    return json.dumps(resp).encode()


def main():
    """Main loop: listen on vsock, handle commands."""
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((VSOCK_CID_ANY, VSOCK_PORT))
    sock.listen(5)

    print(f"[guest-agent] Listening on vsock port {VSOCK_PORT}", flush=True)

    while True:
        conn, addr = sock.accept()
        try:
            header = conn.recv(HEADER_SIZE)
            if len(header) < HEADER_SIZE:
                continue
            length = struct.unpack(HEADER_FMT, header)[0]
            data = b""
            while len(data) < length:
                chunk = conn.recv(length - len(data))
                if not chunk:
                    break
                data += chunk

            response = handle_message(data)
            conn.sendall(struct.pack(HEADER_FMT, len(response)))
            conn.sendall(response)
        except Exception as e:
            print(f"[guest-agent] Error: {e}", flush=True)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_guest_agent.py -v`
Expected: All 6 tests PASS

- [x] **Step 5: Commit**

```bash
git add guest/fc_guest_agent.py tests/test_guest_agent.py
git commit -m "feat: implement guest agent with vsock message handling"
```

---

### Task 3: Guest init script and rootfs build script

**Files:**
- Create: `guest/init.sh`
- Create: `guest/build_rootfs.sh`

These are shell scripts — no unit tests, validated by manual VM boot in Task 8.

- [x] **Step 1: Create the init script**

`guest/init.sh`:
```bash
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

# Start guest agent as PID 1's child
exec python3 /usr/local/bin/fc-guest-agent
```

- [x] **Step 2: Create the rootfs build script**

`guest/build_rootfs.sh`:
```bash
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
```

- [x] **Step 3: Make scripts executable**

Run: `chmod +x guest/build_rootfs.sh guest/init.sh`

- [x] **Step 4: Commit**

```bash
git add guest/init.sh guest/build_rootfs.sh
git commit -m "feat: add guest init script and rootfs build script"
```

---

## Chunk 2: Networking + Pool Manager Config

### Task 4: Network setup script

**Files:**
- Create: `config/setup_network.sh`

- [x] **Step 1: Create the network setup script**

`config/setup_network.sh`:
```bash
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
```

- [x] **Step 2: Make executable**

Run: `chmod +x config/setup_network.sh`

- [x] **Step 3: Commit**

```bash
git add config/setup_network.sh
git commit -m "feat: add host network bridge setup script"
```

---

### Task 5: Pool manager configuration

**Files:**
- Create: `config/fc-pool.yaml`
- Create: `config/kernel.json`
- Create: `config/fc-pool-manager.service`
- Create: `fc_pool_manager/config.py`
- Create: `tests/test_config.py`

- [x] **Step 1: Create config files**

`config/fc-pool.yaml`:
```yaml
pool:
  size: 5
  max_vms: 30
  replenish_threshold: 2
  health_check_interval: 30

vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/firecracker/vmlinux
  rootfs: /opt/firecracker/rootfs.ext4
  boot_args_template: "console=ttyS0 reboot=k panic=1 pci=off ip={vm_ip}::172.16.0.1:255.255.255.0::eth0:off init=/init"

network:
  bridge: fcbr0
  subnet: "172.16.0.0/24"
  gateway: "172.16.0.1"
  vm_ip_start: 2

jailer:
  enabled: true
  chroot_base: /srv/jailer
  exec_path: /usr/bin/firecracker
  uid: 123
  gid: 100
```

`config/kernel.json`:
```json
{
    "display_name": "Python 3 (Firecracker)",
    "language": "python",
    "argv": [],
    "metadata": {
        "kernel_provisioner": {
            "provisioner_name": "firecracker-provisioner",
            "config": {
                "pool_socket": "/var/run/fc-pool.sock",
                "vcpu_count": 1,
                "mem_size_mib": 512
            }
        }
    }
}
```

`config/fc-pool-manager.service`:
```ini
[Unit]
Description=Firecracker VM Pool Manager
After=network.target

[Service]
Type=simple
ExecStartPre=/bin/bash /opt/fc-kernel-provisioner/config/setup_network.sh
ExecStart=/usr/bin/python3 -m fc_pool_manager.server --config /opt/fc-kernel-provisioner/config/fc-pool.yaml
Restart=on-failure
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

- [x] **Step 2: Write failing test for config loader**

`tests/test_config.py`:
```python
"""Tests for pool manager configuration loader."""

import pytest
from fc_pool_manager.config import PoolConfig


class TestPoolConfig:
    def test_load_from_yaml(self, tmp_path):
        yaml_content = """
pool:
  size: 3
  max_vms: 10
  replenish_threshold: 1
  health_check_interval: 15

vm_defaults:
  vcpu: 2
  mem_mib: 1024
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "console=ttyS0 init=/init ip={vm_ip}::10.0.0.1:255.255.255.0::eth0:off"

network:
  bridge: testbr0
  subnet: "10.0.0.0/24"
  gateway: "10.0.0.1"
  vm_ip_start: 2

jailer:
  enabled: true
  chroot_base: /tmp/jailer
  exec_path: /usr/bin/firecracker
  uid: 1000
  gid: 1000
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_content)

        cfg = PoolConfig.from_yaml(str(config_file))

        assert cfg.pool_size == 3
        assert cfg.max_vms == 10
        assert cfg.replenish_threshold == 1
        assert cfg.health_check_interval == 15
        assert cfg.vm_vcpu == 2
        assert cfg.vm_mem_mib == 1024
        assert cfg.vm_kernel == "/opt/fc/vmlinux"
        assert cfg.vm_rootfs == "/opt/fc/rootfs.ext4"
        assert "{vm_ip}" in cfg.boot_args_template
        assert cfg.bridge == "testbr0"
        assert cfg.subnet == "10.0.0.0/24"
        assert cfg.gateway == "10.0.0.1"
        assert cfg.vm_ip_start == 2
        assert cfg.jailer_enabled is True
        assert cfg.chroot_base == "/tmp/jailer"
        assert cfg.firecracker_path == "/usr/bin/firecracker"
        assert cfg.jailer_uid == 1000
        assert cfg.jailer_gid == 1000

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            PoolConfig.from_yaml("/nonexistent/config.yaml")
```

- [x] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `fc_pool_manager.config` does not exist

- [x] **Step 4: Implement config loader**

`fc_pool_manager/config.py`:
```python
"""YAML configuration loader for the pool manager."""

from dataclasses import dataclass
import yaml


@dataclass(frozen=True)
class PoolConfig:
    """Typed, immutable configuration for the pool manager."""

    pool_size: int
    max_vms: int
    replenish_threshold: int
    health_check_interval: int

    vm_vcpu: int
    vm_mem_mib: int
    vm_kernel: str
    vm_rootfs: str
    boot_args_template: str

    bridge: str
    subnet: str
    gateway: str
    vm_ip_start: int

    jailer_enabled: bool
    chroot_base: str
    firecracker_path: str
    jailer_uid: int
    jailer_gid: int

    @classmethod
    def from_yaml(cls, path: str) -> "PoolConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)

        pool = raw["pool"]
        vm = raw["vm_defaults"]
        net = raw["network"]
        jail = raw["jailer"]

        return cls(
            pool_size=pool["size"],
            max_vms=pool["max_vms"],
            replenish_threshold=pool["replenish_threshold"],
            health_check_interval=pool["health_check_interval"],
            vm_vcpu=vm["vcpu"],
            vm_mem_mib=vm["mem_mib"],
            vm_kernel=vm["kernel"],
            vm_rootfs=vm["rootfs"],
            boot_args_template=vm["boot_args_template"],
            bridge=net["bridge"],
            subnet=net["subnet"],
            gateway=net["gateway"],
            vm_ip_start=net["vm_ip_start"],
            jailer_enabled=jail["enabled"],
            chroot_base=jail["chroot_base"],
            firecracker_path=jail["exec_path"],
            jailer_uid=jail["uid"],
            jailer_gid=jail["gid"],
        )
```

- [x] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: All 2 tests PASS

- [x] **Step 6: Commit**

```bash
git add config/fc-pool.yaml config/kernel.json config/fc-pool-manager.service fc_pool_manager/config.py tests/test_config.py
git commit -m "feat: add pool manager config loader and configuration files"
```

---

### Task 6: Network manager — IP allocation and TAP management

**Files:**
- Create: `fc_pool_manager/network.py`
- Create: `tests/test_network.py`

- [x] **Step 1: Write failing tests**

`tests/test_network.py`:
```python
"""Tests for IP allocation and TAP device naming."""

import pytest
from fc_pool_manager.network import IPAllocator, NetworkManager


class TestIPAllocator:
    def test_allocate_first_ip(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=254)
        ip = alloc.allocate()
        assert ip == "172.16.0.2"

    def test_allocate_sequential(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=254)
        ip1 = alloc.allocate()
        ip2 = alloc.allocate()
        assert ip1 != ip2
        assert ip1 == "172.16.0.2"
        assert ip2 == "172.16.0.3"

    def test_release_and_reuse(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=254)
        ip = alloc.allocate()
        alloc.release(ip)
        ip2 = alloc.allocate()
        assert ip2 == ip

    def test_exhaustion_raises(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=3)
        alloc.allocate()
        alloc.allocate()
        with pytest.raises(RuntimeError, match="exhausted"):
            alloc.allocate()

    def test_available_count(self):
        alloc = IPAllocator(gateway="172.16.0.1", start=2, end=254)
        assert alloc.available == 253
        alloc.allocate()
        assert alloc.available == 252


class TestNetworkManager:
    @pytest.fixture
    def nm(self):
        return NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=2)

    def test_tap_name_within_ifnamsiz(self, nm):
        name = nm._tap_name("a1b2c3d4")
        assert name == "tap-a1b2c3d4"
        assert len(name) <= 15

    def test_mac_from_ip(self, nm):
        mac = nm._mac_from_ip("172.16.0.2")
        assert mac == "AA:FC:00:00:00:02"
        mac = nm._mac_from_ip("172.16.0.255")
        assert mac == "AA:FC:00:00:00:FF"

    def test_allocate_and_release_ip(self, nm):
        ip = nm.allocate_ip()
        assert ip == "172.16.0.2"
        nm.release_ip(ip)
        ip2 = nm.allocate_ip()
        assert ip2 == ip
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_network.py -v`
Expected: FAIL — module does not exist

- [x] **Step 3: Implement network manager**

`fc_pool_manager/network.py`:
```python
"""Network management: IP allocation and TAP device lifecycle.

The IPAllocator is behind an interface for future multi-host support.
NetworkManager handles TAP creation/teardown via subprocess calls to `ip`.
"""

import asyncio
import subprocess
from typing import Protocol


class IPAllocatorProtocol(Protocol):
    """Interface for IP allocation — swap for multi-host or external IPAM."""

    def allocate(self) -> str: ...
    def release(self, ip: str) -> None: ...
    @property
    def available(self) -> int: ...


class IPAllocator:
    """Set-based IP allocator for a single /24 subnet."""

    def __init__(self, gateway: str, start: int, end: int):
        self._gateway = gateway
        self._prefix = gateway.rsplit(".", 1)[0]
        self._free: list[int] = list(range(start, end + 1))
        self._allocated: set[int] = set()

    def allocate(self) -> str:
        if not self._free:
            raise RuntimeError("IP address pool exhausted")
        octet = self._free.pop(0)
        self._allocated.add(octet)
        return f"{self._prefix}.{octet}"

    def release(self, ip: str) -> None:
        octet = int(ip.rsplit(".", 1)[1])
        if octet in self._allocated:
            self._allocated.discard(octet)
            self._free.insert(0, octet)

    @property
    def available(self) -> int:
        return len(self._free)


class NetworkManager:
    """Manages TAP devices and IP allocation for Firecracker VMs."""

    def __init__(self, bridge: str, gateway: str, vm_ip_start: int, vm_ip_end: int = 254):
        self.bridge = bridge
        self.ip_allocator = IPAllocator(gateway=gateway, start=vm_ip_start, end=vm_ip_end)

    def _tap_name(self, short_id: str) -> str:
        """Generate TAP device name from the short UUID hex (8 chars)."""
        return f"tap-{short_id}"

    def _mac_from_ip(self, ip: str) -> str:
        """Generate a deterministic MAC address from the VM IP."""
        last_octet = int(ip.rsplit(".", 1)[1])
        return f"AA:FC:00:00:00:{last_octet:02X}"

    def allocate_ip(self) -> str:
        return self.ip_allocator.allocate()

    def release_ip(self, ip: str) -> None:
        self.ip_allocator.release(ip)

    async def create_tap(self, short_id: str) -> str:
        """Create a TAP device and attach it to the bridge. Returns TAP name."""
        tap = self._tap_name(short_id)
        await self._run("ip", "tuntap", "add", tap, "mode", "tap")
        await self._run("ip", "link", "set", tap, "master", self.bridge)
        await self._run("ip", "link", "set", tap, "up")
        return tap

    async def delete_tap(self, tap_name: str) -> None:
        """Delete a TAP device (auto-detaches from bridge)."""
        try:
            await self._run("ip", "link", "del", tap_name)
        except subprocess.CalledProcessError:
            pass

    async def _run(self, *cmd: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=stderr)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_network.py -v`
Expected: All 8 tests PASS

- [x] **Step 5: Commit**

```bash
git add fc_pool_manager/network.py tests/test_network.py
git commit -m "feat: implement IP allocator and network manager"
```

---

## Chunk 3: Pool Manager Core

### Task 7: VM instance and CID allocator

**Files:**
- Create: `fc_pool_manager/vm.py`
- Create: `tests/test_vm.py`

- [x] **Step 1: Write failing tests**

`tests/test_vm.py`:
```python
"""Tests for VMInstance and CID allocation."""

import pytest
from fc_pool_manager.vm import VMInstance, VMState, CIDAllocator


class TestVMState:
    def test_valid_transitions(self):
        assert VMState.BOOTING.can_transition_to(VMState.IDLE)
        assert VMState.IDLE.can_transition_to(VMState.ASSIGNED)
        assert VMState.ASSIGNED.can_transition_to(VMState.STOPPING)
        assert VMState.BOOTING.can_transition_to(VMState.STOPPING)

    def test_invalid_transition(self):
        assert not VMState.IDLE.can_transition_to(VMState.BOOTING)
        assert not VMState.ASSIGNED.can_transition_to(VMState.IDLE)


class TestCIDAllocator:
    def test_first_cid_is_three(self):
        alloc = CIDAllocator()
        assert alloc.allocate() == 3

    def test_sequential(self):
        alloc = CIDAllocator()
        assert alloc.allocate() == 3
        assert alloc.allocate() == 4

    def test_recycle(self):
        alloc = CIDAllocator()
        cid = alloc.allocate()
        alloc.release(cid)
        assert alloc.allocate() == cid


class TestVMInstance:
    def test_creation(self):
        vm = VMInstance(
            vm_id="vm-a1b2c3d4",
            short_id="a1b2c3d4",
            ip="172.16.0.2",
            cid=3,
            tap_name="tap-a1b2c3d4",
            mac="AA:FC:00:00:00:02",
            jail_path="/srv/jailer/firecracker/vm-a1b2c3d4/root",
            vsock_path="/srv/jailer/firecracker/vm-a1b2c3d4/root/v.sock",
        )
        assert vm.state == VMState.BOOTING
        assert vm.vm_id == "vm-a1b2c3d4"

    def test_transition(self):
        vm = VMInstance(
            vm_id="vm-test",
            short_id="test1234",
            ip="172.16.0.2",
            cid=3,
            tap_name="tap-test1234",
            mac="AA:FC:00:00:00:02",
            jail_path="/tmp/jail",
            vsock_path="/tmp/jail/v.sock",
        )
        vm.transition_to(VMState.IDLE)
        assert vm.state == VMState.IDLE

    def test_invalid_transition_raises(self):
        vm = VMInstance(
            vm_id="vm-test",
            short_id="test1234",
            ip="172.16.0.2",
            cid=3,
            tap_name="tap-test1234",
            mac="AA:FC:00:00:00:02",
            jail_path="/tmp/jail",
            vsock_path="/tmp/jail/v.sock",
        )
        with pytest.raises(ValueError, match="Invalid state transition"):
            vm.transition_to(VMState.ASSIGNED)
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_vm.py -v`
Expected: FAIL — module does not exist

- [x] **Step 3: Implement VMInstance and CIDAllocator**

`fc_pool_manager/vm.py`:
```python
"""VM instance state management and CID allocation."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VMState(Enum):
    """VM lifecycle states."""
    BOOTING = "booting"
    IDLE = "idle"
    ASSIGNED = "assigned"
    STOPPING = "stopping"

    def can_transition_to(self, target: "VMState") -> bool:
        return target in _VALID_TRANSITIONS.get(self, set())


_VALID_TRANSITIONS = {
    VMState.BOOTING: {VMState.IDLE, VMState.STOPPING},
    VMState.IDLE: {VMState.ASSIGNED, VMState.STOPPING},
    VMState.ASSIGNED: {VMState.STOPPING},
    VMState.STOPPING: set(),
}


class CIDAllocator:
    """Allocates unique vsock CIDs starting at 3 (0-2 are reserved)."""

    def __init__(self, start: int = 3):
        self._next = start
        self._free: list[int] = []
        self._allocated: set[int] = set()

    def allocate(self) -> int:
        if self._free:
            cid = self._free.pop(0)
        else:
            cid = self._next
            self._next += 1
        self._allocated.add(cid)
        return cid

    def release(self, cid: int) -> None:
        if cid in self._allocated:
            self._allocated.discard(cid)
            self._free.append(cid)


@dataclass
class VMInstance:
    """Represents a single Firecracker microVM."""

    vm_id: str
    short_id: str
    ip: str
    cid: int
    tap_name: str
    mac: str
    jail_path: str
    vsock_path: str
    state: VMState = field(default=VMState.BOOTING)
    jailer_process: Optional[object] = field(default=None, repr=False)

    def transition_to(self, new_state: VMState) -> None:
        if not self.state.can_transition_to(new_state):
            raise ValueError(
                f"Invalid state transition: {self.state.value} -> {new_state.value} "
                f"for VM {self.vm_id}"
            )
        self.state = new_state
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_vm.py -v`
Expected: All 7 tests PASS

- [x] **Step 5: Commit**

```bash
git add fc_pool_manager/vm.py tests/test_vm.py
git commit -m "feat: implement VM state machine and CID allocator"
```

---

### Task 7b: Pool manager vsock helpers

The pool manager needs to communicate with guest agents via vsock for health checks
and boot verification. To avoid a circular dependency on `fc_provisioner` (which is
built in Chunk 4), the pool manager has its own minimal vsock module.

**Files:**
- Create: `fc_pool_manager/vsock.py`

- [x] **Step 1: Create vsock helpers**

`fc_pool_manager/vsock.py`:
```python
"""Minimal vsock communication for pool manager health checks.

This is a self-contained copy of the vsock protocol helpers that the pool
manager needs for boot verification and health checks. The provisioner
package (fc_provisioner) has its own full-featured vsock_client module.
Both use the same wire protocol: 4-byte big-endian length + JSON payload.
"""

import asyncio
import json
import struct
from typing import Any

GUEST_AGENT_PORT = 52
HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


async def vsock_request(
    vsock_uds_path: str,
    msg: dict[str, Any],
    timeout: float = 30,
) -> dict[str, Any]:
    """Send a request to the guest agent and return the response."""
    reader, writer = await asyncio.open_unix_connection(vsock_uds_path)
    try:
        # Firecracker vsock handshake
        writer.write(f"CONNECT {GUEST_AGENT_PORT}\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line.startswith(b"OK"):
            raise ConnectionError(f"Vsock handshake failed: {line.decode().strip()}")

        # Send request
        payload = json.dumps(msg).encode()
        writer.write(struct.pack(HEADER_FMT, len(payload)) + payload)
        await writer.drain()

        # Read response
        header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=timeout)
        length = struct.unpack(HEADER_FMT, header)[0]
        resp_data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(resp_data)
    finally:
        writer.close()
        await writer.wait_closed()
```

- [x] **Step 2: Commit**

```bash
git add fc_pool_manager/vsock.py
git commit -m "feat: add pool manager vsock helpers (avoids circular dependency)"
```

---

### Task 8: Firecracker API client

**Files:**
- Create: `fc_pool_manager/firecracker_api.py`
- Create: `tests/test_firecracker_api.py`

- [x] **Step 1: Write failing tests**

`tests/test_firecracker_api.py`:
```python
"""Tests for the Firecracker REST API client."""

import pytest
from fc_pool_manager.firecracker_api import FirecrackerAPI


class TestFirecrackerAPI:
    @pytest.fixture
    def api(self):
        return FirecrackerAPI(socket_path="/tmp/test-fc.sock")

    def test_build_machine_config(self, api):
        body = api._machine_config_body(vcpu=2, mem_mib=1024)
        assert body == {"vcpu_count": 2, "mem_size_mib": 1024}

    def test_build_boot_source(self, api):
        body = api._boot_source_body(
            kernel_path="vmlinux",
            boot_args="console=ttyS0 init=/init",
        )
        assert body["kernel_image_path"] == "vmlinux"
        assert body["boot_args"] == "console=ttyS0 init=/init"

    def test_build_drive(self, api):
        body = api._drive_body("rootfs", "overlay.ext4", is_root=True)
        assert body["drive_id"] == "rootfs"
        assert body["path_on_host"] == "overlay.ext4"
        assert body["is_root_device"] is True
        assert body["is_read_only"] is False

    def test_build_network_interface(self, api):
        body = api._network_body("eth0", "tap-abc", "AA:FC:00:00:00:02")
        assert body["iface_id"] == "eth0"
        assert body["host_dev_name"] == "tap-abc"
        assert body["guest_mac"] == "AA:FC:00:00:00:02"

    def test_build_vsock(self, api):
        body = api._vsock_body(cid=3, uds_path="v.sock")
        assert body["guest_cid"] == 3
        assert body["uds_path"] == "v.sock"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_firecracker_api.py -v`
Expected: FAIL — module does not exist

- [x] **Step 3: Implement Firecracker API client**

`fc_pool_manager/firecracker_api.py`:
```python
"""Async REST client for the Firecracker microVM API.

Firecracker exposes a REST API on a Unix domain socket. Each VM has its own
socket inside the jailed directory. All paths in API calls are relative to
the chroot root (not absolute host paths).
"""

import aiohttp
from typing import Any


class FirecrackerAPI:
    """Client for a single Firecracker VM's REST API socket."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._base_url = "http://localhost"

    def _connector(self) -> aiohttp.UnixConnector:
        return aiohttp.UnixConnector(path=self.socket_path)

    async def _put(self, path: str, body: dict[str, Any]) -> None:
        async with aiohttp.ClientSession(connector=self._connector()) as session:
            resp = await session.put(f"{self._base_url}{path}", json=body)
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(
                    f"Firecracker API error: PUT {path} -> {resp.status}: {text}"
                )

    def _machine_config_body(self, vcpu: int, mem_mib: int) -> dict:
        return {"vcpu_count": vcpu, "mem_size_mib": mem_mib}

    def _boot_source_body(self, kernel_path: str, boot_args: str) -> dict:
        return {"kernel_image_path": kernel_path, "boot_args": boot_args}

    def _drive_body(self, drive_id: str, path: str, is_root: bool) -> dict:
        return {
            "drive_id": drive_id,
            "path_on_host": path,
            "is_root_device": is_root,
            "is_read_only": False,
        }

    def _network_body(self, iface_id: str, tap_name: str, mac: str) -> dict:
        return {
            "iface_id": iface_id,
            "host_dev_name": tap_name,
            "guest_mac": mac,
        }

    def _vsock_body(self, cid: int, uds_path: str) -> dict:
        return {"guest_cid": cid, "uds_path": uds_path}

    async def configure_machine(self, vcpu: int, mem_mib: int) -> None:
        await self._put("/machine-config", self._machine_config_body(vcpu, mem_mib))

    async def configure_boot_source(self, kernel_path: str, boot_args: str) -> None:
        await self._put("/boot-source", self._boot_source_body(kernel_path, boot_args))

    async def configure_drive(self, drive_id: str, path: str, is_root: bool = True) -> None:
        await self._put(f"/drives/{drive_id}", self._drive_body(drive_id, path, is_root))

    async def configure_network(self, iface_id: str, tap_name: str, mac: str) -> None:
        await self._put(
            f"/network-interfaces/{iface_id}",
            self._network_body(iface_id, tap_name, mac),
        )

    async def configure_vsock(self, cid: int, uds_path: str) -> None:
        await self._put("/vsock", self._vsock_body(cid, uds_path))

    async def start(self) -> None:
        await self._put("/actions", {"action_type": "InstanceStart"})
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_firecracker_api.py -v`
Expected: All 5 tests PASS

- [x] **Step 5: Commit**

```bash
git add fc_pool_manager/firecracker_api.py tests/test_firecracker_api.py
git commit -m "feat: implement Firecracker REST API client"
```

---

### Task 9: Pool manager core — acquire/release/boot

**Files:**
- Create: `fc_pool_manager/manager.py`
- Create: `tests/test_pool_manager.py`

- [x] **Step 1: Write failing tests**

`tests/test_pool_manager.py`:
```python
"""Tests for pool manager core logic (mocked, no real VMs)."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from fc_pool_manager.manager import PoolManager
from fc_pool_manager.config import PoolConfig
from fc_pool_manager.vm import VMInstance, VMState


def make_test_config(tmp_path) -> PoolConfig:
    yaml_content = """
pool:
  size: 2
  max_vms: 5
  replenish_threshold: 1
  health_check_interval: 30
vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "console=ttyS0 ip={vm_ip}::172.16.0.1:255.255.255.0::eth0:off init=/init"
network:
  bridge: fcbr0
  subnet: "172.16.0.0/24"
  gateway: "172.16.0.1"
  vm_ip_start: 2
jailer:
  enabled: true
  chroot_base: /tmp/test-jailer
  exec_path: /usr/bin/firecracker
  uid: 1000
  gid: 1000
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_content)
    return PoolConfig.from_yaml(str(config_file))


def make_idle_vm(vm_id="vm-test1234", ip="172.16.0.2", cid=3):
    vm = VMInstance(
        vm_id=vm_id,
        short_id=vm_id.replace("vm-", ""),
        ip=ip,
        cid=cid,
        tap_name=f"tap-{vm_id.replace('vm-', '')}",
        mac="AA:FC:00:00:00:02",
        jail_path="/tmp/jail",
        vsock_path="/tmp/jail/v.sock",
    )
    vm.transition_to(VMState.IDLE)
    return vm


class TestPoolManagerAcquireRelease:
    @pytest.fixture
    def manager(self, tmp_path):
        config = make_test_config(tmp_path)
        mgr = PoolManager(config)
        mgr._boot_vm = AsyncMock(return_value=None)
        mgr._destroy_vm = AsyncMock(return_value=None)
        return mgr

    async def test_acquire_from_idle_pool(self, manager):
        vm = make_idle_vm()
        manager._vms["vm-test1234"] = vm

        result = await manager.acquire(vcpu=1, mem_mib=512)
        assert result["id"] == "vm-test1234"
        assert result["ip"] == "172.16.0.2"
        assert manager._vms["vm-test1234"].state == VMState.ASSIGNED

    async def test_acquire_fails_on_resource_mismatch(self, manager):
        manager._vms["vm-test1234"] = make_idle_vm()
        with pytest.raises(ValueError, match="do not match"):
            await manager.acquire(vcpu=4, mem_mib=2048)

    async def test_acquire_raises_on_exhaustion(self, manager):
        for i in range(manager._config.max_vms):
            vm = make_idle_vm(vm_id=f"vm-{i:08x}", ip=f"172.16.0.{i+2}", cid=i+3)
            vm.transition_to(VMState.ASSIGNED)
            manager._vms[vm.vm_id] = vm

        with pytest.raises(RuntimeError, match="pool_exhausted"):
            await manager.acquire(vcpu=1, mem_mib=512)

    async def test_release_destroys_vm(self, manager):
        vm = make_idle_vm()
        vm.transition_to(VMState.ASSIGNED)
        manager._vms["vm-test1234"] = vm

        await manager.release("vm-test1234", destroy=True)
        assert "vm-test1234" not in manager._vms
        manager._destroy_vm.assert_awaited_once()

    async def test_idle_count(self, manager):
        for i in range(3):
            manager._vms[f"vm-{i:08x}"] = make_idle_vm(
                vm_id=f"vm-{i:08x}", ip=f"172.16.0.{i+2}", cid=i+3
            )
        assert manager.idle_count == 3
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pool_manager.py -v`
Expected: FAIL — module does not exist

- [x] **Step 3: Implement pool manager**

`fc_pool_manager/manager.py`:
```python
"""Pool manager — maintains a pool of pre-warmed Firecracker microVMs."""

import asyncio
import logging
import os
import shutil
import uuid
from typing import Any, Optional

from .config import PoolConfig
from .firecracker_api import FirecrackerAPI
from .network import NetworkManager
from .vm import CIDAllocator, VMInstance, VMState

logger = logging.getLogger(__name__)


class PoolManager:
    """Manages a pool of Firecracker microVMs."""

    def __init__(self, config: PoolConfig):
        self._config = config
        self._vms: dict[str, VMInstance] = {}
        self._network = NetworkManager(
            bridge=config.bridge,
            gateway=config.gateway,
            vm_ip_start=config.vm_ip_start,
        )
        self._cid_alloc = CIDAllocator()
        self._boot_lock = asyncio.Lock()

    @property
    def idle_count(self) -> int:
        return sum(1 for vm in self._vms.values() if vm.state == VMState.IDLE)

    @property
    def total_count(self) -> int:
        return len(self._vms)

    def pool_status(self) -> dict[str, int]:
        counts = {"idle": 0, "assigned": 0, "booting": 0}
        for vm in self._vms.values():
            key = vm.state.value
            if key in counts:
                counts[key] += 1
        counts["max"] = self._config.max_vms
        return counts

    async def acquire(self, vcpu: int, mem_mib: int) -> dict[str, Any]:
        """Claim an idle VM from the pool."""
        if vcpu != self._config.vm_vcpu or mem_mib != self._config.vm_mem_mib:
            raise ValueError(
                f"Requested resources (vcpu={vcpu}, mem_mib={mem_mib}) "
                f"do not match pool profile "
                f"(vcpu={self._config.vm_vcpu}, mem_mib={self._config.vm_mem_mib})"
            )

        for vm in self._vms.values():
            if vm.state == VMState.IDLE:
                vm.transition_to(VMState.ASSIGNED)
                logger.info("Acquired VM %s (ip=%s)", vm.vm_id, vm.ip)
                asyncio.create_task(self.replenish())  # backfill pool
                return {
                    "id": vm.vm_id,
                    "ip": vm.ip,
                    "vsock_path": vm.vsock_path,
                }

        if self.total_count >= self._config.max_vms:
            raise RuntimeError("pool_exhausted")

        # No idle VMs but under max — boot one on demand
        logger.info("No idle VMs, booting on demand")
        vm = await self._boot_vm()
        vm.transition_to(VMState.ASSIGNED)
        return {
            "id": vm.vm_id,
            "ip": vm.ip,
            "vsock_path": vm.vsock_path,
        }

    async def release(self, vm_id: str, destroy: bool = True) -> None:
        """Release a VM back to the pool or destroy it."""
        vm = self._vms.get(vm_id)
        if vm is None:
            logger.warning("Release called for unknown VM %s", vm_id)
            return

        if destroy:
            vm.transition_to(VMState.STOPPING)
            await self._destroy_vm(vm)
            del self._vms[vm_id]
            logger.info("Destroyed VM %s", vm_id)

    async def is_alive(self, vm_id: str) -> dict[str, Any]:
        """Check if a VM is alive by pinging the guest agent."""
        vm = self._vms.get(vm_id)
        if vm is None:
            return {"alive": False}

        try:
            from .vsock import vsock_request
            resp = await vsock_request(vm.vsock_path, {"action": "ping"}, timeout=5)
            return {
                "alive": resp.get("status") == "alive",
                "uptime": resp.get("uptime", 0),
                "kernel_alive": resp.get("kernel_alive", False),
            }
        except Exception:
            return {"alive": False}

    async def _boot_vm(self) -> VMInstance:
        """Boot a new jailed Firecracker VM."""
        short_id = uuid.uuid4().hex[:8]
        vm_id = f"vm-{short_id}"
        ip = self._network.allocate_ip()
        cid = self._cid_alloc.allocate()
        tap_name = self._network._tap_name(short_id)
        mac = self._network._mac_from_ip(ip)

        jail_path = os.path.join(
            self._config.chroot_base, "firecracker", vm_id, "root"
        )
        vsock_path = os.path.join(jail_path, "v.sock")

        vm = VMInstance(
            vm_id=vm_id, short_id=short_id, ip=ip, cid=cid,
            tap_name=tap_name, mac=mac,
            jail_path=jail_path, vsock_path=vsock_path,
        )
        self._vms[vm_id] = vm

        try:
            os.makedirs(jail_path, exist_ok=True)
            kernel_dest = os.path.join(jail_path, "vmlinux")
            if not os.path.exists(kernel_dest):
                os.link(self._config.vm_kernel, kernel_dest)
            overlay_dest = os.path.join(jail_path, "overlay.ext4")
            # Use cp --reflink=auto for CoW on supported filesystems (btrfs, xfs)
            await self._run_cmd("cp", "--reflink=auto", self._config.vm_rootfs, overlay_dest)

            await self._network.create_tap(short_id)

            boot_args = self._config.boot_args_template.format(vm_ip=ip)
            jailer_cmd = [
                "jailer", "--id", vm_id,
                "--exec-file", self._config.firecracker_path,
                "--uid", str(self._config.jailer_uid),
                "--gid", str(self._config.jailer_gid),
                "--chroot-base-dir", self._config.chroot_base,
            ]
            jailer_proc = await asyncio.create_subprocess_exec(
                *jailer_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            vm.jailer_process = jailer_proc

            api_socket = os.path.join(jail_path, "run", "firecracker.socket")
            await self._wait_for_socket(api_socket, timeout=10)

            api = FirecrackerAPI(api_socket)
            await api.configure_machine(self._config.vm_vcpu, self._config.vm_mem_mib)
            await api.configure_boot_source("vmlinux", boot_args)
            await api.configure_drive("rootfs", "overlay.ext4", is_root=True)
            await api.configure_network("eth0", tap_name, mac)
            await api.configure_vsock(cid, "v.sock")
            await api.start()

            from .vsock import vsock_request
            resp = await vsock_request(vsock_path, {"action": "ping"}, timeout=30)
            if resp.get("status") != "alive":
                raise RuntimeError(f"Guest agent not ready: {resp}")

            vm.transition_to(VMState.IDLE)
            logger.info("VM %s booted (ip=%s, cid=%d)", vm_id, ip, cid)
            return vm

        except Exception:
            await self._destroy_vm(vm)
            del self._vms[vm_id]
            raise

    async def _destroy_vm(self, vm: VMInstance) -> None:
        """Tear down a VM: kill jailer, delete TAP, remove jail dir."""
        if vm.jailer_process and vm.jailer_process.returncode is None:
            vm.jailer_process.terminate()
            try:
                await asyncio.wait_for(vm.jailer_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                vm.jailer_process.kill()

        await self._network.delete_tap(vm.tap_name)
        self._network.release_ip(vm.ip)
        self._cid_alloc.release(vm.cid)

        if os.path.exists(vm.jail_path):
            await asyncio.to_thread(shutil.rmtree, vm.jail_path, ignore_errors=True)

    async def replenish(self) -> None:
        """Boot VMs until idle count meets pool_size."""
        async with self._boot_lock:
            while (
                self.idle_count < self._config.pool_size
                and self.total_count < self._config.max_vms
            ):
                try:
                    await self._boot_vm()
                except Exception as e:
                    logger.error("Failed to boot VM: %s", e)
                    break

    async def health_check_loop(self) -> None:
        """Periodically ping idle VMs and replace unhealthy ones."""
        while True:
            await asyncio.sleep(self._config.health_check_interval)
            for vm in list(self._vms.values()):
                if vm.state != VMState.IDLE:
                    continue
                health = await self.is_alive(vm.vm_id)
                if not health["alive"]:
                    logger.warning("VM %s unhealthy, replacing", vm.vm_id)
                    vm.transition_to(VMState.STOPPING)
                    await self._destroy_vm(vm)
                    del self._vms[vm.vm_id]
            await self.replenish()

    async def shutdown(self) -> None:
        """Gracefully stop all VMs."""
        logger.info("Shutting down pool manager, stopping %d VMs", len(self._vms))
        for vm in list(self._vms.values()):
            try:
                if vm.state != VMState.STOPPING:
                    vm.transition_to(VMState.STOPPING)
                await self._destroy_vm(vm)
            except Exception as e:
                logger.error("Error stopping VM %s: %s", vm.vm_id, e)
        self._vms.clear()

    async def _run_cmd(self, *cmd: str) -> None:
        """Run a shell command."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Command {cmd} failed: {stderr.decode()}")

    async def _wait_for_socket(self, path: str, timeout: float) -> None:
        """Wait for a Unix socket file to appear."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if os.path.exists(path):
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"Socket {path} did not appear within {timeout}s")
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pool_manager.py -v`
Expected: All 5 tests PASS

- [x] **Step 5: Commit**

```bash
git add fc_pool_manager/manager.py tests/test_pool_manager.py
git commit -m "feat: implement pool manager with acquire/release and VM lifecycle"
```

---

### Task 10: Pool manager HTTP server

**Files:**
- Create: `fc_pool_manager/server.py`
- Create: `fc_pool_manager/__main__.py`
- Create: `tests/test_server.py`

- [x] **Step 1: Write failing tests**

`tests/test_server.py`:
```python
"""Tests for the pool manager HTTP server."""

import pytest
from aiohttp import web
from unittest.mock import AsyncMock, MagicMock
from fc_pool_manager.server import create_app


class TestPoolManagerServer:
    @pytest.fixture
    def mock_manager(self):
        mgr = MagicMock()
        mgr.acquire = AsyncMock(return_value={
            "id": "vm-test1234",
            "ip": "172.16.0.2",
            "vsock_path": "/tmp/v.sock",
        })
        mgr.release = AsyncMock()
        mgr.is_alive = AsyncMock(return_value={
            "alive": True, "uptime": 100, "kernel_alive": True,
        })
        mgr.pool_status = MagicMock(return_value={
            "idle": 3, "assigned": 1, "booting": 0, "max": 30,
        })
        return mgr

    @pytest.fixture
    def client(self, aiohttp_client, mock_manager):
        app = create_app(mock_manager)
        return aiohttp_client(app)

    async def test_acquire(self, client):
        c = await client
        resp = await c.post("/api/vms/acquire", json={"vcpu": 1, "mem_mib": 512})
        assert resp.status == 200
        data = await resp.json()
        assert data["id"] == "vm-test1234"

    async def test_acquire_exhaustion(self, client, mock_manager):
        mock_manager.acquire = AsyncMock(side_effect=RuntimeError("pool_exhausted"))
        c = await client
        resp = await c.post("/api/vms/acquire", json={"vcpu": 1, "mem_mib": 512})
        assert resp.status == 503
        data = await resp.json()
        assert data["error"] == "pool_exhausted"

    async def test_acquire_resource_mismatch(self, client, mock_manager):
        mock_manager.acquire = AsyncMock(
            side_effect=ValueError("do not match pool profile")
        )
        c = await client
        resp = await c.post("/api/vms/acquire", json={"vcpu": 4, "mem_mib": 2048})
        assert resp.status == 400

    async def test_release(self, client):
        c = await client
        resp = await c.post("/api/vms/vm-test1234/release", json={"destroy": True})
        assert resp.status == 200

    async def test_health(self, client):
        c = await client
        resp = await c.get("/api/vms/vm-test1234/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["alive"] is True

    async def test_pool_status(self, client):
        c = await client
        resp = await c.get("/api/pool/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["idle"] == 3
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL — module does not exist

- [x] **Step 3: Implement the HTTP server**

`fc_pool_manager/server.py`:
```python
"""Unix domain socket HTTP server for the pool manager API."""

import argparse
import asyncio
import logging
import signal

from aiohttp import web

from .config import PoolConfig
from .manager import PoolManager

logger = logging.getLogger(__name__)


def create_app(manager: PoolManager) -> web.Application:
    """Create the aiohttp application with all routes."""
    app = web.Application()
    app["manager"] = manager

    app.router.add_post("/api/vms/acquire", handle_acquire)
    app.router.add_post("/api/vms/{vm_id}/release", handle_release)
    app.router.add_get("/api/vms/{vm_id}/health", handle_health)
    app.router.add_get("/api/pool/status", handle_pool_status)

    return app


async def handle_acquire(request: web.Request) -> web.Response:
    manager: PoolManager = request.app["manager"]
    body = await request.json()
    vcpu = body.get("vcpu", 1)
    mem_mib = body.get("mem_mib", 512)

    try:
        result = await manager.acquire(vcpu=vcpu, mem_mib=mem_mib)
        return web.json_response(result)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except RuntimeError as e:
        if "pool_exhausted" in str(e):
            return web.json_response(
                {"error": "pool_exhausted", "retry_after_ms": 5000},
                status=503,
            )
        return web.json_response({"error": str(e)}, status=500)


async def handle_release(request: web.Request) -> web.Response:
    manager: PoolManager = request.app["manager"]
    vm_id = request.match_info["vm_id"]
    body = await request.json()
    destroy = body.get("destroy", True)
    await manager.release(vm_id, destroy=destroy)
    return web.json_response({"ok": True})


async def handle_health(request: web.Request) -> web.Response:
    manager: PoolManager = request.app["manager"]
    vm_id = request.match_info["vm_id"]
    result = await manager.is_alive(vm_id)
    return web.json_response(result)


async def handle_pool_status(request: web.Request) -> web.Response:
    manager: PoolManager = request.app["manager"]
    return web.json_response(manager.pool_status())


async def run_server(config_path: str, socket_path: str) -> None:
    """Start the pool manager and HTTP server."""
    config = PoolConfig.from_yaml(config_path)
    manager = PoolManager(config)

    app = create_app(manager)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, socket_path)
    await site.start()

    logger.info("Pool manager listening on %s", socket_path)

    await manager.replenish()

    health_task = asyncio.create_task(manager.health_check_loop())

    stop_event = asyncio.Event()

    def on_signal():
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal)

    await stop_event.wait()

    logger.info("Shutting down...")
    health_task.cancel()
    await manager.shutdown()
    await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description="Firecracker VM Pool Manager")
    parser.add_argument("--config", required=True, help="Path to fc-pool.yaml")
    parser.add_argument(
        "--socket", default="/var/run/fc-pool.sock",
        help="Unix socket path (default: /var/run/fc-pool.sock)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    asyncio.run(run_server(args.config, args.socket))


if __name__ == "__main__":
    main()
```

`fc_pool_manager/__main__.py`:
```python
from .server import main

main()
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`
Expected: All 6 tests PASS

- [x] **Step 5: Commit**

```bash
git add fc_pool_manager/server.py fc_pool_manager/__main__.py tests/test_server.py
git commit -m "feat: implement pool manager HTTP server with acquire/release/health/status"
```

---

## Chunk 4: Provisioner Plugin

### Task 11: Vsock client

**Files:**
- Create: `fc_provisioner/vsock_client.py`
- Create: `tests/test_vsock_client.py`

- [x] **Step 1: Write failing tests**

`tests/test_vsock_client.py`:
```python
"""Tests for the vsock client protocol helpers."""

import json
import struct
import pytest
from fc_provisioner.vsock_client import (
    HEADER_FMT, HEADER_SIZE, GUEST_AGENT_PORT,
    _encode_message, _decode_message,
)


class TestMessageFraming:
    def test_encode_message(self):
        msg = {"action": "ping"}
        encoded = _encode_message(msg)
        payload = json.dumps(msg).encode()
        expected = struct.pack(HEADER_FMT, len(payload)) + payload
        assert encoded == expected

    def test_decode_message(self):
        msg = {"status": "alive", "uptime": 42}
        payload = json.dumps(msg).encode()
        data = struct.pack(HEADER_FMT, len(payload)) + payload
        decoded = _decode_message(data)
        assert decoded == msg

    def test_guest_agent_port(self):
        assert GUEST_AGENT_PORT == 52

    def test_header_size(self):
        assert HEADER_SIZE == 4
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_vsock_client.py -v`
Expected: FAIL — module does not exist

- [x] **Step 3: Implement vsock client**

`fc_provisioner/vsock_client.py`:
```python
"""Host-side vsock communication with the guest agent.

Firecracker maps guest AF_VSOCK to a host-side Unix domain socket.
The host connects to that UDS and sends "CONNECT <port>\n" to reach
the guest agent listening on that port.

IMPORTANT: The guest agent is single-threaded and responds on the same
connection. vsock_request() sends AND receives on a single connection.
"""

import asyncio
import json
import struct
from typing import Any

GUEST_AGENT_PORT = 52
HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


def _encode_message(msg: dict[str, Any]) -> bytes:
    """Encode a dict as length-prefixed JSON."""
    payload = json.dumps(msg).encode()
    return struct.pack(HEADER_FMT, len(payload)) + payload


def _decode_message(data: bytes) -> dict[str, Any]:
    """Decode length-prefixed JSON from raw bytes."""
    length = struct.unpack(HEADER_FMT, data[:HEADER_SIZE])[0]
    payload = data[HEADER_SIZE : HEADER_SIZE + length]
    return json.loads(payload)


async def _handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    port: int = GUEST_AGENT_PORT,
) -> None:
    """Perform the Firecracker vsock handshake."""
    writer.write(f"CONNECT {port}\n".encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    if not line.startswith(b"OK"):
        raise ConnectionError(f"Vsock handshake failed: {line.decode().strip()}")


async def vsock_request(
    vsock_uds_path: str,
    msg: dict[str, Any],
    timeout: float = 30,
) -> dict[str, Any]:
    """Send a request and return the response on a single connection."""
    reader, writer = await asyncio.open_unix_connection(vsock_uds_path)
    try:
        await _handshake(reader, writer)

        writer.write(_encode_message(msg))
        await writer.drain()

        header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=timeout)
        length = struct.unpack(HEADER_FMT, header)[0]
        payload = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(payload)
    finally:
        writer.close()
        await writer.wait_closed()


async def vsock_send_only(
    vsock_uds_path: str,
    msg: dict[str, Any],
) -> None:
    """Send a fire-and-forget message (e.g., signal)."""
    reader, writer = await asyncio.open_unix_connection(vsock_uds_path)
    try:
        await _handshake(reader, writer)
        writer.write(_encode_message(msg))
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        await writer.wait_closed()
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_vsock_client.py -v`
Expected: All 4 tests PASS

- [x] **Step 5: Commit**

```bash
git add fc_provisioner/vsock_client.py tests/test_vsock_client.py
git commit -m "feat: implement vsock client with length-prefixed JSON protocol"
```

---

### Task 12: Pool client

**Files:**
- Create: `fc_provisioner/pool_client.py`
- Create: `tests/test_pool_client.py`

- [x] **Step 1: Write failing tests**

`tests/test_pool_client.py`:
```python
"""Tests for the pool client."""

import pytest
from fc_provisioner.pool_client import PoolClient


class TestPoolClient:
    @pytest.fixture
    def client(self):
        return PoolClient(socket_path="/tmp/test.sock")

    def test_init(self, client):
        assert client.socket_path == "/tmp/test.sock"

    def test_base_url(self, client):
        assert "localhost" in client._base_url
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pool_client.py -v`
Expected: FAIL — module does not exist

- [x] **Step 3: Implement pool client**

`fc_provisioner/pool_client.py`:
```python
"""Async client for the pool manager's Unix socket API."""

import aiohttp
from typing import Any


class PoolClient:
    """Communicates with the pool manager daemon over a Unix domain socket."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._base_url = "http://localhost"

    def _connector(self) -> aiohttp.UnixConnector:
        return aiohttp.UnixConnector(path=self.socket_path)

    async def acquire(self, vcpu: int = 1, mem_mib: int = 512) -> dict[str, Any]:
        """Claim a pre-warmed VM from the pool."""
        async with aiohttp.ClientSession(connector=self._connector()) as session:
            resp = await session.post(
                f"{self._base_url}/api/vms/acquire",
                json={"vcpu": vcpu, "mem_mib": mem_mib},
            )
            if resp.status == 503:
                data = await resp.json()
                raise RuntimeError(data.get("error", "pool_exhausted"))
            if resp.status == 400:
                data = await resp.json()
                raise ValueError(data.get("error", "bad request"))
            resp.raise_for_status()
            return await resp.json()

    async def release(self, vm_id: str, destroy: bool = True) -> None:
        """Release a VM back to the pool."""
        async with aiohttp.ClientSession(connector=self._connector()) as session:
            resp = await session.post(
                f"{self._base_url}/api/vms/{vm_id}/release",
                json={"destroy": destroy},
            )
            resp.raise_for_status()

    async def is_alive(self, vm_id: str) -> bool:
        """Check if a VM is still running."""
        async with aiohttp.ClientSession(connector=self._connector()) as session:
            resp = await session.get(f"{self._base_url}/api/vms/{vm_id}/health")
            if resp.status == 200:
                data = await resp.json()
                return data.get("alive", False)
            return False
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pool_client.py -v`
Expected: All 2 tests PASS

- [x] **Step 5: Commit**

```bash
git add fc_provisioner/pool_client.py tests/test_pool_client.py
git commit -m "feat: implement pool client for provisioner-to-pool-manager IPC"
```

---

### Task 13: FirecrackerProvisioner

**Files:**
- Create: `fc_provisioner/provisioner.py`
- Create: `tests/test_provisioner.py`

- [x] **Step 1: Write failing tests**

`tests/test_provisioner.py`:
```python
"""Tests for FirecrackerProvisioner (mocked, no real VMs)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fc_provisioner.provisioner import FirecrackerProvisioner, FirecrackerProcess


class TestFirecrackerProcess:
    async def test_poll_alive(self):
        pool_client = MagicMock()
        pool_client.is_alive = AsyncMock(return_value=True)
        proc = FirecrackerProcess("vm-test", pool_client)
        assert await proc.poll() is None

    async def test_poll_dead(self):
        pool_client = MagicMock()
        pool_client.is_alive = AsyncMock(return_value=False)
        proc = FirecrackerProcess("vm-test", pool_client)
        assert await proc.poll() == 1

    async def test_poll_caches_exit_code(self):
        pool_client = MagicMock()
        pool_client.is_alive = AsyncMock(return_value=False)
        proc = FirecrackerProcess("vm-test", pool_client)
        await proc.poll()
        pool_client.is_alive = AsyncMock(return_value=True)
        assert await proc.poll() == 1

    async def test_kill(self):
        pool_client = MagicMock()
        pool_client.release = AsyncMock()
        proc = FirecrackerProcess("vm-test", pool_client)
        await proc.kill()
        pool_client.release.assert_awaited_once_with("vm-test", destroy=True)
        assert proc._exit_code == -9


class TestFirecrackerProvisioner:
    @pytest.fixture
    def provisioner(self):
        p = FirecrackerProvisioner.__new__(FirecrackerProvisioner)
        p.kernel_id = "test-kernel"
        p.pool_socket = "/var/run/fc-pool.sock"
        p.vcpu_count = 1
        p.mem_size_mib = 512
        p.vm_id = None
        p.vm_ip = None
        p.vsock_path = None
        p.process = None
        p.pool_client = None
        p.connection_info = {
            "key": "test-hmac-key",
            "ip": "127.0.0.1",
            "transport": "tcp",
        }
        p.kernel_spec = MagicMock()
        p.kernel_spec.metadata = {
            "kernel_provisioner": {
                "config": {
                    "pool_socket": "/var/run/fc-pool.sock",
                    "vcpu_count": 1,
                    "mem_size_mib": 512,
                }
            }
        }
        return p

    @patch("fc_provisioner.provisioner.PoolClient")
    async def test_pre_launch_acquires_vm(self, MockPoolClient, provisioner):
        mock_client = MagicMock()
        mock_client.acquire = AsyncMock(return_value={
            "id": "vm-abc12345",
            "ip": "172.16.0.2",
            "vsock_path": "/srv/jailer/firecracker/vm-abc12345/root/v.sock",
        })
        MockPoolClient.return_value = mock_client

        with patch.object(
            FirecrackerProvisioner.__bases__[0], "pre_launch",
            new_callable=AsyncMock, return_value={}
        ):
            await provisioner.pre_launch()

        assert provisioner.vm_id == "vm-abc12345"
        assert provisioner.vm_ip == "172.16.0.2"
        mock_client.acquire.assert_awaited_once_with(vcpu=1, mem_mib=512)

    @patch("fc_provisioner.provisioner.vsock_request")
    async def test_launch_process_starts_kernel(self, mock_vsock, provisioner):
        provisioner.vm_id = "vm-abc12345"
        provisioner.vm_ip = "172.16.0.2"
        provisioner.vsock_path = "/tmp/v.sock"
        provisioner.pool_client = MagicMock()

        mock_vsock.return_value = {"status": "ready", "pid": 42}

        proc = await provisioner.launch_process(cmd=[])

        assert isinstance(proc, FirecrackerProcess)
        assert provisioner.connection_info["ip"] == "172.16.0.2"
        assert provisioner.connection_info["shell_port"] == 5555

        call_args = mock_vsock.call_args
        msg = call_args[0][1]
        assert msg["action"] == "start_kernel"
        assert msg["key"] == "test-hmac-key"

    async def test_cleanup_releases_vm(self, provisioner):
        provisioner.vm_id = "vm-abc12345"
        provisioner.pool_client = MagicMock()
        provisioner.pool_client.release = AsyncMock()
        provisioner.process = MagicMock()

        await provisioner.cleanup(restart=False)

        provisioner.pool_client.release.assert_awaited_once_with(
            "vm-abc12345", destroy=True
        )
        assert provisioner.vm_id is None
        assert provisioner.process is None

    @patch("fc_provisioner.provisioner.vsock_request")
    async def test_cleanup_restart(self, mock_vsock, provisioner):
        provisioner.vm_id = "vm-abc12345"
        provisioner.vsock_path = "/tmp/v.sock"
        provisioner.pool_client = MagicMock()
        mock_vsock.return_value = {"status": "ready", "pid": 43}

        await provisioner.cleanup(restart=True)

        msg = mock_vsock.call_args[0][1]
        assert msg["action"] == "restart_kernel"

    async def test_get_provisioner_info(self, provisioner):
        provisioner.vm_id = "vm-abc12345"
        provisioner.vm_ip = "172.16.0.2"
        provisioner.vsock_path = "/tmp/v.sock"

        with patch.object(
            FirecrackerProvisioner.__bases__[0], "get_provisioner_info",
            new_callable=AsyncMock,
            return_value={"provisioner_name": "firecracker-provisioner"},
        ):
            info = await provisioner.get_provisioner_info()

        assert info["vm_id"] == "vm-abc12345"
        assert info["vm_ip"] == "172.16.0.2"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_provisioner.py -v`
Expected: FAIL — `provisioner.py` does not exist

- [x] **Step 3: Implement the provisioner**

`fc_provisioner/provisioner.py`:
```python
"""FirecrackerProvisioner — launches Jupyter kernels inside Firecracker microVMs."""

import asyncio
from typing import Any, Optional

from jupyter_client.provisioning import KernelProvisionerBase

from .pool_client import PoolClient
from .vsock_client import vsock_request, vsock_send_only


class FirecrackerProcess:
    """Process-like handle for a kernel running inside a Firecracker VM."""

    def __init__(self, vm_id: str, pool_client: PoolClient):
        self.vm_id = vm_id
        self.pool_client = pool_client
        self._exit_code: Optional[int] = None

    async def poll(self) -> Optional[int]:
        if self._exit_code is not None:
            return self._exit_code
        alive = await self.pool_client.is_alive(self.vm_id)
        if not alive:
            self._exit_code = 1
            return 1
        return None

    async def kill(self):
        await self.pool_client.release(self.vm_id, destroy=True)
        self._exit_code = -9

    async def terminate(self):
        await self.kill()

    def send_signal(self, signum: int):
        pass


class FirecrackerProvisioner(KernelProvisionerBase):
    """Jupyter kernel provisioner that runs kernels in Firecracker microVMs."""

    pool_socket: str = "/var/run/fc-pool.sock"
    vcpu_count: int = 1
    mem_size_mib: int = 512

    vm_id: Optional[str] = None
    vm_ip: Optional[str] = None
    vsock_path: Optional[str] = None
    process: Optional[FirecrackerProcess] = None
    pool_client: Optional[PoolClient] = None

    KERNEL_PORTS = {
        "shell_port": 5555,
        "iopub_port": 5556,
        "stdin_port": 5557,
        "control_port": 5558,
        "hb_port": 5559,
    }

    def _apply_config(self):
        config = self.kernel_spec.metadata.get(
            "kernel_provisioner", {}
        ).get("config", {})
        self.pool_socket = config.get("pool_socket", self.pool_socket)
        self.vcpu_count = config.get("vcpu_count", self.vcpu_count)
        self.mem_size_mib = config.get("mem_size_mib", self.mem_size_mib)

    async def pre_launch(self, **kwargs) -> dict[str, Any]:
        self._apply_config()
        self.pool_client = PoolClient(self.pool_socket)

        vm = await self.pool_client.acquire(
            vcpu=self.vcpu_count, mem_mib=self.mem_size_mib,
        )
        self.vm_id = vm["id"]
        self.vm_ip = vm["ip"]
        self.vsock_path = vm["vsock_path"]

        kwargs["cmd"] = []
        return await super().pre_launch(**kwargs)

    async def launch_process(self, cmd: list[str], **kwargs) -> FirecrackerProcess:
        conn_info = self.connection_info
        key = conn_info.get("key", "")

        resp = await vsock_request(
            self.vsock_path,
            {"action": "start_kernel", "ports": self.KERNEL_PORTS, "key": key},
            timeout=30,
        )

        if resp.get("status") != "ready":
            raise RuntimeError(
                f"Guest agent failed to start kernel: {resp.get('error', 'unknown')}"
            )

        self.connection_info["ip"] = self.vm_ip
        self.connection_info["transport"] = "tcp"
        self.connection_info.update(self.KERNEL_PORTS)

        self.process = FirecrackerProcess(self.vm_id, self.pool_client)
        return self.process

    @property
    def has_process(self) -> bool:
        return self.process is not None

    async def poll(self) -> Optional[int]:
        if self.process is None:
            return 0
        return await self.process.poll()

    async def wait(self) -> Optional[int]:
        if self.process is None:
            return 0
        while True:
            status = await self.process.poll()
            if status is not None:
                return status
            await asyncio.sleep(1)

    async def send_signal(self, signum: int):
        if self.vsock_path:
            await vsock_send_only(
                self.vsock_path, {"action": "signal", "signum": signum},
            )

    async def kill(self, restart: bool = False):
        if self.process:
            await self.process.kill()

    async def terminate(self, restart: bool = False):
        await self.kill(restart=restart)

    async def cleanup(self, restart: bool = False):
        if restart and self.vsock_path:
            await vsock_request(
                self.vsock_path,
                {
                    "action": "restart_kernel",
                    "ports": self.KERNEL_PORTS,
                    "key": self.connection_info.get("key", ""),
                },
                timeout=30,
            )
        elif self.vm_id and self.pool_client:
            await self.pool_client.release(self.vm_id, destroy=True)
            self.vm_id = None
            self.vm_ip = None
            self.vsock_path = None
            self.process = None

    async def get_provisioner_info(self) -> dict[str, Any]:
        info = await super().get_provisioner_info()
        info["vm_id"] = self.vm_id
        info["vm_ip"] = self.vm_ip
        info["vsock_path"] = self.vsock_path
        info["pool_socket"] = self.pool_socket
        return info

    async def load_provisioner_info(self, info: dict[str, Any]):
        await super().load_provisioner_info(info)
        self.vm_id = info.get("vm_id")
        self.vm_ip = info.get("vm_ip")
        self.vsock_path = info.get("vsock_path")
        self.pool_socket = info.get("pool_socket", self.pool_socket)
        if self.vm_id:
            self.pool_client = PoolClient(self.pool_socket)
            self.process = FirecrackerProcess(self.vm_id, self.pool_client)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_provisioner.py -v`
Expected: All 7 tests PASS

- [x] **Step 5: Commit**

```bash
git add fc_provisioner/provisioner.py tests/test_provisioner.py
git commit -m "feat: implement FirecrackerProvisioner kernel provisioner plugin"
```

---

## Chunk 5: Integration Test + Final Verification

### Task 14: Integration smoke test

**Files:**
- Create: `tests/test_integration.py`
- Modify: `pyproject.toml` (add integration marker)

This test requires a running pool manager, Kernel Gateway, and real Firecracker VMs. Skip in CI, run manually on KVM hosts.

- [x] **Step 1: Write the integration test**

`tests/test_integration.py`:
```python
"""End-to-end integration test: code in -> Firecracker VM -> stdout out.

Prerequisites:
  1. Host has KVM enabled
  2. Rootfs built: guest/build_rootfs.sh
  3. Network setup: config/setup_network.sh
  4. Pool manager running: python -m fc_pool_manager.server --config config/fc-pool.yaml
  5. Kernel Gateway running: jupyter kernelgateway --default_kernel_name=python3-firecracker

Run: uv run pytest tests/test_integration.py -v -m integration
Skip: uv run pytest tests/ -v -m "not integration"
"""

import asyncio
import json
import os
import uuid

import aiohttp
import pytest

GATEWAY_URL = os.environ.get("KERNEL_GATEWAY_URL", "http://localhost:8888")

pytestmark = pytest.mark.integration


@pytest.fixture
async def kernel_id():
    """Start a kernel and yield its ID, then clean up."""
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{GATEWAY_URL}/api/kernels",
            json={"name": "python3-firecracker"},
        )
        resp.raise_for_status()
        data = await resp.json()
        kid = data["id"]

    yield kid

    async with aiohttp.ClientSession() as session:
        await session.delete(f"{GATEWAY_URL}/api/kernels/{kid}")


async def execute_code(kernel_id: str, code: str, timeout: float = 120) -> dict:
    """Execute code on a kernel via WebSocket and collect output."""
    msg_id = uuid.uuid4().hex
    results = {"stdout": "", "stderr": "", "error": None}

    ws_url = GATEWAY_URL.replace("http://", "ws://")

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"{ws_url}/api/kernels/{kernel_id}/channels"
        ) as ws:
            await ws.send_json({
                "header": {
                    "msg_id": msg_id,
                    "username": "",
                    "session": uuid.uuid4().hex,
                    "msg_type": "execute_request",
                    "version": "5.3",
                },
                "parent_header": {},
                "metadata": {},
                "content": {
                    "code": code,
                    "silent": False,
                    "store_history": True,
                    "user_expressions": {},
                    "allow_stdin": False,
                    "stop_on_error": True,
                },
                "buffers": [],
                "channel": "shell",
            })

            while True:
                raw = await asyncio.wait_for(ws.receive(), timeout=timeout)
                if raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                if raw.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    continue

                msg = json.loads(raw.data)
                parent_id = msg.get("parent_header", {}).get("msg_id")
                if parent_id != msg_id:
                    continue

                msg_type = msg["header"]["msg_type"]
                content = msg.get("content", {})

                if msg_type == "stream":
                    name = content.get("name", "stdout")
                    results[name] += content.get("text", "")
                elif msg_type == "error":
                    results["error"] = {
                        "name": content.get("ename", "Error"),
                        "value": content.get("evalue", ""),
                    }
                elif msg_type == "status":
                    if content.get("execution_state") == "idle":
                        break

    return results


class TestFullPipeline:
    async def test_hello_world(self, kernel_id):
        result = await execute_code(kernel_id, "print('hello')")
        assert result["stdout"].strip() == "hello"
        assert result["error"] is None

    async def test_state_persists_across_cells(self, kernel_id):
        await execute_code(kernel_id, "x = 42")
        result = await execute_code(kernel_id, "print(x)")
        assert result["stdout"].strip() == "42"

    async def test_error_handling(self, kernel_id):
        result = await execute_code(kernel_id, "1/0")
        assert result["error"] is not None
        assert result["error"]["name"] == "ZeroDivisionError"

    async def test_imports_work(self, kernel_id):
        result = await execute_code(kernel_id, "import numpy; print(numpy.__version__)")
        assert result["error"] is None
        assert result["stdout"].strip()

    async def test_multiline_output(self, kernel_id):
        result = await execute_code(kernel_id, "for i in range(3): print(i)")
        assert result["stdout"].strip() == "0\n1\n2"
```

- [x] **Step 2: Add pytest marker to pyproject.toml**

Add under `[tool.pytest.ini_options]`:
```toml
markers = [
    "integration: requires running pool manager and Kernel Gateway (deselect with '-m not integration')",
]
```

- [x] **Step 3: Verify unit tests still pass**

Run: `uv run pytest tests/ -v -m "not integration"`
Expected: All unit tests PASS, integration tests skipped

- [x] **Step 4: Commit**

```bash
git add tests/test_integration.py pyproject.toml
git commit -m "feat: add integration smoke test for full Firecracker pipeline"
```

---

### Task 15: Final verification

- [x] **Step 1: Run full unit test suite**

Run: `uv run pytest tests/ -v -m "not integration"`
Expected: All tests PASS

- [x] **Step 2: Verify package syncs**

Run: `uv sync --group dev`
Expected: No errors

- [x] **Step 3: Verify entry point**

Run: `uv run python -c "from fc_provisioner import FirecrackerProvisioner; print(FirecrackerProvisioner)"`
Expected: `<class 'fc_provisioner.provisioner.FirecrackerProvisioner'>`

- [x] **Step 4: Final commit if needed**

Run: `git status`
If untracked files: `git add` and commit them.

---

## Post-Plan Work (completed after Tasks 1–15)

### Task 16: Edge case tests (PR #10, #11, #12)

- [x] Added 8 edge case test files covering all modules:
  - `tests/test_config_edge_cases.py` — defaults, missing fields, format edge cases
  - `tests/test_guest_agent_edge_cases.py` — crash handling, missing fields, concurrent restart
  - `tests/test_network_edge_cases.py` — boundary IPs, double release, MAC generation
  - `tests/test_pool_manager_edge_cases.py` — race conditions, exhaustion, replenish behavior
  - `tests/test_provisioner_edge_cases.py` — cleanup, state round-trip, config defaults
  - `tests/test_server_edge_cases.py` — default params, 404, response structure
  - `tests/test_vm_edge_cases.py` — exhaustive state transitions, CID boundaries
  - `tests/test_vsock_client_edge_cases.py` — encode/decode edge cases, round-trip
- [x] Fixed bugs found during edge case testing:
  - Race condition in pool manager acquire with asyncio lock
  - CID allocator MAX_CID boundary (2^32-1)
  - VM state machine transition error messages include vm_id

### Task 17: Comprehensive code review fixes (PR #13)

22 issues fixed:

- [x] Fix #1: Reap zombie after kill in `_destroy_vm` — added `await vm.jailer_process.wait()`
- [x] Fix #2: Release error handling — wrapped `manager.release()` in try/except in server
- [x] Fix #3: `boot_args` injection — `.format(vm_ip=ip)` → `.replace("{vm_ip}", ip)`
- [x] Fix #5: Release handles STOPPING state — guard `if vm.state != VMState.STOPPING`
- [x] Fix #6: Health check lock — wrapped in `async with self._acquire_lock`
- [x] Fix #7: Guest agent waits for old kernel to die (timeout + kill fallback)
- [x] Fix #8: `vsock_send_only` — writer guard in finally block
- [x] Fix #9/#10: Deduplicated `launch_kernel`/`launch_process` into `_start_guest_kernel()`
- [x] Fix #11: `cleanup(restart=True)` checks response status, creates new FirecrackerProcess
- [x] Fix #12: Removed dead `replenish_threshold` from config
- [x] Fix #13: `is_alive` returns `dict[str, Any]` instead of `bool`
- [x] Fix #14: Body parsing — `request.can_read_body` check with fallback
- [x] Fix #15: systemd service comment for root requirement
- [x] Fix #17: `asyncio.get_event_loop()` → `asyncio.get_running_loop()`
- [x] Fix #18: `MAX_MESSAGE_SIZE = 1MB` validation in vsock_request
- [x] Fix #19: `uuid.uuid4().hex[:8]` → `secrets.token_hex(8)` for VM ID entropy
- [x] Fix #20: Guest init.sh — supervisor loop restarts agent on crash
- [x] Fix #21: Release endpoint POST → DELETE (`/api/vms/{vm_id}`)
- [x] Fix #22: CID in log message

### Task 18: Reversible host setup scripts (PR #14)

- [x] `scripts/setup-host.sh` — added `teardown` and `status` modes
- [x] `config/setup_network.sh` — added `teardown` mode (removes bridge, NAT, ebtables)
- [x] `guest/build_rootfs.sh` — added `--clean` flag and host pollution documentation

### Task 19: Testing documentation (PR #15)

- [x] Updated `docs/testing.md` with current test counts and coverage
- [x] Added per-module coverage table
- [x] Added cleanup/teardown section with host impact summary
- [x] Added Pool Manager API reference table

### Task 20: Graceful test skips (PR #16)

- [x] `fc_provisioner/__init__.py` — lazy import with try/except for jupyter_client
- [x] `pytest.importorskip("jupyter_client")` in provisioner test files
- [x] `pytest.importorskip("pytest_aiohttp")` in server test files
- [x] Result: 207 tests pass with all deps; tests skip gracefully without optional deps

### Task 21: Project README (PR #24)

- [x] Created `README.md` with architecture, quick start, API reference, configuration, and testing docs

---

## Remaining Work

All core slice implementation is complete. The following items require a **Linux host with KVM** and are tracked as GitHub issues:

| Issue | Title | Priority |
|-------|-------|----------|
| — | **Run integration smoke test on KVM host** | Next step |
| #17 | Sandbox client library + output capture | Follow-on |
| #18 | Dashboard serving (Panel + Caddy) | Follow-on |
| #19 | Execution API (FastAPI wrapper) | Follow-on |
| #20 | Network hardening | Follow-on |
| #21 | Snapshot optimization (sub-50ms cold starts) | Follow-on |
| #22 | Prometheus metrics endpoint | Follow-on |
| #23 | VM auto-cull (timeout idle VMs) | Follow-on |
