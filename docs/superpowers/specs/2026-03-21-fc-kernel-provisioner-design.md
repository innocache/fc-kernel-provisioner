# Firecracker Kernel Provisioner — Design Specification

> **Date**: 2026-03-21
> **Status**: Approved
> **Approach**: Monorepo, separate processes, core-slice-first delivery

---

## 1. Overview

A system that lets an LLM-powered chatbot execute Python code inside Firecracker microVM sandboxes and return output (stdout, images, HTML, dashboards) back to users. Built as a Jupyter kernel provisioner plugin so the Kernel Gateway can launch kernels inside Firecracker VMs instead of local processes.

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Build strategy | Core slice first | Validates riskiest integration points (vsock, ZMQ-over-TAP, provisioner wiring) early with minimal code |
| Architecture | Separate processes, Unix socket IPC | Clean process boundaries for debugging, pool manager restarts independently, Unix socket promotable to TCP for multi-host |
| Jailer | From day one | Avoids rearchitecting later; cgroups/namespaces/seccomp from the start |
| Dashboards | Deferred | Not on critical path; layers on cleanly after execution pipeline works |
| Networking | Single host/subnet, flexible interfaces | `/24` subnet sufficient now; IP allocator behind an interface for future multi-host |
| Artifact storage | Interface designed, local impl deferred | Core slice is stdout-only; storage interface enables S3 swap later |

### Core Slice Definition

The core slice delivers one thing: **execute Python code inside a jailed Firecracker microVM and return stdout to the host**. No images, no HTML, no dashboards — just `print('hello')` → `"hello"`.

---

## 2. Build Sequence

| Phase | Deliverable | Depends On |
|-------|-------------|------------|
| 1 | Guest rootfs + init + guest agent — bootable ext4 with Python, ipykernel, vsock agent | — |
| 2 | Networking — host bridge, TAP creation logic, verify host can ping a manually-booted VM | Phase 1 |
| 3 | Pool manager — asyncio daemon with jailer integration, VM lifecycle, Unix socket API | Phase 1, 2 |
| 4 | Firecracker provisioner — Jupyter plugin, claims VMs, starts kernels via vsock | Phase 3 |
| 5 | Integration smoke test — `print('hello')` through the full Firecracker path | Phase 1–4 |

### Explicitly Deferred

- Sandbox client library (use raw WebSocket for smoke test)
- Output capture (images, HTML, JSON artifacts)
- Dashboard serving (Panel + Caddy)
- Artifact storage implementation
- Snapshot optimization (sub-50ms cold starts)
- Execution API (FastAPI wrapper)
- Prometheus metrics
- VM timeout/auto-cull

---

## 3. Guest Layer

### Rootfs Image

Minimal Alpine 3.19 ext4 (512 MB), built by `guest/build_rootfs.sh`:

| Layer | Contents | Purpose |
|-------|----------|---------|
| Base | Alpine 3.19 (musl, busybox) | Minimal OS |
| Python | Python 3.11, pip | Runtime |
| Jupyter | ipykernel, jupyter-client | Kernel protocol |
| Data science | numpy, pandas, matplotlib, scipy, plotly, seaborn | Baked in now to avoid rootfs rebuild later |
| Guest agent | `/usr/local/bin/fc-guest-agent` | VM control |
| Init | `/init` shell script | Boot sequence |

### Init Script (`/init`)

Runs as PID 1:

1. Mount `/proc`, `/sys`, `/dev`
2. Bring up `lo` and `eth0` (static IP via kernel boot args — no DHCP)
3. `exec` the guest agent

Static IP is set via kernel boot args: `ip=172.16.0.X::172.16.0.1:255.255.255.0::eth0:off`. This eliminates DHCP dependency and saves ~2s boot time. The pool manager controls IP assignment and passes it in `boot_args`.

### Guest Agent (`fc_guest_agent.py`)

Listens on `AF_VSOCK` port 52. Synchronous, single-threaded (one connection at a time per VM).

**Protocol**: Length-prefixed JSON over AF_VSOCK.

```
Wire format: [4-byte big-endian length][JSON payload]
```

**Commands**:

| Command | Request | Response |
|---------|---------|----------|
| `start_kernel` | `{action, ports: {shell:5555, iopub:5556, stdin:5557, control:5558, hb:5559}, key}` | `{status: "ready", pid: N}` or `{status: "error", error: "..."}` |
| `restart_kernel` | Same as `start_kernel` | Same as `start_kernel` |
| `signal` | `{action, signum: N}` | `{status: "ok"}` or `{status: "error", error: "no kernel running"}` |
| `ping` | `{action: "ping"}` | `{status: "alive", uptime: N, kernel_alive: bool, mem_free_mib: N}` |

**`start_kernel` behavior**:

1. Kill existing kernel process if any
2. Write connection file to `/tmp/kernel.json` with provided ports, key, `ip=0.0.0.0`, `transport=tcp`
3. Spawn `python3 -m ipykernel_launcher -f /tmp/kernel.json` in its own process group (`preexec_fn=os.setsid`)
4. Wait 0.5s to confirm it didn't crash
5. Respond with `{"status": "ready", "pid": N}`

**`signal` behavior**: Forward signal to kernel's process group via `os.killpg(os.getpgid(pid), signum)`.

### Kernel Binary

Firecracker's pre-built minimal kernel (5.10.x), downloaded once and shared read-only across all VMs via hard-link into each jail.

---

## 4. Networking

### Host Bridge (run once at boot)

- Create bridge `fcbr0` with IP `172.16.0.1/24`
- Enable IP forwarding (`net.ipv4.ip_forward=1`)
- NAT outbound VM traffic via `MASQUERADE` on the default host interface
- Block VM-to-VM traffic with `ebtables DROP` on `tap-+` → `tap-+` across the bridge

### Per-VM TAP (managed by pool manager)

- Create TAP device `tap-{vm_id_short}` (name truncated to 15 chars for `IFNAMSIZ`)
- Attach to `fcbr0`, bring up
- No IP on the TAP — VM gets its IP via kernel boot args, host routes through bridge
- On teardown: `ip link del tap-{vm_id_short}` (auto-detaches from bridge)

### IP Address Management

- Subnet: `172.16.0.0/24`, gateway `172.16.0.1`
- VM IPs: `.2` through `.254` (253 slots)
- Allocation: set-based free list in pool manager. Acquire pops, release returns. No persistence — state rebuilt on restart by scanning running VMs.
- Behind an interface (`IPAllocator`) for future multi-host (range-per-host or external IPAM).

### Isolation Rules

| Rule | Mechanism | Effect |
|------|-----------|--------|
| VM ↔ VM blocked | ebtables DROP tap-+ to tap-+ | No lateral movement |
| VM → internet allowed | NAT MASQUERADE | pip install, API calls |
| VM → host services | iptables whitelist (future) | Only gateway port reachable |

Per-VM rate limiting and host service whitelisting are deferred hardening steps.

---

## 5. Pool Manager

### Process Model

Standalone asyncio daemon. Runs as systemd service (`fc-pool-manager.service`). Exposes Unix domain socket HTTP API at `/var/run/fc-pool.sock` via `aiohttp`.

### VM Lifecycle State Machine

```
BOOTING ──→ IDLE ──→ ASSIGNED ──→ STOPPING ──→ [destroyed]
   │                      │
   └── (fail) → [destroyed]  └── (timeout/release) → STOPPING
```

### Responsibilities

1. **Pool maintenance** — Keep `pool_size` (default 5) idle VMs pre-warmed. When idle count drops below `replenish_threshold` (default 2), boot new VMs in background up to `max_vms` ceiling (default 30).

2. **VM boot sequence** — For each new VM:
   - Allocate IP from free list, generate VM ID (`vm-{uuid_hex[:8]}`)
   - Create CoW rootfs overlay (`cp --reflink=auto base.ext4 → overlay.ext4`)
   - Create TAP device, attach to bridge
   - Set up jailed directory structure (hard-link kernel, rootfs)
   - Invoke `jailer` binary with chroot base, exec path, UID/GID, VM ID
   - Configure via Firecracker REST API on jailed Unix socket: machine config, boot source (static IP in boot args), rootfs drive, network interface, vsock
   - `PUT /actions` → `InstanceStart`
   - Wait for guest agent ping over vsock (timeout 30s) → transition to `IDLE`

3. **Jailer integration** — Each VM gets a jailed directory:
   ```
   /srv/jailer/firecracker/{vm_id}/root/
   ├── overlay.ext4      (CoW rootfs)
   ├── vmlinux           (hard-linked from shared kernel)
   └── run/              (API socket, vsock UDS)
   ```
   Jailer creates cgroup, user/pid/mount namespaces, seccomp filter.

4. **VM teardown** — On release with `destroy=true`:
   - Kill jailer process (cascades to Firecracker VMM)
   - Delete TAP device
   - Remove jailed directory
   - Return IP to free list

5. **Health checking** — Background task every 30s: ping each `IDLE` VM over vsock, replace unresponsive ones.

6. **Graceful shutdown** — On SIGTERM: stop all VMs, clean up TAPs, remove socket file.

### Unix Socket API

| Endpoint | Method | Request | Response |
|----------|--------|---------|----------|
| `/api/vms/acquire` | POST | `{vcpu, mem_mib}` | `{id, ip, vsock_path}` |
| `/api/vms/{id}/release` | POST | `{destroy: bool}` | `{ok: true}` |
| `/api/vms/{id}/health` | GET | — | `{alive, uptime}` |
| `/api/pool/status` | GET | — | `{idle, assigned, booting, max}` |

### Internal Abstractions

- **`VMInstance`** — Per-VM state: id, ip, tap name, jail path, vsock path, state, jailer process handle
- **`FirecrackerAPI`** — Async REST client for the per-VM jailed Unix socket
- **`NetworkManager`** — TAP creation/teardown, IP allocation. IP allocator behind an `IPAllocator` interface.
- **`Config`** — YAML loader for `fc-pool.yaml`

---

## 6. Firecracker Provisioner

### Package & Registration

Package `fc_provisioner`, registered as a Jupyter kernel provisioner via entry point:

```toml
[project.entry-points."jupyter_client.kernel_provisioners"]
firecracker-provisioner = "fc_provisioner:FirecrackerProvisioner"
```

Activated by kernelspec at `/usr/share/jupyter/kernels/python3-firecracker/kernel.json`:

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

### Class: `FirecrackerProvisioner(KernelProvisionerBase)`

Implements the Jupyter kernel provisioner interface:

1. **`pre_launch(**kwargs)`** — Read config from kernelspec metadata. Create `PoolClient`. Call `acquire()` to claim an idle VM (`{id, ip, vsock_path}`). Clear `cmd` (no `Popen`).

2. **`launch_process(cmd, **kwargs)`** — Send `start_kernel` to guest agent over vsock with ZMQ ports (5555–5559) and HMAC key from `connection_info`. Wait for `{"status": "ready"}` (timeout 30s). Override `connection_info` to point at VM's TAP IP and fixed ports. Return `FirecrackerProcess` handle.

3. **`poll()`** — Delegate to `FirecrackerProcess.poll()`, which calls `pool_client.is_alive(vm_id)`. Returns `None` if alive, exit code if dead.

4. **`send_signal(signum)`** — Forward signal to guest agent over vsock.

5. **`cleanup(restart=False)`** — If restarting: send `restart_kernel` over vsock (same VM, new kernel process). If stopping: call `pool_client.release(vm_id, destroy=True)`, clear state.

6. **`get_provisioner_info()` / `load_provisioner_info()`** — Persist/restore `vm_id`, `vm_ip`, `vsock_path` for Gateway restart resilience.

### Supporting Classes

- **`FirecrackerProcess`** — Process-like handle for `KernelManager`. Implements `poll()`, `kill()`, `terminate()`, `send_signal()`. Delegates to pool client and vsock.

- **`PoolClient`** — Async HTTP client using `aiohttp.UnixConnector`. Methods: `acquire()`, `release()`, `is_alive()`.

- **`vsock_client`** — `vsock_send(path, msg)` and `vsock_recv(path, timeout)`. Handle Firecracker vsock UDS handshake (`CONNECT <port>\n` → `OK <port>\n`), then send/receive length-prefixed JSON. Connection opened and closed per call in the core slice.

### Key Design Point

The provisioner does NOT modify ipykernel or the Jupyter protocol. It only changes *where* the kernel runs and *how* ZMQ connects to it. From the Gateway's perspective, it's talking to a regular ipykernel over TCP.

---

## 7. Artifact Storage Interface

Deferred from core slice. Interface designed for future S3 swappability.

### Interface: `ArtifactStore`

```
save(session_id, filename, data, content_type) → url
get(session_id, filename) → bytes
delete_session(session_id) → None
```

### Implementations

- **`LocalArtifactStore`** — Writes to `{base_dir}/{session_id}/{filename}`. Returns URL `/{url_prefix}/{session_id}/{filename}`. Caddy serves directory. Background TTL cleanup (walks directory, deletes sessions older than `ttl_hours`).

- **Future `S3ArtifactStore`** — Writes to `s3://{bucket}/{session_id}/{filename}`. Returns presigned or CloudFront URLs.

### Integration Point

`SandboxClient._capture_rich_output()` receives an `ArtifactStore` instance rather than writing to disk directly. Configured once at startup. No rework when switching implementations.

---

## 8. Testing

### Integration Smoke Test (Core Slice Acceptance)

Single async test proving the full path:

1. Assert pool manager is running
2. Assert Kernel Gateway is running with `python3-firecracker` kernelspec
3. Open WebSocket to `ws://localhost:8888/api/kernels/{id}/channels`
4. Execute `print('hello')` — assert stdout contains `"hello"`
5. Execute `x = 42` then `print(x)` — assert state persists across cells
6. Delete kernel — assert VM cleaned up (TAP gone, jail dir removed)

Raw WebSocket test — no `SandboxClient`.

### Unit Tests (mocked, no VMs)

| Test | Validates |
|------|-----------|
| `test_provisioner::pre_launch` | Pool client called, connection info prepared |
| `test_provisioner::launch_process` | Vsock send with correct ports/key, connection info overridden |
| `test_provisioner::cleanup` | Release called, state cleared |
| `test_pool_manager::acquire_release` | State transitions IDLE → ASSIGNED → destroyed |
| `test_pool_manager::replenish` | New VMs booted when idle < threshold |
| `test_pool_manager::max_vms` | Acquire returns error at ceiling |
| `test_guest_agent::start_kernel` | Connection file written, subprocess spawned, ready response |
| `test_guest_agent::signal` | Signal forwarded to process group |

### Dev Workflow

1. Build rootfs once: `guest/build_rootfs.sh`
2. Download Firecracker kernel once
3. Run `config/setup_network.sh` once per host boot
4. Start pool manager: `python -m fc_pool_manager.server --config config/fc-pool.yaml`
5. Start Kernel Gateway: `jupyter kernelgateway --default_kernel_name=python3-firecracker`
6. Run smoke test: `pytest tests/test_integration.py -v`

---

## 9. Project Structure

```
fc-kernel-provisioner/
├── fc_provisioner/                  # Kernel Provisioner plugin
│   ├── __init__.py
│   ├── provisioner.py               # FirecrackerProvisioner class
│   ├── pool_client.py               # Async client for pool manager
│   └── vsock_client.py              # Vsock send/recv helpers
│
├── fc_pool_manager/                 # Pool Manager daemon
│   ├── __init__.py
│   ├── manager.py                   # PoolManager class
│   ├── vm.py                        # VMInstance class
│   ├── firecracker_api.py           # Firecracker REST client
│   ├── network.py                   # TAP + IP management
│   ├── config.py                    # YAML config loader
│   └── server.py                    # Unix socket API server
│
├── guest/                           # Guest Agent + Rootfs
│   ├── fc_guest_agent.py            # Guest agent (PID 1 child)
│   ├── init.sh                      # /init script
│   └── build_rootfs.sh              # Rootfs build script
│
├── sandbox_client/                  # Chatbot client (deferred)
│   ├── __init__.py
│   ├── client.py                    # SandboxSession class
│   ├── output.py                    # Output capture
│   └── artifact_store.py            # ArtifactStore interface + LocalArtifactStore
│
├── config/
│   ├── kernel.json                  # Kernelspec
│   ├── fc-pool.yaml                 # Pool manager config
│   ├── Caddyfile                    # Reverse proxy (deferred)
│   ├── setup_network.sh             # Host bridge setup
│   └── fc-pool-manager.service      # systemd unit
│
├── tests/
│   ├── test_provisioner.py
│   ├── test_pool_manager.py
│   ├── test_guest_agent.py
│   └── test_integration.py          # End-to-end smoke test
│
├── pyproject.toml
└── README.md
```

---

## 10. Follow-on Phases (Post Core Slice)

After the core slice is validated:

1. **Sandbox client + output capture** — `SandboxSession` class, `_capture_rich_output()`, `LocalArtifactStore` implementation
2. **Dashboard serving** — Panel serve + Caddy config, `launch_dashboard` tool
3. **Execution API** — Optional FastAPI wrapper
4. **Network hardening** — Per-VM rate limiting, host service whitelisting
5. **Snapshot optimization** — VM snapshots for sub-50ms cold starts
6. **Prometheus metrics** — Pool manager metrics endpoint
7. **VM auto-cull** — Timeout idle assigned VMs
