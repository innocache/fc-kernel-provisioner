# Per-VM Kernel Gateway — Design Spec

## Problem

The central Kernel Gateway (KG) on the host is the source of two critical issues:

1. **Staleness (#41)**: KG maintains persistent WebSocket-ZMQ connections to kernels
   inside VMs. After snapshot restore, the kernel is alive but the KG connection is
   stale. KG's heartbeat fails, sessions return 503. Every testing session requires
   a pool restart.

2. **Scaling (#36)**: A single KG instance on one host manages all kernels. Cannot
   scale beyond one host without clustering KG (which it doesn't support).

Both stem from the same root cause: KG assumes long-lived persistent connections to
kernels, but the warm pool model creates, snapshots, restores, and destroys VMs
constantly.

## Solution

Move KG from the host into each VM. Each VM runs its own KG instance that owns
one kernel. The Execution API connects directly to the VM's KG via WebSocket.

```
BEFORE (central KG):

  Execution API --WebSocket--> Central KG (host:8888) --ZMQ--> kernel (VM)
                                    |
                                    +-- kernel (VM)
                                    +-- kernel (VM)


AFTER (per-VM KG):

  Execution API --WebSocket--> KG (VM1:8888) --ZMQ--> kernel (VM1)
                --WebSocket--> KG (VM2:8888) --ZMQ--> kernel (VM2)
                --WebSocket--> KG (VM3:8888) --ZMQ--> kernel (VM3)
```

## Why Per-VM KG (Not Direct ZMQ)

| Criteria | Direct ZMQ | Per-VM KG |
|----------|-----------|-----------|
| SandboxSession changes | Rewrite (new protocol) | Minimal (discover kernel, change URL) |
| OutputParser changes | None | None |
| New dependencies | pyzmq, jupyter_client in Execution API | KG in VM rootfs |
| Risk | High — new protocol, new failure modes | Low — same protocol, just moved |
| Implementation effort | Large (1-2 weeks) | Medium (3-5 days) |

## Current Flow

```
1. Execution API POST /sessions
2.   -> SandboxSession.start()
3.     -> HTTP POST http://host:8888/api/kernels  (central KG)
4.       -> KG triggers WarmPoolProvisioner
5.         -> Provisioner asks pool manager for VM
6.         -> Pool manager returns VM with kernel_ports
7.         -> Provisioner gives KG the ZMQ connection info
8.       -> KG opens ZMQ channels to kernel
9.     <- KG returns kernel_id
10.    -> WebSocket ws://host:8888/api/kernels/{id}/channels
11.      -> KG bridges WebSocket <-> ZMQ

12. Execution API POST /sessions/{id}/execute
13.   -> SandboxSession.execute()
14.     -> Send execute_request over WebSocket -> KG -> ZMQ -> kernel
15.     <- Collect iopub messages <- ZMQ <- KG <- WebSocket
```

## Target Flow

```
1. Execution API POST /sessions
2.   -> Ask pool manager: POST /api/vms/acquire
3.   <- Pool manager returns {vm_id, ip}
4.   -> GET http://{ip}:8888/api/kernels
5.   <- Returns [{"id": "xxx", ...}]  (one prespawned kernel)
6.   -> WebSocket ws://{ip}:8888/api/kernels/xxx/channels
7.      -> Per-VM KG bridges WebSocket <-> ZMQ (all local)

8. Execution API POST /sessions/{id}/execute
9.   -> SandboxSession.execute()
10.    -> Send execute_request over WebSocket -> per-VM KG -> kernel
11.    <- Collect iopub messages <- kernel <- per-VM KG <- WebSocket

12. Execution API DELETE /sessions/{id}
13.   -> Close WebSocket (do NOT delete kernel — VM destruction is the boundary)
14.   -> Pool manager: DELETE /api/vms/{vm_id}
```

Key differences:
- Steps 2-6 replace steps 3-10. The Execution API talks to the pool manager
  directly and discovers the prespawned kernel via GET, not POST.
- SandboxSession connects to the VM's KG instead of the host's KG.
- No provisioner plugin in the middle.
- Session destroy closes the WebSocket and destroys the VM. Kernel deletion
  is unnecessary — VM destruction is the lifecycle boundary.

## KG Configuration (Corrected)

Stock `jupyter_kernel_gateway` does NOT support attaching to an existing kernel
via `--seed_uri` (that option runs a seed notebook on newly spawned kernels).
It also does NOT support "single prestarted kernel attach" mode.

The correct approach uses supported KG configuration:

```
jupyter kernelgateway \
  --KernelGatewayApp.ip=0.0.0.0 \
  --KernelGatewayApp.port=8888 \
  --KernelGatewayApp.prespawn_count=1 \
  --JupyterWebsocketPersonality.list_kernels=True \
  --KernelGatewayApp.seed_uri=/opt/agent/seed_warm_imports.ipynb
```

- `prespawn_count=1` — KG spawns exactly one kernel at startup.
- `list_kernels=True` — exposes `GET /api/kernels` so clients can discover it.
- `seed_uri` — runs a seed notebook (warm imports: numpy, pandas, matplotlib)
  on the prespawned kernel. This replaces the current guest agent's `start_kernel()`.
- KG owns the kernel process. The guest agent no longer starts ipykernel directly.

The Execution API discovers the kernel:
```python
resp = await http.get(f"http://{vm_ip}:8888/api/kernels")
kernels = await resp.json()
kernel_id = kernels[0]["id"]  # exactly one prespawned kernel
```

Then attaches via WebSocket — same protocol as today.

## What Changes

### Guest Agent (`guest/fc_guest_agent.py`)

`pre_warm_kernel()` changes from starting ipykernel directly to starting KG
(which owns the kernel):

```python
def pre_warm_kernel() -> dict:
    python = sys.executable or "/usr/bin/python3"
    kg_proc = subprocess.Popen([
        python, "-m", "jupyter", "kernelgateway",
        "--KernelGatewayApp.ip=0.0.0.0",
        "--KernelGatewayApp.port=8888",
        "--KernelGatewayApp.prespawn_count=1",
        "--JupyterWebsocketPersonality.list_kernels=True",
        "--KernelGatewayApp.seed_uri=/opt/agent/seed_warm_imports.ipynb",
    ])
    _wait_for_kg(timeout=60)
    return {"kg_port": 8888}
```

The seed notebook (`seed_warm_imports.ipynb`) contains:
```python
%matplotlib inline
import numpy, pandas
import os; os.makedirs('/data', exist_ok=True)
```

This replaces the current warm-up code in the agent's `_ensure_session()`.

### SandboxSession (`execution_api/_sandbox/session.py`)

Two changes:

1. `start()` uses `GET /api/kernels` to discover the prespawned kernel
   instead of `POST /api/kernels` to create a new one.

2. `stop()` closes the WebSocket but does NOT delete the kernel (VM
   destruction handles cleanup).

```python
async def start(self) -> None:
    self._http = aiohttp.ClientSession()

    # Discover the prespawned kernel (do NOT create a new one)
    resp = await self._http.get(f"{self._gateway_url}/api/kernels")
    resp.raise_for_status()
    kernels = await resp.json()
    if not kernels:
        raise RuntimeError("No kernel available in VM")
    self._kernel_id = kernels[0]["id"]

    # Open WebSocket (unchanged protocol)
    ws_url = self._gateway_url.replace("http://", "ws://")
    self._ws_ctx = self._http.ws_connect(
        f"{ws_url}/api/kernels/{self._kernel_id}/channels",
    )
    self._ws = await self._ws_ctx.__aenter__()
    self._started = True

async def stop(self) -> None:
    self._started = False
    if self._ws_ctx is not None:
        try:
            await self._ws_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        self._ws_ctx = None
        self._ws = None
    # Do NOT delete kernel — VM destruction is the lifecycle boundary
    if self._http is not None:
        await self._http.close()
        self._http = None
    self._kernel_id = None
```

### Execution API Server (`execution_api/server.py`)

Session creation acquires a VM directly from pool manager:

```python
async def create_session():
    vm = await pool_client.acquire(vcpu=1, mem_mib=512)
    try:
        session = SandboxSession(gateway_url=f"http://{vm['ip']}:8888")
        await session.start()
    except Exception:
        await pool_client.destroy(vm["vm_id"])
        raise

    entry = _SessionEntry(
        session=session,
        vm_id=vm["vm_id"],
        vm_ip=vm["ip"],
        lock=asyncio.Lock(),
        last_active=time.time(),
    )
    return entry
```

Note the try/except: if `session.start()` fails, the VM is destroyed immediately
to prevent leaked VMs.

Session destruction:

```python
async def destroy_session(session_id):
    entry = session_manager.pop(session_id)
    if entry is None:
        return
    await entry.session.stop()              # close WebSocket only
    await pool_client.destroy(entry.vm_id)  # destroy VM
```

### Identifiers

`kernel_id` is no longer used as a host-global identifier. All snapshot-cloned
VMs may share the same `kernel_id` (since it's baked into the snapshot).

Use `session_id` or `vm_id` instead for:
- Artifact storage paths
- Dashboard URLs / Caddy routes
- Logging / metrics
- Any host-level resource keying

### VM Rootfs (`guest/build_rootfs.sh`)

Add `jupyter_kernel_gateway` to installed packages and the seed notebook:

```bash
pip install jupyter_kernel_gateway

# Create seed notebook for warm imports
cat > /opt/agent/seed_warm_imports.ipynb << 'SEED'
{
 "cells": [{"cell_type": "code", "source": [
   "%matplotlib inline\n",
   "import numpy, pandas\n",
   "import os; os.makedirs('/data', exist_ok=True)"
 ], "metadata": {}, "outputs": [], "execution_count": null}],
 "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
 "nbformat": 4, "nbformat_minor": 5
}
SEED
```

### Pool Manager

Minimal changes:
- `acquire` response already includes `{vm_id, ip}`.
- `bind_kernel` call is no longer needed for KG routing.
- Pool Manager validates idle VMs are KG-ready before handing them out
  (health check hits `GET http://{vm_ip}:8888/api/kernels`).
- Auto-cull of assigned VMs must be reconciled with Execution API ownership
  (see Control Plane section).

### What Gets Removed (After Canary)

These are removed ONLY after per-VM KG is proven in production. Until then,
keep behind a feature flag.

- `fc_provisioner/` — entire directory (WarmPoolProvisioner, FirecrackerProvisioner,
  pool_client).
- `fc-kernel-gateway.service` — systemd unit for central KG on host.
- `config/fc-kernel-gateway.service` — service file.
- Kernel spec installation (`python3-firecracker` kernel spec).
- `network.allowed_host_ports: [53, 8888]` — port 8888 no longer needed from
  VM to host (KG is inside the VM now).

### What Stays

- `SandboxSession` — same WebSocket protocol (minor change: GET vs POST).
- `OutputParser` — unchanged (same Jupyter message format).
- `fc_pool_manager/` — unchanged (pool management, VM lifecycle).
- `guest/fc_guest_agent.py` — modified (KG replaces direct kernel start).
- Caddy — still needed for `/artifacts/*`, `/health`, and dev WebSocket proxy.

## Control Plane

### Current Control Plane

```
Execution API                Central KG               Pool Manager
     |                           |                         |
     |  POST /sessions           |                         |
     +-------------------------->|                         |
     |                           |  (provisioner plugin)   |
     |                           +------------------------>| acquire VM
     |                           |<------------------------| {vm_id, ip, ports}
     |                           |                         |
     |                           |  bind_kernel(vm, kid)   |
     |                           +------------------------>| register Caddy route
     |                           |                         |
     |  <-- kernel_id -----------|                         |
     |                           |                         |
     |  DELETE /sessions/{id}    |                         |
     +-------------------------->|  kill kernel            |
     |                           +------------------------>| destroy VM
```

KG owns kernel lifecycle. The provisioner is the glue between KG and Pool Manager.

### Target Control Plane

```
Execution API                          Pool Manager
     |                                      |
     |  POST /sessions                      |
     +------------------------------------->| POST /api/vms/acquire
     |                                      | -> returns warm VM
     |<-------------------------------------| {vm_id, ip}
     |                                      |
     |  GET http://{ip}:8888/api/kernels    |
     |  -> discover prespawned kernel_id    |
     |  WebSocket ws://{ip}:8888/...        |
     |                                      |
     |  DELETE /sessions/{id}               |
     |  -> close WebSocket                  |
     +------------------------------------->| DELETE /api/vms/{vm_id}
     |                                      | -> destroy VM
```

### Ownership

| Responsibility | Current owner | New owner |
|---|---|---|
| VM allocation | KG (via provisioner) | Execution API (direct pool call) |
| Session -> VM mapping | KG (kernel_id) | Execution API (`_SessionEntry.vm_id`) |
| VM destruction | KG (via provisioner) | Execution API (on delete/timeout) |
| Kernel lifecycle | KG | Per-VM KG (inside VM, destroyed with VM) |
| Health: idle VMs | Pool Manager (basic) | Pool Manager (must verify KG-ready) |
| Health: active sessions | KG heartbeat (broken) | Execution API (HTTP to per-VM KG) |
| Session TTL / cleanup | Execution API | Execution API (unchanged) |
| Pool replenishment | Pool Manager | Pool Manager (unchanged) |

### Session State Machine

Sessions have explicit states to prevent races between concurrent delete,
execute, health check, and TTL cleanup:

```
    create
      |
      v
  CREATING  --start() fails--> (destroy VM, discard)
      |
      | start() succeeds
      v
   ACTIVE  <-- execute, health check operate here
      |
      | delete / TTL / health fail
      v
   CLOSING  -- close WS, destroy VM (idempotent)
      |
      v
   CLOSED   -- removed from session manager
```

Rules:
- Only ACTIVE sessions accept execute requests.
- Transition to CLOSING is atomic (compare-and-swap on state).
- VM destroy is idempotent — multiple paths may converge on the same VM.
- `destroy_session()` is safe to call from delete handler, TTL cleanup,
  health check, and execute-failure recovery.

### Session Entry

```python
class SessionState(Enum):
    CREATING = "creating"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"

@dataclass
class _SessionEntry:
    session: SandboxSession
    vm_id: str
    vm_ip: str
    state: SessionState
    lock: asyncio.Lock
    last_active: float
    active_dashboard: str | None
```

### Failure Recovery

| Failure | Detection | Recovery |
|---------|-----------|----------|
| VM dies | Execute returns ConnectionError | Mark CLOSING, destroy VM. Agent creates new session. |
| KG dies inside VM | WebSocket close / HTTP timeout | Same as VM death. |
| VM stale (idle) | Proactive health check | Mark CLOSING, destroy VM, remove session. |
| Pool manager unreachable | Acquire fails | Return 503 to client. |
| start() fails after acquire | Exception in session.start() | Destroy VM immediately (try/finally). |
| Concurrent delete + execute | State check before execute | Execute sees CLOSING, returns error. |

### Health Checks (Two Levels)

**Level 1: Pool Manager checks idle VMs**

Before handing out a VM via acquire, pool manager verifies KG is responsive:

```python
async def _validate_vm_before_acquire(self, vm):
    try:
        resp = await http.get(f"http://{vm.ip}:8888/api/kernels", timeout=3)
        return resp.status == 200
    except Exception:
        return False
```

If validation fails, the VM is culled and a fresh one is allocated.

**Level 2: Execution API checks active sessions**

Periodic sweep of active sessions:

```python
async def _health_check_loop():
    while True:
        await asyncio.sleep(60)
        for sid in list(session_manager.keys()):
            entry = session_manager.get(sid)
            if entry is None or entry.state != SessionState.ACTIVE:
                continue
            try:
                resp = await http.get(
                    f"http://{entry.vm_ip}:8888/api/kernels", timeout=3)
                if resp.status != 200:
                    raise Exception("unhealthy")
            except Exception:
                await destroy_session(sid)
```

### Pool Manager Lifecycle Reconciliation

Current pool manager auto-culls assigned VMs after `vm_idle_timeout` (900s).
With Execution API owning session lifecycle, this creates a conflict: pool
manager might destroy a VM that has an active session.

Resolution: pool manager's auto-cull checks with the Execution API before
destroying assigned VMs, OR the Execution API's session TTL is always shorter
than `vm_idle_timeout` so sessions are cleaned up first.

### Pool Manager Communication

The Execution API needs a direct HTTP client to the pool manager for VM
acquire/destroy. The pool manager listens on a Unix socket
(`/var/run/fc-pool.sock`).

- Production: Execution API on same host -> Unix socket (fast, secure).
- Development (split-host): pool manager also listens on TCP, or Caddy
  proxies pool manager API. Caddy proxy must be localhost-only.

## Performance

### Existing Benchmarks

| Metric | Value |
|--------|-------|
| Cold boot (no snapshot) | ~4.7s |
| Snapshot restore | ~196ms |
| Session create (warm pool) | p50=63ms |
| One-shot execute | p50=107ms |
| VM memory config | 512MB |

### Session Create Breakdown

Current (central KG, warm pool):

```
HTTP POST to central KG /api/kernels          ~5ms
  KG triggers provisioner.pre_launch()         ~2ms
    Pool client acquire (Unix socket)          ~1ms
    Pool manager returns warm VM               ~1ms
  KG opens ZMQ to kernel                       ~10ms
  KG returns kernel_id                         ~2ms
WebSocket connect to central KG                ~5ms
Warm-up execute                                ~35ms
                                               -----
                                        Total: ~63ms
```

Projected (per-VM KG, warm pool):

```
HTTP POST to pool manager /api/vms/acquire     ~2ms
  Pool manager returns warm VM + IP            ~1ms
GET http://{ip}:8888/api/kernels               ~5ms
  Returns prespawned kernel_id                 ~1ms
WebSocket connect to per-VM KG                 ~10ms
Warm-up: already done via seed notebook        ~0ms
                                               -----
                                        Total: ~20ms (projected)
```

Note: warm-up imports (~35ms) move into the seed notebook, which runs during
VM boot/snapshot creation — not at session creation time. This makes session
creation significantly faster.

### Memory Impact

| Component | Memory |
|-----------|--------|
| Firecracker overhead | ~5MB |
| Linux kernel + init | ~20MB |
| ipykernel + Python | ~80MB |
| Pre-warmed packages | ~150MB |
| Per-VM KG (NEW) | ~30-50MB |
| **Total per VM** | **~285-305MB** (up from ~255MB) |

With 512MB per VM, this fits. On a 64GB host (after host overhead): ~150-200
max VMs. Practical difference: negligible for pool sizes of 5-30.

### Burst Scenario

10 concurrent requests, pool size 5:

```
Request 1-5:  warm pool                       -> ~20ms each
Request 6-10: trigger snapshot restore         -> ~216ms each (196ms restore + 20ms setup)
Pool replenishment: parallel restores          -> ~196ms wall clock
```

### Cold Boot (Rare Path)

Cold boot includes KG startup + kernel spawn + seed notebook execution:
~4.7s (boot) + ~2s (KG + kernel + seed) = ~6.7s total.

This only happens when snapshots are invalid (rootfs changed). The golden
snapshot includes KG with a pre-warmed kernel, so snapshot restores pay
zero startup cost.

## Staleness Fix (#41)

With per-VM KG, there is no cross-host connection to go stale:

| Component | Lifetime | Bounded by |
|-----------|----------|------------|
| VM | Pool TTL | Pool manager |
| ipykernel | Same as VM | Owned by per-VM KG |
| Per-VM KG | Same as VM | Started at boot/restore |
| KG-kernel ZMQ | localhost in VM | Both co-located |
| Execution API-KG WebSocket | Per-session | Opened at create, closed at destroy |

After snapshot restore:
- VM restores with KG + kernel already running (both in snapshot)
- KG-kernel ZMQ is localhost — survives restore
- Execution API opens a fresh WebSocket — no stale state

## Scaling Fix (#36)

Each VM is self-contained. The Execution API can connect to VMs on any host:

```
Execution API --> Pool Manager Host A --> VMs on Host A
              --> Pool Manager Host B --> VMs on Host B
```

## Snapshot Considerations

The golden snapshot captures:
- Per-VM KG running (with one prespawned kernel)
- ipykernel running (with warm imports from seed notebook)
- Dispatcher running (for Panel dashboards, dormant)

On restore, all processes resume. KG-kernel ZMQ is localhost — survives restore.

Risks:
- **Duplicated kernel_id**: All cloned VMs inherit the same kernel UUID from
  the snapshot. This is safe as long as kernel_id is never used as a host-global
  identifier. Use session_id or vm_id for artifacts, routes, and logging.
- **KG internal state**: The snapshot is taken before any client WebSocket
  connects (quiescent state). KG has no stale client tracking to worry about.
- **Tornado timers**: KG's Tornado event loop resumes after restore. Timers
  may fire immediately. This should be benign for a quiescent server but
  should be verified during implementation.

## Security

Each VM now exposes an unauthenticated HTTP/WebSocket API on port 8888.

Mitigations:
- VMs are on a private bridge network (172.16.0.0/24), not exposed to the
  internet.
- Current iptables/ebtables rules restrict VM-to-host and VM-to-VM traffic.
- Host-to-VM traffic is unrestricted on the bridge — any host process can
  reach VM KG. This is acceptable if the host is trusted (single-tenant).
- Dev Caddy proxy for VM KG must be localhost-only. Exposing per-VM KG on
  a public listener is remote code execution exposure.

## Network Topology

### Production (Execution API on same host as VMs)

```
Execution API (host) --Unix socket--> Pool Manager (host)
                     --WebSocket----> VM KG (172.16.0.x:8888)
```

Everything is on the same host. VM IPs are directly reachable.

### Development (Execution API on macOS, VMs on Linux)

```
Execution API (macOS) --HTTP---------> Pool Manager (Linux, via Caddy)
                      --WebSocket----> VM KG (172.16.0.x:8888) <- NOT REACHABLE
```

The 172.16.0.x network is internal to the Linux host. Options:
- Run Execution API on the Linux host (simplest, recommended for dev)
- Caddy on the Linux host proxies per-VM WebSocket (dev-only, localhost-only,
  requires path rewriting to strip prefix)

## Migration Path (Feature-Flag Rollout)

Phase 1: Add per-VM KG (keep central KG running)
1. Add KG + seed notebook to VM rootfs (`guest/build_rootfs.sh`)
2. Update guest agent to start KG instead of raw ipykernel
3. Add feature flag: `USE_PER_VM_KG=true/false` in Execution API
4. Behind flag: acquire VM directly, discover kernel via GET, connect
5. Default: false (use central KG, existing flow)
6. Rebuild rootfs, create new golden snapshot, test

Phase 2: Canary
7. Enable flag in staging, run full test suite
8. Monitor for 1 week: staleness, performance, error rates
9. Run benchmarks, compare with baseline

Phase 3: Remove central KG (after canary succeeds)
10. Remove `fc_provisioner/` directory
11. Remove `fc-kernel-gateway.service`
12. Remove feature flag (per-VM KG is the only path)
13. Update deploy scripts, docs, README

Rollback at any phase: set flag to false, restart Execution API.

## Effort Estimate

| Step | Effort |
|------|--------|
| Add KG + seed notebook to rootfs | Small |
| Guest agent: start KG instead of kernel | Small |
| SandboxSession: GET to discover kernel | Small |
| Execution API: direct pool acquire + feature flag | Medium |
| Session state machine | Medium |
| Pool Manager: KG health validation | Small |
| Dev network routing (Caddy or run on Linux) | Medium |
| Deploy + golden snapshot + test | Medium |
| **Total** | **Large (5-8 days)** |

## Out of Scope

- Multi-host pool manager federation (tracked in #36)
- Removing ipykernel in favor of a lighter execution engine
- Per-VM resource limits (CPU, memory) — already handled by Firecracker config
- KG authentication (VMs on private bridge, single-tenant host)
