# Kernel Gateway + Firecracker Provisioner — Implementation Specification

> **Purpose**: This document is the complete implementation spec for a system that lets an LLM-powered chatbot execute Python code in Firecracker microVM sandboxes and serve the output (static charts, interactive HTML, persistent dashboards) back to users via embeddable URLs and iframes. It is written to be consumed by an AI coding assistant (Claude Code, Cursor, Copilot, etc.) as a single-file project blueprint.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Component Inventory](#3-component-inventory)
4. [Project File Structure](#4-project-file-structure)
5. [Component 1: FirecrackerProvisioner](#5-component-1-firecrackerprovisioner)
6. [Component 2: Pool Manager](#6-component-2-pool-manager)
7. [Component 3: Guest Agent](#7-component-3-guest-agent)
8. [Component 4: Guest Rootfs](#8-component-4-guest-rootfs)
9. [Component 5: Execution API](#9-component-5-execution-api)
10. [Component 6: Sandbox Client](#10-component-6-sandbox-client)
11. [Component 7: Dashboard Serving](#11-component-7-dashboard-serving)
12. [Component 8: Networking](#12-component-8-networking)
13. [Configuration Files](#13-configuration-files)
14. [LLM Tool Definitions](#14-llm-tool-definitions)
15. [Output Types and Capture Logic](#15-output-types-and-capture-logic)
16. [Security Model](#16-security-model)
17. [Resource Budget](#17-resource-budget)
18. [Testing Plan](#18-testing-plan)
19. [Build Order](#19-build-order)
20. [Migration Path: Local → Docker → Firecracker](#20-migration-path)

---

## 1. System Overview

### What This System Does

1. A user asks a question in a chatbot UI.
2. The chatbot backend calls an LLM (Claude API) with tool-calling enabled.
3. The LLM generates Python code and invokes an `execute_python` or `launch_dashboard` tool.
4. The tool handler sends the code to a **Jupyter Kernel Gateway** over WebSocket.
5. The Kernel Gateway delegates kernel provisioning to a custom **FirecrackerProvisioner**.
6. The provisioner claims a pre-warmed **Firecracker microVM** from a pool manager.
7. A **guest agent** inside the VM starts `ipykernel`, which executes the code.
8. Outputs (stdout, images, HTML, Plotly JSON) flow back through the Jupyter protocol.
9. The tool handler saves artifacts (PNGs, HTML files) to an artifact store.
10. The chatbot returns inline images, iframe URLs, and/or shareable dashboard links.

### Target Environment

- **Host**: Ubuntu 24.04, 8 CPU cores, 16 GB RAM, KVM enabled
- **Isolation**: Firecracker microVMs (KVM-based hardware virtualization)
- **Concurrency**: 25–50 simultaneous sandboxed kernels
- **Output types**: matplotlib/seaborn PNGs, self-contained Plotly HTML, persistent Panel/Bokeh dashboards

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Execution API | Jupyter Kernel Gateway (not JupyterHub) | Headless WebSocket API; no notebook UI; chatbot is the client |
| Kernel provisioning | `jupyter_client` Kernel Provisioner API | Pluggable; same API for Local/Docker/Firecracker; zero changes upstream |
| Host-to-guest data path | ZMQ over TCP via TAP bridge | ipykernel already speaks ZMQ/TCP; zero modifications to kernel code |
| Host-to-guest control path | JSON over AF_VSOCK port 52 | Out-of-band; doesn't interfere with ZMQ; reliable for lifecycle commands |
| Dashboard serving | Panel serve + Caddy reverse proxy | Panel supports Plotly, Bokeh, HoloViews; Caddy auto-TLS + dynamic routing |
| Rootfs strategy | Read-only base + CoW overlay per VM | Fast boot; no state leakage between users |

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Chatbot Backend (FastAPI)                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ LLM (Claude) │─▶│ Tool Router  │─▶│ Sandbox Client (WS)   │  │
│  │ tool calling  │  │              │  │ jupyter kernel proto   │  │
│  └──────────────┘  └──────────────┘  └───────────┬───────────┘  │
└──────────────────────────────────────────────────┼──────────────┘
                                                   │ WebSocket
┌──────────────────────────────────────────────────┼──────────────┐
│  Kernel Gateway (host process)                   ▼              │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ WebSocket API│─▶│KernelManager │─▶│FirecrackerProvisioner │  │
│  │ REST :8888   │  │ ZMQ client   │  │ (custom plugin)       │  │
│  └──────────────┘  └──────┬───────┘  └──────┬────────────────┘  │
│                           │ ZMQ/TCP         │ vsock (control)   │
│                           │                 │                   │
│  ┌───────────────────┐    │                 │                   │
│  │ Pool Manager      │◄───┼─────────────────┘                   │
│  │ (asyncio daemon)  │    │                                     │
│  └────────┬──────────┘    │                                     │
│           │               │                                     │
│  ─────────┼───────────────┼─── KVM boundary ──────────────────  │
│           │               │                                     │
│  ┌────────▼──────────┐    │   ┌──────────────────────────────┐  │
│  │ microVM 1         │    │   │ microVM 2                    │  │
│  │  172.16.0.2       │◄───┘   │  172.16.0.3                  │  │
│  │  ┌──────────────┐ │       │  ┌──────────────┐             │  │
│  │  │ Guest Agent  │ │       │  │ Guest Agent  │             │  │
│  │  │ vsock :52    │ │       │  │ vsock :52    │             │  │
│  │  └──────┬───────┘ │       │  └──────────────┘             │  │
│  │  ┌──────▼───────┐ │       │  ┌──────────────┐             │  │
│  │  │ ipykernel    │ │       │  │ ipykernel    │             │  │
│  │  │ ZMQ :5555-59 │ │       │  │ ZMQ :5555-59 │             │  │
│  │  └──────────────┘ │       │  └──────────────┘             │  │
│  └───────────────────┘       └──────────────────────────────┘   │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ Artifact     │  │ Panel serve  │  │ Caddy reverse proxy   │  │
│  │ Store (disk) │  │ :5006        │  │ :443 (auto-TLS)       │  │
│  └──────────────┘  └──────────────┘  └───────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Component Inventory

| # | Component | Language | ~Lines | Description |
|---|-----------|----------|--------|-------------|
| 1 | FirecrackerProvisioner | Python | ~400 | Kernel provisioner plugin: claims VMs, starts kernels, manages lifecycle |
| 2 | Pool Manager | Python | ~600 | Asyncio daemon: VM pool, TAP networking, Firecracker API client |
| 3 | Guest Agent | Python | ~250 | PID 1 inside each VM: vsock listener, kernel launcher, health reporter |
| 4 | Guest Rootfs | Shell | ~100 | Build script for ext4 image with Python + ipykernel + data science stack |
| 5 | Execution API | Python | ~200 | FastAPI wrapper (optional; Gateway is the primary API) |
| 6 | Sandbox Client | Python | ~300 | WebSocket client for chatbot backend; kernel lifecycle + output capture |
| 7 | Dashboard Serving | Config | ~50 | Caddy + Panel serve config for static artifacts + live dashboards |
| 8 | Networking | Shell | ~60 | Host bridge setup, TAP creation, iptables isolation rules |

---

## 4. Project File Structure

```
fc-kernel-provisioner/
├── fc_provisioner/                  # Component 1: Kernel Provisioner
│   ├── __init__.py                  # exports FirecrackerProvisioner
│   ├── provisioner.py               # FirecrackerProvisioner class
│   ├── pool_client.py               # Async client for pool manager Unix socket API
│   └── vsock_client.py              # Vsock send/recv helpers (host side)
│
├── fc_pool_manager/                 # Component 2: Pool Manager
│   ├── __init__.py
│   ├── manager.py                   # PoolManager class: pool lifecycle, replenishment
│   ├── vm.py                        # VMInstance class: single VM abstraction
│   ├── firecracker_api.py           # REST client for Firecracker Unix socket
│   ├── network.py                   # TAP device + bridge management
│   ├── config.py                    # YAML config loader
│   └── server.py                    # Unix socket API server (aiohttp)
│
├── guest/                           # Component 3 + 4: Guest Agent + Rootfs
│   ├── fc_guest_agent.py            # Guest agent (PID 1 inside VM)
│   ├── init.sh                      # Minimal /init script (mounts, network, exec agent)
│   └── build_rootfs.sh              # Rootfs build script
│
├── sandbox_client/                  # Component 6: Chatbot Sandbox Client
│   ├── __init__.py
│   ├── client.py                    # SandboxSession class (WebSocket to Gateway)
│   └── output.py                    # Output capture + artifact saving
│
├── config/                          # Component 7 + 8 + 13: Configuration
│   ├── kernel.json                  # Kernelspec with Firecracker provisioner
│   ├── fc-pool.yaml                 # Pool manager configuration
│   ├── Caddyfile                    # Reverse proxy config
│   ├── setup_network.sh             # Host bridge + iptables setup
│   └── fc-pool-manager.service      # systemd unit for pool manager
│
├── tests/
│   ├── test_provisioner.py
│   ├── test_pool_manager.py
│   ├── test_guest_agent.py
│   ├── test_sandbox_client.py
│   └── test_integration.py          # End-to-end: code in → artifacts out
│
├── pyproject.toml                   # Package config + entry points
└── README.md
```

---

## 5. Component 1: FirecrackerProvisioner

### File: `fc_provisioner/provisioner.py`

### Class: `FirecrackerProvisioner(KernelProvisionerBase)`

This is the central plugin. It implements the Jupyter kernel provisioner interface so the Kernel Gateway can launch kernels inside Firecracker microVMs instead of local processes.

### Dependencies

```
jupyter_client >= 7.0   # KernelProvisionerBase
aiohttp                 # async HTTP for pool manager
```

### Entry Point Registration

```toml
# pyproject.toml
[project.entry-points."jupyter_client.kernel_provisioners"]
firecracker-provisioner = "fc_provisioner:FirecrackerProvisioner"
```

### Interface Contract

The Kernel Gateway's KernelManager calls these methods in order:

```
1. pre_launch(**kwargs)     → Prepare environment, claim resources
2. launch_process(cmd, **kw) → Start the kernel process, return handle
3. poll()                    → Check if kernel is alive (None=alive, int=exit code)
4. send_signal(signum)       → Forward signal (e.g., SIGINT for interrupt)
5. cleanup(restart=False)    → Tear down (or restart) the kernel
6. get_provisioner_info()    → Return metadata for state persistence
```

### Full Implementation

```python
"""FirecrackerProvisioner — launches Jupyter kernels inside Firecracker microVMs."""

import os
import json
import asyncio
from typing import Any, Dict, List, Optional
from jupyter_client.provisioning import KernelProvisionerBase
from .pool_client import PoolClient
from .vsock_client import vsock_send, vsock_recv


class FirecrackerProcess:
    """Process-like handle for a kernel running inside a Firecracker VM.
    
    The KernelManager expects a process object with poll() and kill().
    """

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
        # Handled async in provisioner.send_signal()
        pass


class FirecrackerProvisioner(KernelProvisionerBase):
    """Jupyter kernel provisioner that runs kernels in Firecracker microVMs."""

    # -- Provisioner configuration (from kernel.json metadata.kernel_provisioner.config) --
    # These are set by _apply_config() from the kernelspec config stanza.
    pool_socket: str = "/var/run/fc-pool.sock"
    vcpu_count: int = 1
    mem_size_mib: int = 512
    rootfs_path: str = "/opt/firecracker/rootfs.ext4"
    kernel_path: str = "/opt/firecracker/vmlinux"

    # -- Internal state --
    vm_id: Optional[str] = None
    vm_ip: Optional[str] = None
    vsock_path: Optional[str] = None
    process: Optional[FirecrackerProcess] = None
    pool_client: Optional[PoolClient] = None

    # Fixed ZMQ ports inside the VM (ipykernel binds these)
    KERNEL_PORTS = {
        "shell_port": 5555,
        "iopub_port": 5556,
        "stdin_port": 5557,
        "control_port": 5558,
        "hb_port": 5559,
    }

    def _apply_config(self):
        """Read config from kernel.json's metadata.kernel_provisioner.config."""
        config = self.kernel_spec.metadata.get("kernel_provisioner", {}).get("config", {})
        self.pool_socket = config.get("pool_socket", self.pool_socket)
        self.vcpu_count = config.get("vcpu_count", self.vcpu_count)
        self.mem_size_mib = config.get("mem_size_mib", self.mem_size_mib)
        self.rootfs_path = config.get("rootfs_path", self.rootfs_path)
        self.kernel_path = config.get("kernel_path", self.kernel_path)

    async def pre_launch(self, **kwargs) -> Dict[str, Any]:
        """Claim a pre-warmed VM from the pool."""
        self._apply_config()
        self.pool_client = PoolClient(self.pool_socket)

        # 1. Acquire a VM
        self.vm = await self.pool_client.acquire(
            vcpu=self.vcpu_count,
            mem_mib=self.mem_size_mib,
        )
        self.vm_id = self.vm["id"]
        self.vm_ip = self.vm["ip"]
        self.vsock_path = self.vm["vsock_path"]

        # 2. Clear cmd — we handle launch via guest agent, not Popen
        kwargs["cmd"] = []

        return await super().pre_launch(**kwargs)

    async def launch_process(self, cmd: List[str], **kwargs) -> FirecrackerProcess:
        """Start ipykernel inside the microVM via guest agent."""
        # 1. Get the HMAC key from the connection info
        #    (KernelManager generates this for ZMQ auth)
        conn_info = self.connection_info
        key = conn_info.get("key", "")

        # 2. Tell guest agent to start ipykernel with our ports + key
        await vsock_send(self.vsock_path, {
            "action": "start_kernel",
            "ports": self.KERNEL_PORTS,
            "key": key,
        })

        # 3. Wait for readiness confirmation
        resp = await vsock_recv(self.vsock_path, timeout=30)
        if resp.get("status") != "ready":
            raise RuntimeError(
                f"Guest agent failed to start kernel: {resp.get('error', 'unknown')}"
            )

        # 4. Override connection info to point at VM's TAP IP
        #    This is the critical step: KernelManager's ZMQ client
        #    will connect to these addresses instead of localhost
        self.connection_info["ip"] = self.vm_ip
        self.connection_info["transport"] = "tcp"
        self.connection_info.update(self.KERNEL_PORTS)

        # 5. Return process handle
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
        """Forward signal to the kernel process inside the VM."""
        if self.vsock_path:
            try:
                await vsock_send(self.vsock_path, {
                    "action": "signal",
                    "signum": signum,
                })
            except Exception:
                pass  # VM may already be dead

    async def kill(self, restart: bool = False):
        if self.process:
            await self.process.kill()

    async def terminate(self, restart: bool = False):
        await self.kill(restart=restart)

    async def cleanup(self, restart: bool = False):
        """Destroy the VM and return it to the pool."""
        if restart and self.vsock_path:
            # Restart: kill kernel process but keep VM
            await vsock_send(self.vsock_path, {
                "action": "restart_kernel",
                "ports": self.KERNEL_PORTS,
                "key": self.connection_info.get("key", ""),
            })
        elif self.vm_id and self.pool_client:
            # Full stop: destroy VM
            await self.pool_client.release(self.vm_id, destroy=True)
            self.vm_id = None
            self.vm_ip = None
            self.vsock_path = None
            self.process = None

    async def get_provisioner_info(self) -> Dict[str, Any]:
        """Persist state for Gateway restarts."""
        info = await super().get_provisioner_info()
        info["vm_id"] = self.vm_id
        info["vm_ip"] = self.vm_ip
        info["vsock_path"] = self.vsock_path
        return info

    async def load_provisioner_info(self, info: Dict[str, Any]):
        """Restore state after Gateway restart."""
        await super().load_provisioner_info(info)
        self.vm_id = info.get("vm_id")
        self.vm_ip = info.get("vm_ip")
        self.vsock_path = info.get("vsock_path")
        if self.vm_id:
            self.pool_client = PoolClient(self.pool_socket)
            self.process = FirecrackerProcess(self.vm_id, self.pool_client)
```

### File: `fc_provisioner/pool_client.py`

```python
"""Async client for the pool manager's Unix socket API."""

import aiohttp
import json
from typing import Any, Dict, Optional


class PoolClient:
    """Communicates with the pool manager daemon over a Unix domain socket."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._base_url = "http://localhost"  # unused with Unix socket

    def _connector(self):
        return aiohttp.UnixConnector(path=self.socket_path)

    async def acquire(self, vcpu: int = 1, mem_mib: int = 512) -> Dict[str, Any]:
        """Claim a pre-warmed VM from the pool.
        
        Returns:
            {
                "id": "vm-abc123",
                "ip": "172.16.0.2",
                "vsock_path": "/var/run/fc-vms/vm-abc123/v.sock",
                "tap": "tap-vm-abc123",
            }
        """
        async with aiohttp.ClientSession(connector=self._connector()) as session:
            resp = await session.post(
                f"{self._base_url}/api/vms/acquire",
                json={"vcpu": vcpu, "mem_mib": mem_mib},
            )
            resp.raise_for_status()
            return await resp.json()

    async def release(self, vm_id: str, destroy: bool = True):
        """Release a VM back to the pool (optionally destroying it)."""
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

### File: `fc_provisioner/vsock_client.py`

```python
"""Host-side vsock communication with the guest agent.

Firecracker maps guest AF_VSOCK ports to a host-side Unix domain socket.
The host connects to that socket and sends "CONNECT <port>\n" to reach
the guest agent listening on that port.
"""

import asyncio
import json
import struct
from typing import Any, Dict


GUEST_AGENT_PORT = 52
HEADER_FMT = "!I"  # 4-byte big-endian unsigned int (message length)
HEADER_SIZE = struct.calcsize(HEADER_FMT)


async def vsock_send(vsock_uds_path: str, msg: Dict[str, Any]):
    """Send a JSON message to the guest agent via Firecracker's vsock UDS.
    
    Protocol:
    1. Connect to the host-side Unix domain socket
    2. Send "CONNECT <port>\n" to Firecracker
    3. Read "OK <assigned_port>\n" response
    4. Send length-prefixed JSON payload
    """
    reader, writer = await asyncio.open_unix_connection(vsock_uds_path)
    try:
        # Firecracker vsock handshake
        writer.write(f"CONNECT {GUEST_AGENT_PORT}\n".encode())
        await writer.drain()

        # Read handshake response
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line.startswith(b"OK"):
            raise ConnectionError(f"Vsock handshake failed: {line.decode().strip()}")

        # Send length-prefixed JSON
        payload = json.dumps(msg).encode()
        writer.write(struct.pack(HEADER_FMT, len(payload)))
        writer.write(payload)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def vsock_recv(vsock_uds_path: str, timeout: float = 30) -> Dict[str, Any]:
    """Send a no-op to the guest agent and read the response.
    
    Note: In practice, the send and recv happen on the same connection.
    This is a simplified version; the real implementation should keep
    the connection open within launch_process().
    """
    reader, writer = await asyncio.open_unix_connection(vsock_uds_path)
    try:
        # Firecracker vsock handshake
        writer.write(f"CONNECT {GUEST_AGENT_PORT}\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line.startswith(b"OK"):
            raise ConnectionError(f"Vsock handshake failed: {line.decode().strip()}")

        # Read length-prefixed JSON response
        header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=timeout)
        length = struct.unpack(HEADER_FMT, header)[0]
        payload = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(payload)
    finally:
        writer.close()
        await writer.wait_closed()
```

---

## 6. Component 2: Pool Manager

### File: `fc_pool_manager/manager.py`

### Responsibilities

1. Maintain a pool of `pool_size` pre-booted, idle Firecracker microVMs
2. Boot new VMs when pool drops below `replenish_threshold`
3. Assign unique TAP interfaces and IPs from a managed subnet
4. Track VM state: `BOOTING → IDLE → ASSIGNED → STOPPING → [destroyed]`
5. Enforce `max_vms` ceiling to prevent host resource exhaustion
6. Periodic health check of idle VMs; replace unhealthy ones
7. Graceful shutdown: stop all VMs on SIGTERM

### VM Lifecycle State Machine

```
  BOOTING ────→ IDLE ────→ ASSIGNED ────→ STOPPING ────→ [destroyed]
     │                │             │
     └── (fail) ──→ [destroyed]  │  └── (timeout) ──→ STOPPING
                                    └── (user stop) ─→ STOPPING
```

### Pool Manager API (Unix Domain Socket)

| Endpoint | Method | Request Body | Response |
|----------|--------|-------------|----------|
| `POST /api/vms/acquire` | Claim idle VM | `{"vcpu": 1, "mem_mib": 512}` | `{"id": "vm-abc", "ip": "172.16.0.2", "vsock_path": "/var/run/fc-vms/vm-abc/v.sock"}` |
| `POST /api/vms/{id}/release` | Release/destroy | `{"destroy": true}` | `{"ok": true}` |
| `GET /api/vms/{id}/health` | Check VM alive | — | `{"alive": true, "uptime": 3600}` |
| `GET /api/pool/status` | Pool stats | — | `{"idle": 5, "assigned": 3, "booting": 1, "max": 30}` |
| `GET /api/metrics` | Prometheus metrics | — | text/plain Prometheus exposition format |

### Firecracker API Calls Per VM Boot

The pool manager calls the Firecracker REST API (Unix socket at `/var/run/fc-vms/{id}/api.sock`) to configure and boot each VM:

```
1. PUT /machine-config          → {"vcpu_count": 1, "mem_size_mib": 512}
2. PUT /boot-source             → {"kernel_image_path": "/opt/fc/vmlinux", "boot_args": "console=ttyS0 reboot=k panic=1 pci=off init=/init"}
3. PUT /drives/rootfs           → {"drive_id": "rootfs", "path_on_host": "/var/run/fc-vms/{id}/overlay.ext4", "is_root_device": true, "is_read_only": false}
4. PUT /network-interfaces/eth0 → {"iface_id": "eth0", "host_dev_name": "tap-{id}", "guest_mac": "auto"}
5. PUT /vsock                   → {"guest_cid": 3, "uds_path": "/var/run/fc-vms/{id}/v.sock"}
6. PUT /actions                 → {"action_type": "InstanceStart"}
```

### CoW Rootfs Strategy

```bash
# For each new VM, create a copy-on-write overlay:
cp --reflink=auto /opt/firecracker/rootfs.ext4 /var/run/fc-vms/{id}/overlay.ext4

# On btrfs/XFS with reflink support, this is instant (no data copy).
# On ext4, falls back to a full copy (~250MB, ~200ms).
# Alternative: use device-mapper thin provisioning for true thin copies.
```

### IP Address Management

- Subnet: `172.16.0.0/24`
- Gateway (host): `172.16.0.1`
- VM IPs: `172.16.0.2` through `172.16.0.254` (253 max VMs)
- Allocation: simple incrementing counter with free-list recycling
- Each VM gets a unique TAP device: `tap-{vm_id_short}`

---

## 7. Component 3: Guest Agent

### File: `guest/fc_guest_agent.py`

### Overview

The guest agent is the first process (PID 1) inside each Firecracker microVM. It:

1. Sets up the network (DHCP or static IP)
2. Mounts essential filesystems (proc, sys, devtmpfs)
3. Listens on `AF_VSOCK` port 52 for commands from the host
4. Starts `ipykernel` on demand with the provided ZMQ ports and HMAC key
5. Monitors the kernel child process
6. Responds to health pings

### Protocol: Length-Prefixed JSON over AF_VSOCK

```
Wire format: [4-byte big-endian length][JSON payload]

Host → Guest messages:
  {"action": "start_kernel", "ports": {...}, "key": "..."}
  {"action": "restart_kernel", "ports": {...}, "key": "..."}
  {"action": "signal", "signum": 2}
  {"action": "ping"}

Guest → Host messages:
  {"status": "ready", "pid": 42}
  {"status": "alive", "uptime": 3600, "mem_free_mib": 312}
  {"status": "error", "error": "kernel failed to start"}
```

### Full Implementation

```python
#!/usr/bin/env python3
"""Firecracker guest agent — runs as PID 1 inside each microVM."""

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
        kernel_proc.wait(timeout=5)

    conn_file = "/tmp/kernel.json"
    write_connection_file(conn_file, ports, key)

    kernel_proc = subprocess.Popen(
        [sys.executable, "-m", "ipykernel_launcher", "-f", conn_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,  # own process group for clean kill
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

    if action == "start_kernel":
        try:
            pid = start_kernel(msg["ports"], msg.get("key", ""))
            resp = {"status": "ready", "pid": pid}
        except Exception as e:
            resp = {"status": "error", "error": str(e)}

    elif action == "restart_kernel":
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
            # Read length-prefixed message
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

            # Handle and respond
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

---

## 8. Component 4: Guest Rootfs

### File: `guest/build_rootfs.sh`

### Contents

| Layer | Contents | Size |
|-------|----------|------|
| Base OS | Alpine 3.19 (musl, busybox) | ~8 MB |
| Python | Python 3.11, pip | ~50 MB |
| Jupyter kernel | ipykernel, jupyter-client | ~40 MB |
| Data science | numpy, pandas, matplotlib, scipy, plotly, seaborn, bokeh, panel | ~200 MB |
| Guest agent | `/usr/local/bin/fc-guest-agent` | ~20 KB |
| Init | `/init` script (mount, network, exec agent) | ~1 KB |

**Total: ~300 MB ext4 image**

### Build Script

```bash
#!/bin/bash
# guest/build_rootfs.sh — Creates the guest rootfs ext4 image
set -euo pipefail

ROOTFS_DIR=$(mktemp -d)
IMAGE=${1:-/opt/firecracker/rootfs.ext4}
IMAGE_SIZE_MB=512

echo "==> Bootstrapping Alpine into $ROOTFS_DIR"
apk --root "$ROOTFS_DIR" --initdb --arch x86_64 \
    --repository https://dl-cdn.alpinelinux.org/alpine/v3.19/main \
    --repository https://dl-cdn.alpinelinux.org/alpine/v3.19/community \
    add alpine-base python3 py3-pip py3-numpy py3-scipy \
        py3-matplotlib py3-pandas dhcpcd

echo "==> Installing Python packages"
chroot "$ROOTFS_DIR" pip3 install --break-system-packages \
    ipykernel jupyter-client \
    plotly seaborn bokeh panel hvplot

echo "==> Installing guest agent"
cp guest/fc_guest_agent.py "$ROOTFS_DIR/usr/local/bin/fc-guest-agent"
chmod +x "$ROOTFS_DIR/usr/local/bin/fc-guest-agent"

echo "==> Creating /init"
cat > "$ROOTFS_DIR/init" << 'INITEOF'
#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mkdir -p /run

# Network: bring up eth0 with DHCP or static
ip link set lo up
ip link set eth0 up
# Static IP is set by Firecracker's MMDS or boot args; fallback to DHCP:
dhcpcd eth0 --waitip --timeout 10 2>/dev/null || true

# Start guest agent as PID 1's child
exec python3 /usr/local/bin/fc-guest-agent
INITEOF
chmod +x "$ROOTFS_DIR/init"

echo "==> Creating ext4 image ($IMAGE_SIZE_MB MB)"
dd if=/dev/zero of="$IMAGE" bs=1M count="$IMAGE_SIZE_MB"
mkfs.ext4 -F "$IMAGE"
MOUNT_DIR=$(mktemp -d)
mount -o loop "$IMAGE" "$MOUNT_DIR"
cp -a "$ROOTFS_DIR"/* "$MOUNT_DIR"/
umount "$MOUNT_DIR"
rmdir "$MOUNT_DIR"
rm -rf "$ROOTFS_DIR"

echo "==> Done: $IMAGE"
```

### Guest Kernel

Use the Firecracker-recommended minimal kernel:

```bash
# Download pre-built Firecracker-compatible kernel
KERNEL_VERSION=5.10.217
wget https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/kernels/vmlinux-${KERNEL_VERSION}.bin \
    -O /opt/firecracker/vmlinux
```

---

## 9. Component 5: Execution API

This is **optional** — the Kernel Gateway itself is the primary API. However, if your chatbot needs a simpler REST wrapper (e.g., `POST /execute` with JSON in, JSON out), use this thin FastAPI layer.

### File: `execution_api/server.py`

```python
"""Optional REST wrapper around the Kernel Gateway for simpler chatbot integration."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sandbox_client.client import SandboxSession
import uuid

app = FastAPI()
sessions: dict[str, SandboxSession] = {}

class ExecuteRequest(BaseModel):
    session_id: str | None = None
    code: str

class DashboardRequest(BaseModel):
    code: str
    framework: str = "panel"  # "panel" | "streamlit" | "gradio"

@app.post("/execute")
async def execute(req: ExecuteRequest):
    sid = req.session_id or uuid.uuid4().hex
    if sid not in sessions:
        sessions[sid] = SandboxSession(sid)
        await sessions[sid].start()
    result = await sessions[sid].execute(req.code)
    return {"session_id": sid, **result}

@app.post("/dashboard")
async def launch_dashboard(req: DashboardRequest):
    sid = uuid.uuid4().hex
    session = SandboxSession(sid)
    await session.start()
    sessions[sid] = session
    url = await session.launch_dashboard(req.code, req.framework)
    return {"session_id": sid, "iframe_url": url}

@app.delete("/sessions/{session_id}")
async def stop_session(session_id: str):
    if session_id in sessions:
        await sessions[session_id].stop()
        del sessions[session_id]
    return {"ok": True}
```

---

## 10. Component 6: Sandbox Client

### File: `sandbox_client/client.py`

This is the Python library your chatbot backend imports to interact with the Kernel Gateway.

```python
"""Sandbox client — manages kernel sessions and captures output via Kernel Gateway."""

import aiohttp
import asyncio
import base64
import json
import os
import uuid
from typing import Any, Dict, List, Optional

GATEWAY_URL = os.environ.get("KERNEL_GATEWAY_URL", "http://localhost:8888")
ARTIFACTS_DIR = os.environ.get("ARTIFACTS_DIR", "/var/lib/sandbox-artifacts")
ARTIFACTS_URL_PREFIX = os.environ.get("ARTIFACTS_URL_PREFIX", "/s")


class SandboxSession:
    """Manages a Jupyter kernel session for code execution.
    
    Usage:
        session = SandboxSession("user-123")
        await session.start()
        result = await session.execute("import pandas as pd; print(pd.__version__)")
        # result = {"stdout": "2.1.4\n", "stderr": "", "images": [], "html": [], "error": None}
        await session.stop()
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.kernel_id: Optional[str] = None

    async def start(self):
        """Start a new kernel via the Gateway REST API."""
        async with aiohttp.ClientSession() as s:
            # POST /api/kernels to start a kernel
            # The Gateway will use our FirecrackerProvisioner to launch it in a VM
            resp = await s.post(
                f"{GATEWAY_URL}/api/kernels",
                json={"name": "python3-firecracker"},
            )
            resp.raise_for_status()
            data = await resp.json()
            self.kernel_id = data["id"]

    async def execute(self, code: str) -> Dict[str, Any]:
        """Execute code and capture all output types.
        
        Returns:
            {
                "stdout": str,
                "stderr": str,
                "images": [{"url": "/s/session/abc.png", "type": "image/png"}],
                "html": [{"url": "/s/session/abc.html"}],
                "data": [{"type": "application/json", "content": {...}}],
                "error": {"name": "ValueError", "value": "..."} | None,
            }
        """
        if not self.kernel_id:
            raise RuntimeError("Session not started. Call start() first.")

        msg_id = uuid.uuid4().hex
        results: Dict[str, Any] = {
            "stdout": "",
            "stderr": "",
            "images": [],
            "html": [],
            "data": [],
            "error": None,
        }

        ws_url = f"ws://{GATEWAY_URL.replace('http://', '')}/api/kernels/{self.kernel_id}/channels"

        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(ws_url) as ws:
                # Send execute_request
                await ws.send_json({
                    "header": {
                        "msg_id": msg_id,
                        "username": "",
                        "session": self.session_id,
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

                # Collect responses until execution_state == "idle"
                while True:
                    raw = await asyncio.wait_for(ws.receive(), timeout=120)
                    if raw.type == aiohttp.WSMsgType.TEXT:
                        msg = json.loads(raw.data)
                    elif raw.type == aiohttp.WSMsgType.BINARY:
                        msg = json.loads(raw.data)
                    else:
                        continue

                    # Only process messages from our execution
                    parent_id = msg.get("parent_header", {}).get("msg_id")
                    if parent_id != msg_id and msg["header"]["msg_type"] != "status":
                        continue

                    msg_type = msg["header"]["msg_type"]
                    content = msg.get("content", {})

                    if msg_type == "stream":
                        name = content.get("name", "stdout")
                        results[name] += content.get("text", "")

                    elif msg_type in ("display_data", "execute_result"):
                        data = content.get("data", {})
                        self._capture_rich_output(data, results)

                    elif msg_type == "error":
                        results["error"] = {
                            "name": content.get("ename", "Error"),
                            "value": content.get("evalue", ""),
                            "traceback": content.get("traceback", []),
                        }

                    elif msg_type == "status":
                        if content.get("execution_state") == "idle":
                            break

        return results

    def _capture_rich_output(self, data: Dict[str, Any], results: Dict[str, Any]):
        """Extract and save rich output (images, HTML, JSON) from display_data."""
        artifact_dir = os.path.join(ARTIFACTS_DIR, self.session_id)
        os.makedirs(artifact_dir, exist_ok=True)

        # PNG images (matplotlib, seaborn)
        if "image/png" in data:
            img_id = uuid.uuid4().hex[:8]
            filename = f"{img_id}.png"
            filepath = os.path.join(artifact_dir, filename)
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(data["image/png"]))
            results["images"].append({
                "url": f"{ARTIFACTS_URL_PREFIX}/{self.session_id}/{filename}",
                "type": "image/png",
            })

        # SVG (some matplotlib backends, Plotly)
        if "image/svg+xml" in data:
            img_id = uuid.uuid4().hex[:8]
            filename = f"{img_id}.svg"
            filepath = os.path.join(artifact_dir, filename)
            with open(filepath, "w") as f:
                f.write(data["image/svg+xml"])
            results["images"].append({
                "url": f"{ARTIFACTS_URL_PREFIX}/{self.session_id}/{filename}",
                "type": "image/svg+xml",
            })

        # HTML (Plotly, Bokeh, Panel inline output)
        if "text/html" in data:
            html_content = data["text/html"]
            # Only save if it's substantial (not just a repr)
            if len(html_content) > 200:
                html_id = uuid.uuid4().hex[:8]
                filename = f"{html_id}.html"
                filepath = os.path.join(artifact_dir, filename)
                with open(filepath, "w") as f:
                    f.write(html_content)
                results["html"].append({
                    "url": f"{ARTIFACTS_URL_PREFIX}/{self.session_id}/{filename}",
                })

        # JSON data (Plotly figure JSON, Vega specs)
        if "application/json" in data:
            results["data"].append({
                "type": "application/json",
                "content": data["application/json"],
            })

    async def launch_dashboard(self, code: str, framework: str = "panel") -> str:
        """Write a dashboard app and return its iframe URL.
        
        The code is written to /apps/{app_id}.py on the shared volume,
        where the Panel server auto-detects and serves it.
        """
        app_id = uuid.uuid4().hex[:8]
        filename = f"dash_{app_id}.py"

        # Execute code that writes the dashboard app file
        escaped_code = code.replace('"""', '\\"\\"\\"')
        wrapper = f'''
import os
os.makedirs("/apps", exist_ok=True)
with open("/apps/{filename}", "w") as f:
    f.write("""{escaped_code}""")
print("APP_WRITTEN:{filename}")
'''
        result = await self.execute(wrapper)
        if result["error"]:
            raise RuntimeError(f"Failed to write dashboard: {result['error']}")

        return f"/dash/dash_{app_id}"

    async def stop(self):
        """Shut down the kernel (destroys the Firecracker VM)."""
        if self.kernel_id:
            try:
                async with aiohttp.ClientSession() as s:
                    await s.delete(f"{GATEWAY_URL}/api/kernels/{self.kernel_id}")
            except Exception:
                pass  # Gateway may already have cleaned up
            self.kernel_id = None
```

---

## 11. Component 7: Dashboard Serving

### Caddy Reverse Proxy

```
# config/Caddyfile
{
    auto_https off  # or configure your domain for auto-TLS
}

:8080 {
    # Kernel Gateway API (WebSocket + REST) — used by sandbox client
    handle /api/kernels/* {
        reverse_proxy localhost:8888
    }

    # Static artifacts (PNG, SVG, HTML files from executions)
    handle /s/* {
        uri strip_prefix /s
        root * /var/lib/sandbox-artifacts
        file_server
        header Cache-Control "public, max-age=3600"
        header X-Content-Type-Options "nosniff"
    }

    # Live dashboards (Panel/Bokeh server)
    handle /dash/* {
        reverse_proxy localhost:5006
    }

    # Health check
    handle /health {
        respond "ok" 200
    }
}
```

### Panel Server Launch

```bash
# Run Panel serve watching the /apps/ directory
panel serve /var/lib/sandbox-apps/ \
    --address 0.0.0.0 \
    --port 5006 \
    --allow-websocket-origin yourhost.com \
    --allow-websocket-origin localhost:8080 \
    --prefix /dash \
    --warm \
    --num-procs 2 \
    --check-unused-sessions-milliseconds 30000 \
    --unused-session-lifetime-milliseconds 7200000
```

---

## 12. Component 8: Networking

### File: `config/setup_network.sh`

```bash
#!/bin/bash
# Run once on host boot to set up the Firecracker network bridge.
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

### TAP Creation (called by pool manager per VM)

```bash
# create_tap.sh — called by pool manager for each new VM
TAP_NAME=$1  # e.g., "tap-vm-abc123"
BRIDGE=fcbr0
VM_IP=$2     # e.g., "172.16.0.2"
VM_MAC=$3    # e.g., "AA:FC:00:00:00:02"

ip tuntap add $TAP_NAME mode tap
ip link set $TAP_NAME master $BRIDGE
ip link set $TAP_NAME up
```

### Static IP Assignment Inside VM

The guest agent's `/init` script or boot args can set the IP statically:

```bash
# In boot_args for Firecracker:
"console=ttyS0 reboot=k panic=1 pci=off ip=172.16.0.2::172.16.0.1:255.255.255.0::eth0:off init=/init"
```

This uses the kernel's built-in IP configuration (no DHCP needed).

---

## 13. Configuration Files

### File: `config/kernel.json`

Install to `/usr/share/jupyter/kernels/python3-firecracker/kernel.json`:

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
                "mem_size_mib": 512,
                "rootfs_path": "/opt/firecracker/rootfs.ext4",
                "kernel_path": "/opt/firecracker/vmlinux"
            }
        }
    }
}
```

### File: `config/fc-pool.yaml`

```yaml
pool:
  size: 5                         # pre-warmed idle VMs
  max_vms: 30                     # hard ceiling
  replenish_threshold: 2          # boot when idle < 2
  health_check_interval: 30       # seconds

vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/firecracker/vmlinux
  rootfs: /opt/firecracker/rootfs.ext4
  boot_args: "console=ttyS0 reboot=k panic=1 pci=off init=/init"

network:
  bridge: fcbr0
  subnet: "172.16.0.0/24"
  gateway: "172.16.0.1"
  vm_ip_start: 2                  # first VM gets .2

jailer:
  enabled: true
  chroot_base: /srv/jailer
  exec_path: /usr/bin/firecracker
  uid: 123
  gid: 100

artifacts:
  base_dir: /var/lib/sandbox-artifacts
  ttl_hours: 24                   # cleanup old artifacts
  
dashboard:
  apps_dir: /var/lib/sandbox-apps
  panel_port: 5006
```

### File: `config/fc-pool-manager.service`

```ini
[Unit]
Description=Firecracker VM Pool Manager
After=network.target

[Service]
Type=simple
ExecStartPre=/bin/bash /opt/fc-kernel-provisioner/config/setup_network.sh
ExecStart=/usr/bin/python3 -m fc_pool_manager.server --config /etc/fc-pool/config.yaml
Restart=on-failure
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Kernel Gateway Launch

```bash
jupyter kernelgateway \
    --KernelGatewayApp.ip=0.0.0.0 \
    --KernelGatewayApp.port=8888 \
    --KernelGatewayApp.allow_origin='*' \
    --KernelGatewayApp.default_kernel_name=python3-firecracker \
    --KernelGatewayApp.max_kernels=25 \
    --KernelGatewayApp.prespawn_count=2 \
    --KernelGatewayApp.cull_idle_timeout=600 \
    --KernelGatewayApp.cull_interval=60
```

---

## 14. LLM Tool Definitions

These are the tool definitions your chatbot passes to the Claude API for function calling:

```json
{
    "tools": [
        {
            "name": "execute_python",
            "description": "Execute Python code in an isolated sandbox. Use for data analysis, chart generation, computations, and file processing. Code runs in a fresh environment with numpy, pandas, matplotlib, plotly, seaborn, scipy pre-installed. Save charts to /output/ to return them as images. Use plt.savefig('/output/chart.png') for matplotlib. Use fig.write_html('/output/chart.html') for interactive Plotly charts.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute"
                    }
                },
                "required": ["code"]
            }
        },
        {
            "name": "launch_dashboard",
            "description": "Create and deploy an interactive dashboard web app. Use when the user needs a persistent interactive experience (filters, dropdowns, real-time updates) rather than a static chart. The code should be a complete Panel app using pn.serve or a Streamlit app.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Complete Panel or Streamlit app code"
                    },
                    "framework": {
                        "type": "string",
                        "enum": ["panel", "streamlit", "gradio"],
                        "description": "Dashboard framework to use"
                    }
                },
                "required": ["code"]
            }
        }
    ]
}
```

### System Prompt Guidance for the LLM

```
When the user asks a question that requires data analysis, computation, or visualization:

1. Use `execute_python` for:
   - Data analysis and statistics
   - Static charts (matplotlib: plt.savefig('/output/chart.png'))
   - Interactive HTML charts (plotly: fig.write_html('/output/chart.html'))
   - File processing, data transformation
   - One-shot computations

2. Use `launch_dashboard` for:
   - Interactive dashboards with user controls (dropdowns, sliders, date pickers)
   - Real-time data exploration with filtering
   - Multi-view linked visualizations
   - Apps that need persistent server-side state

Output conventions:
- matplotlib/seaborn: always call plt.savefig('/output/chart.png', dpi=150, bbox_inches='tight')
- plotly: use fig.write_html('/output/chart.html', include_plotlyjs='cdn') for interactive, or fig.write_image('/output/chart.png') for static
- print() output is captured as text and returned to the user
- Multiple outputs per execution are supported
```

---

## 15. Output Types and Capture Logic

| Output Type | How LLM Generates It | How It's Captured | How Chatbot Embeds It |
|-------------|---------------------|-------------------|----------------------|
| Text (stdout) | `print(...)` | `stream` message on WebSocket | Inline text in chat |
| matplotlib PNG | `plt.savefig('/output/chart.png')` | `display_data` with `image/png` base64 | `<img src="/s/{session}/{id}.png">` |
| Plotly interactive | `fig.write_html('/output/viz.html')` or `fig.show()` | `display_data` with `text/html` or file on disk | `<iframe src="/s/{session}/{id}.html">` |
| Plotly static | `fig.write_image('/output/chart.png')` | File on disk in `/output/` | `<img src="/s/{session}/{id}.png">` |
| Seaborn/mpl SVG | `plt.savefig('/output/chart.svg')` | `display_data` with `image/svg+xml` | Inline SVG or `<img>` |
| Panel dashboard | Writes `.py` file to `/apps/` | Panel serve auto-detects | `<iframe src="/dash/dash_{id}/">` |
| Bokeh inline | `show(fig)` in kernel | `display_data` with `text/html` (Bokeh JS) | `<iframe src="/s/{session}/{id}.html">` |
| JSON data | `display(JSON(data))` | `display_data` with `application/json` | Rendered by chatbot UI |
| Error | Code raises exception | `error` message on WebSocket | Shown as error in chat |

---

## 16. Security Model

### Isolation Layers

| Layer | Mechanism | Protects Against |
|-------|-----------|-----------------|
| 1. KVM virtualization | Firecracker VMM | Kernel exploits, syscall attacks |
| 2. Jailer | cgroups + namespaces + seccomp per VMM process | VMM escape, resource abuse |
| 3. Network isolation | ebtables block VM-to-VM; iptables per-VM rate limits | Lateral movement, network DoS |
| 4. Ephemeral rootfs | Read-only base + CoW overlay destroyed on stop | Persistent compromise |
| 5. Resource limits | Firecracker: vCPU cap, memory cap, I/O rate limiters | Resource exhaustion |
| 6. ZMQ HMAC auth | Kernel connection key (random per session) | Unauthorized kernel access |
| 7. Artifact TTL | Cron cleanup after 24h | Data accumulation |

### Network Rules

```
VMs can reach:
  ✅ Host gateway (172.16.0.1) — for JupyterHub API callbacks
  ✅ External internet — for pip install, API calls (optional; disable with iptables)

VMs cannot reach:
  ❌ Other VMs (ebtables DROP on bridge)
  ❌ Host services on non-gateway ports (iptables whitelist)
```

---

## 17. Resource Budget

### Host Allocation (8-core / 16 GB)

| Component | CPU | RAM | Disk |
|-----------|-----|-----|------|
| Host OS + systemd | 0.5 cores | 1 GB | — |
| Kernel Gateway + Caddy | 0.5 cores | 512 MB | ~100 MB |
| Pool Manager | 0.25 cores | 256 MB | ~10 MB |
| Panel serve | 0.5 cores | 512 MB | — |
| **Available for microVMs** | **~6.25 cores** | **~13.75 GB** | — |

### VM Density Scenarios

| Profile | vCPU/VM | RAM/VM | Max Concurrent | Use Case |
|---------|---------|--------|----------------|----------|
| Light | 0.5 | 256 MB | ~50 | Simple analysis, small datasets |
| Standard | 1 | 512 MB | ~25 | Data science, charting |
| Heavy | 2 | 1 GB | ~13 | ML training, large DataFrames |

---

## 18. Testing Plan

### Unit Tests

| Test | What It Validates |
|------|-------------------|
| `test_provisioner.py::test_pre_launch` | VM acquired from pool, connection info prepared |
| `test_provisioner.py::test_launch_process` | Vsock send/recv, connection info overridden to VM IP |
| `test_provisioner.py::test_poll_alive` | Returns None when VM + kernel alive |
| `test_provisioner.py::test_poll_dead` | Returns exit code when VM dead |
| `test_provisioner.py::test_cleanup` | VM released/destroyed, state cleared |
| `test_pool_manager.py::test_acquire_release` | VM claimed, returned, pool replenished |
| `test_pool_manager.py::test_pool_replenish` | New VMs booted when idle < threshold |
| `test_pool_manager.py::test_max_vms_enforced` | Acquire fails gracefully at ceiling |
| `test_guest_agent.py::test_start_kernel` | ipykernel starts, ready response sent |
| `test_guest_agent.py::test_signal_forwarding` | SIGINT forwarded to kernel process |
| `test_sandbox_client.py::test_execute_stdout` | stdout captured from print() |
| `test_sandbox_client.py::test_execute_image` | PNG artifact saved and URL returned |
| `test_sandbox_client.py::test_execute_error` | Error name/value captured |

### Integration Test

```python
# test_integration.py — end-to-end smoke test
async def test_full_pipeline():
    """Code in → Firecracker VM → artifacts out."""
    session = SandboxSession("test-e2e")
    await session.start()

    # 1. Simple stdout
    r = await session.execute("print('hello')")
    assert r["stdout"].strip() == "hello"
    assert r["error"] is None

    # 2. Matplotlib chart
    r = await session.execute("""
import matplotlib.pyplot as plt
plt.figure(figsize=(6,4))
plt.plot([1,2,3], [1,4,9])
plt.savefig('/output/test.png')
plt.show()
""")
    assert len(r["images"]) >= 1
    assert r["images"][0]["url"].endswith(".png")

    # 3. Plotly interactive HTML
    r = await session.execute("""
import plotly.express as px
fig = px.scatter(x=[1,2,3], y=[1,4,9])
fig.write_html('/output/scatter.html')
""")
    # Check artifact file exists
    assert len(r["html"]) >= 1 or r["stdout"].strip() != ""

    # 4. Error handling
    r = await session.execute("1/0")
    assert r["error"] is not None
    assert r["error"]["name"] == "ZeroDivisionError"

    # 5. State persistence across cells (same kernel)
    await session.execute("x = 42")
    r = await session.execute("print(x)")
    assert r["stdout"].strip() == "42"

    await session.stop()
```

---

## 19. Build Order

| Phase | Milestone | Deliverable | Estimated Effort | Dependencies |
|-------|-----------|-------------|-----------------|--------------|
| **0** | **Dev environment** | Install Firecracker, verify KVM, build kernel | 0.5 days | KVM-enabled host |
| **1** | **Guest rootfs + kernel** | Bootable ext4 with Python + ipykernel | 1–2 days | Phase 0 |
| **2** | **Guest agent** | PID 1 agent with vsock listener + kernel launcher | 1–2 days | Phase 1 |
| **3** | **Pool manager** | Asyncio daemon: boot/destroy VMs, TAP networking | 2–3 days | Phase 1, 2 |
| **4** | **FirecrackerProvisioner** | Kernel provisioner plugin, entry points, kernel.json | 2–3 days | Phase 3 |
| **5** | **Sandbox client** | WebSocket client, output capture, artifact saving | 1–2 days | Phase 4 |
| **6** | **Dashboard serving** | Caddy + Panel serve config, iframe URL generation | 1 day | Phase 5 |
| **7** | **Networking + security** | Bridge setup, iptables, jailer, VM-to-VM isolation | 1–2 days | Phase 3 |
| **8** | **Integration testing** | End-to-end: chatbot → Gateway → VM → artifacts | 1–2 days | All above |
| **9** | **Snapshot optimization** | VM snapshotting for sub-50ms cold starts (optional) | 2–3 days | Phase 4 |

**Total: 11–20 days** for phases 0–8. Phase 9 is a stretch goal.

---

## 20. Migration Path

The entire system is designed so you can start simple and upgrade isolation without rewriting anything upstream:

### Tier 1: Local Kernels (start here for development)

```json
// kernel.json — use default local provisioner
{
    "display_name": "Python 3",
    "language": "python",
    "argv": ["python3", "-m", "ipykernel_launcher", "-f", "{connection_file}"]
}
```
- **Change required**: Nothing. Default Jupyter behavior.
- **Isolation**: Process-level only.
- **Use for**: Development, testing the chatbot pipeline.

### Tier 2: Docker Containers (intermediate)

```json
// kernel.json — use existing DockerProvisioner from gateway_provisioners
{
    "display_name": "Python 3 (Docker)",
    "language": "python",
    "argv": ["python3", "-m", "ipykernel_launcher", "-f", "{connection_file}"],
    "metadata": {
        "kernel_provisioner": {
            "provisioner_name": "docker-provisioner",
            "config": {
                "image_name": "my-kernel-image:latest"
            }
        }
    }
}
```
- **Change required**: Install `gateway-provisioners` package, update kernel.json.
- **Isolation**: Container-level (shared kernel).
- **Use for**: Internal tools, semi-trusted users.

### Tier 3: Firecracker MicroVMs (production)

```json
// kernel.json — this spec
{
    "display_name": "Python 3 (Firecracker)",
    "language": "python",
    "argv": [],
    "metadata": {
        "kernel_provisioner": {
            "provisioner_name": "firecracker-provisioner",
            "config": { ... }
        }
    }
}
```
- **Change required**: Install `fc-kernel-provisioner`, deploy pool manager, build rootfs.
- **Isolation**: VM-level (KVM hardware boundary).
- **Use for**: Untrusted code, public-facing, multi-tenant.

### What stays the same across all tiers

- Chatbot backend code
- LLM tool definitions
- Sandbox client (WebSocket to Gateway)
- Output capture logic
- Dashboard serving (Caddy + Panel)
- Chatbot UI (iframe embedding)
- Artifact store

### What changes per tier

- `kernel.json` (one file)
- Infrastructure underneath (nothing → Docker → Firecracker + pool manager)

---

## Appendix: Key External References

| Resource | URL | Use |
|----------|-----|-----|
| Jupyter Kernel Provisioner API | https://jupyter-client.readthedocs.io/en/latest/provisioning.html | Provisioner base class and protocol |
| Gateway Provisioners (Docker, K8s) | https://github.com/jupyter-server/gateway_provisioners | Reference implementations for Docker/K8s provisioners |
| Firecracker API spec | https://github.com/firecracker-microvm/firecracker/blob/main/src/api_server/swagger/firecracker.yaml | REST API for VM configuration |
| Firecracker vsock docs | https://github.com/firecracker-microvm/firecracker/blob/main/docs/vsock.md | Vsock host-guest communication protocol |
| Firecracker getting started | https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md | Installation and first VM boot |
| Kernel Gateway docs | https://jupyter-kernel-gateway.readthedocs.io/en/latest/ | Gateway configuration and API |
| ForgeVM (reference impl) | https://github.com/DohaerisAI/forgevm | Pool manager and guest agent patterns |
| E2B (reference architecture) | https://github.com/e2b-dev/E2B | Firecracker + Jupyter in production |