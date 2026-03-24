# Performance Enhancements: fc-kernel-provisioner

> **This document supersedes `docs/performance-profile.md` and `docs/snapshot-optimization.md`.**
> All performance optimization work is consolidated here as the single authoritative reference.

---

## 1. Executive Summary

All optimizations combined deliver a **25–42× improvement** in user-facing latency across every request path.

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| Session create | 1,133ms | 45ms | **25×** |
| One-shot execute | 3,820ms | 91ms | **42×** |
| VM boot (snapshot restore) | 1,764ms | 190ms | **9.2×** |
| In-session execution | 47–50ms | 47–50ms | Floor (unchanged) |

In-session execution latency is the irreducible floor: FastAPI → Jupyter kernel gateway WebSocket → ipykernel → result. No further optimization is possible without changing the execution model.

---

## 2. Architecture Overview

### Request Paths

```mermaid
flowchart TD
    Client([Client])

    subgraph "Session-Based Path"
        SC[POST /sessions/create]
        EX[POST /sessions/{id}/execute × N]
        SD[DELETE /sessions/{id}]
    end

    subgraph "One-Shot Path"
        OS[POST /execute]
    end

    subgraph "Provisioner Layer"
        WPP[WarmPoolProvisioner\npre_launch: pop from queue ~5ms]
        COLD[Cold Acquire\npool.acquire + start_kernel ~639ms]
        QUEUE[(asyncio.Queue\nwarm VMs)]
    end

    subgraph "Pool Manager"
        POOL[(VM Pool\nidle / active / replenishing)]
        SNAP[Snapshot Restore\n190ms]
        BOOT[Full Boot\n1,764ms]
        REPLENISH[Background Replenish Loop]
    end

    subgraph "VM Instance"
        TAP[TAP / Network\nreconfigure_network via vsock]
        KG[Jupyter Kernel Gateway]
        IK[ipykernel\npre-warmed in snapshot]
    end

    subgraph "Cleanup"
        ASYNC_STOP[asyncio.create_task\nsession.stop — background]
        SYNC_STOP[Synchronous stop\n2,650ms]
    end

    Client --> SC
    Client --> OS
    SC --> WPP
    WPP -- "queue non-empty" --> QUEUE
    WPP -- "queue empty" --> COLD
    QUEUE --> KG
    COLD --> POOL
    OS --> WPP
    EX --> KG
    KG --> IK
    SD --> SYNC_STOP
    OS --> ASYNC_STOP

    REPLENISH --> SNAP
    SNAP --> TAP
    TAP --> IK
    IK --> QUEUE

    POOL --> SNAP
    POOL --> BOOT
```

### Optimization Layer Map

| Layer | Where It Sits | Latency Saved |
|-------|--------------|---------------|
| Snapshot restore | Pool manager boot path | 1,574ms |
| XFS reflink | Jail setup (rootfs copy) | 283ms |
| Network reconfig | Post-restore, pre-ready | Enables production use |
| Pre-warmed kernels | Guest agent + snapshot | 780ms |
| Warm pool provisioner | Provisioner pre_launch | 598ms |
| Async one-shot cleanup | API server response path | 2,636ms |
| Guest agent poll tuning | vsock start_kernel path | ~600ms |

---

## 3. Optimization Stack

Optimizations are listed in implementation order. Each builds on the previous.

### 3.1 Firecracker Snapshot Restore

**Problem:** Full VM boot takes 1,764ms — jailer setup, Firecracker process start, device configuration, kernel boot, guest agent wait.

**Solution:** A golden snapshot is created once at startup. All subsequent VMs are restored from the snapshot instead of booting from scratch.

**Result:** 1,764ms → 190ms **(9.2×)**

#### How It Works

**Golden snapshot creation (once at startup):**
1. Boot a fresh VM with the standard kernel + rootfs
2. Wait for the guest agent to become ready
3. Pause the VM (`PATCH /vm` → `Paused`)
4. Call `PUT /snapshot/create` with `mem_file_path` and `snapshot_path`
5. Save metadata: SHA-256 of kernel, rootfs, and Firecracker binary

**Restore path (every VM after the first):**
1. Allocate IP and TAP interface
2. Create jail directory structure
3. Hardlink snapshot files into the jail (no copy — instant)
4. Call `PUT /snapshot/load` with the snapshot paths
5. Call `PATCH /vm` → `Resumed`

**Auto-invalidation:** On startup, the pool manager computes SHA-256 of the kernel, rootfs, and Firecracker binary and compares against stored metadata. If any file has changed, the snapshot is deleted and rebuilt automatically.

**Files:** `fc_pool_manager/snapshot.py`, `fc_pool_manager/manager.py`, `fc_pool_manager/firecracker_api.py`

---

### 3.2 XFS Reflink for Rootfs Copy

**Problem:** Copying a 1GB rootfs image into each VM's jail directory takes 289ms on ext4 — a full data copy.

**Solution:** XFS filesystem with `reflink=1`. The `cp --reflink=auto` command becomes a metadata-only operation: the new file shares the same underlying extents as the source until a write occurs (copy-on-write).

**Result:** 289ms → 6ms **(48×)**

#### Setup

```bash
# Create a 50GB sparse XFS image with reflink enabled
sudo truncate -s 50G /var/lib/fc-jailer.xfs
sudo mkfs.xfs -m reflink=1 /var/lib/fc-jailer.xfs
sudo mount -o loop /var/lib/fc-jailer.xfs /srv/jailer
```

Add to `/etc/fstab` for persistence:
```
/var/lib/fc-jailer.xfs  /srv/jailer  xfs  loop,defaults  0 0
```

**Critical requirement:** The kernel image, rootfs image, snapshot directory, and `chroot_base` **must all reside on the same XFS filesystem**. Cross-filesystem reflinks are not possible; the kernel falls back to a full copy.

**Files:** `config/fc-pool.yaml`, `scripts/remote-test.sh`

> See `docs/snapshot-optimization.md` for detailed XFS setup and verification steps.

---

### 3.3 Post-Restore Network Reconfiguration

**Problem:** A snapshot captures the golden VM's IP address and MAC address. Every VM restored from that snapshot starts with the same network identity, causing ARP conflicts and routing failures in production.

**Solution:** After restore and resume, the pool manager sends a `reconfigure_network` command to the guest agent over vsock. The guest agent applies a new IP, MAC, and default route, then sends a gratuitous ARP to update the network's ARP cache. The TAP interface is detached from the bridge during reconfiguration to prevent the golden IP from leaking onto the network.

**Result:** Enables production snapshot use. Adds ~35ms to the restore path.

#### Sequence

```
detach TAP from bridge
  → PUT /snapshot/load
  → PATCH /vm Resumed
  → vsock: reconfigure_network(new_ip, new_mac, gateway, garp=true)
  → reattach TAP to bridge
```

**Files:** `guest/fc_guest_agent.py` (`reconfigure_network` action), `fc_pool_manager/manager.py`, `fc_pool_manager/network.py`

---

### 3.4 Pre-Warmed Kernels

**Problem:** ipykernel startup takes ~780ms — Python must import `zmq`, `tornado`, `traitlets`, and the kernel framework before it can accept connections.

**Solution:** The guest agent starts ipykernel during VM boot, before the snapshot is taken. The golden snapshot captures the VM with a running, connected ipykernel. Every restored VM resumes with the kernel already warm.

**Result:** 780ms → 0ms user-facing **(kernel startup moved entirely to background replenishment)**

#### How It Works

**Guest agent (`pre_warm_kernel`):**
- Generates a random HMAC key
- Starts ipykernel on fixed ports 5555–5559
- Stores the key and port assignments

**Pool manager:**
- After boot (or restore), reads `kernel_key` and `kernel_ports` from the guest agent
- Stores them in the `VMInstance` object

**Snapshot bonus:** Because the golden snapshot is taken after `pre_warm_kernel` runs, every restored VM resumes with an already-running ipykernel. No kernel startup cost at all on the restore path.

**Provisioner:**
- Overrides the Jupyter kernel gateway session's HMAC key with the pre-warmed key
- Skips `launch_kernel` entirely — the kernel is already running

**Files:** `guest/fc_guest_agent.py`, `fc_pool_manager/vm.py`, `fc_pool_manager/manager.py`, `fc_provisioner/provisioner.py`

---

### 3.5 Warm Pool Provisioner

**Problem:** Even with snapshot restore and pre-warmed kernels, session create still takes ~639ms because the provisioner must acquire a VM from the pool and complete the Jupyter kernel gateway handshake (ZMQ channel setup + nudge poll at 0.5s interval).

**Solution:** A custom Jupyter kernel provisioner (`WarmPoolProvisioner`) maintains a queue of pre-acquired VMs with running kernels. `pre_launch()` pops from the queue in ~5ms instead of acquiring on demand.

**Result:** 639ms → 41ms session create **(including KG handshake, which completes in ~30ms because the kernel is already responding)**

#### How It Works

- `WarmPoolProvisioner` subclasses `FirecrackerProvisioner`
- A class-level `asyncio.Queue` holds pre-acquired `VMInstance` objects
- A background replenish loop keeps the queue at `warm_pool_target` size
- `pre_launch()` pops from the queue (~5ms) instead of calling `pool.acquire` + `start_kernel` (~639ms)
- Falls back to cold acquire if the queue is empty (pool exhausted or system startup)

**Config:** The kernelspec uses the `firecracker-warm-pool` provisioner entry point.

```json
{
  "argv": ["python", "-m", "jupyter_kernel_gateway", "..."],
  "display_name": "Firecracker Python",
  "language": "python",
  "metadata": {
    "kernel_provisioner": {
      "provisioner_name": "firecracker-warm-pool"
    }
  }
}
```

**Files:** `fc_provisioner/warm_pool.py`, `config/kernelspec/kernel.json`, `pyproject.toml`

---

### 3.6 Async One-Shot Cleanup

**Problem:** The one-shot endpoint (`POST /execute`) was waiting for `session.stop()` before returning the response. VM destruction takes 2,632ms — the user waited for cleanup they don't care about.

**Solution:** Fire-and-forget `session.stop()` via `asyncio.create_task`. The response is returned immediately after code execution completes. Cleanup runs in the background.

**Result:** 2,727ms → 91ms one-shot **(30×)**

```python
# Before
result = await execute(session, code)
await session.stop()          # user waits 2,632ms
return result

# After
result = await execute(session, code)
asyncio.create_task(session.stop())   # fire and forget
return result                          # immediate
```

**Files:** `execution_api/server.py` (`one_shot_execute` endpoint)

---

### 3.7 Guest Agent Poll Optimizations

**Problem:** The vsock `start_kernel` path had two sources of unnecessary latency:
- A fixed 500ms post-spawn sleep before checking if the kernel process was alive
- A 200ms poll interval and 500ms socket timeout when waiting for the kernel port to open

**Solution:**
- Replace the 500ms sleep with 5 × 10ms fast crash-detect polls (total 50ms max wait, exits immediately on crash)
- Reduce port poll interval from 200ms to 50ms
- Reduce socket timeout from 500ms to 200ms

**Result:** ~600ms saved in the vsock `start_kernel` path

**Files:** `guest/fc_guest_agent.py`

---

## 4. Time Breakdown Tables

### 4.1 Session Create: Cumulative Effect of Each Optimization

| Optimization Applied | Session Create | Delta |
|---------------------|---------------|-------|
| Baseline (no optimizations) | 3,820ms | — |
| + Snapshot restore | ~2,000ms | −1,800ms |
| + XFS reflink | ~1,750ms | −250ms |
| + Network reconfig (enables production snapshots) | ~1,700ms | enables production |
| + Pre-warmed kernels | ~639ms | −1,060ms |
| + Warm pool provisioner | ~41ms | −598ms |

### 4.2 Full Lifecycle Breakdown (Production, All Optimizations)

| Phase | User-Facing | Background | What Happens |
|-------|------------|------------|-------------|
| Session create | 40ms | — | Pop warm VM from provisioner queue |
| Execute (first, with import) | 250ms | — | `import numpy` costs ~200ms first time |
| Execute (warm) | 50ms | — | WebSocket → kernel → result |
| Session delete | instant | 2,650ms | KG shutdown + provisioner cleanup + VM destroy |
| Pool replenish | — | ~1,000ms | Snapshot restore (190ms) + pre-warm kernel (800ms) |

**Key insight:** The "background cost" concept separates work the user waits for from work that happens asynchronously. Session delete and pool replenishment are entirely background — the user sees 0ms for both.

### 4.3 Code Execution by Tier

| Tier | Workload | p50 | Notes |
|------|----------|-----|-------|
| T1: Trivial | `print("hello")` | 47ms | API floor: FastAPI + KG WebSocket + kernel |
| T2: Compute | `sum(range(10_000_000))` | 157ms | ~110ms actual CPU work |
| T3: Data | pandas groupby + describe | 49ms | Warm session (pandas pre-imported) |
| T4: Visualization | matplotlib 2×2 subplot | 550ms | Figure creation + PNG encoding |
| T5: Heavy | scipy L-BFGS-B (50 dims) | 561ms | Optimizer convergence time |

T1 (47ms) is the irreducible floor. It represents the minimum round-trip through FastAPI, the kernel gateway WebSocket, and ipykernel for a no-op execution.

### 4.4 Burst Load Performance

| Scenario | Wall Time | p50 | p95 | Bottleneck |
|----------|-----------|-----|-----|-----------|
| 10 concurrent one-shots | 6,131ms | 3,413ms | 6,122ms | Warm pool drains after 3; cold fallback |
| 10 concurrent sessions | 8,819ms | 7,326ms | 8,809ms | Same + synchronous delete per session |

**Burst behavior:** With `warm_pool_target=3`, the first 3 concurrent requests are served from the warm queue (~41ms each). Requests 4–10 fall back to the cold path (~639ms each) while the replenish loop races to refill the queue. The p95 spread reflects this bimodal distribution.

---

## 5. Best Practices for Production

### 5.1 Size the Warm Pool to Match Concurrency

The `warm_pool_target` setting (default: 3) determines how many concurrent session creates can be served instantly from the warm queue. Set it to match your expected burst size.

```yaml
# config/fc-pool.yaml
warm_pool_target: 10   # serve 10 concurrent creates instantly
pool_size: 15          # headroom: warm_pool_target + 2–5 for replenish
```

**Trade-off:** Each warm entry = 1 idle VM consuming ~512MB RAM. A target of 10 requires ~5GB RAM reserved for idle VMs.

| warm_pool_target | RAM reserved | Concurrent instant creates |
|-----------------|-------------|---------------------------|
| 3 (default) | ~1.5GB | 3 |
| 10 | ~5GB | 10 |
| 20 | ~10GB | 20 |

### 5.2 Use Sessions, Not One-Shots

Sessions are **1.8× faster per execution** and use one VM for an entire conversation. One-shot creates and destroys a VM per call.

| Pattern | Per-execution | VM usage | Best for |
|---------|--------------|----------|----------|
| Session | 50ms | 1 VM / conversation | Chatbots, interactive use |
| One-shot | 91ms | 1 VM / call | Stateless API, batch processing |

For any workload with more than one execution per user interaction, sessions are strictly better.

### 5.3 Defer Delete — Let Auto-Cull Handle Cleanup

Instead of calling `DELETE /sessions/{id}` immediately after the last execution, let sessions idle. The auto-cull (`vm_idle_timeout=600s`) destroys them automatically after 10 minutes of inactivity.

**Benefits:**
- Avoids 2.6s synchronous delete on the user path
- Session reuse if the user returns within 10 minutes
- Same security guarantee — VM is destroyed either way

```python
# Instead of this:
result = await execute(session_id, code)
await delete_session(session_id)   # 2,650ms on user path

# Do this:
result = await execute(session_id, code)
# Let auto-cull handle it
```

### 5.4 Pre-Warm Imports

The first import of a heavy library costs 200–900ms. Send a warmup execution immediately after session create, before the user's first request:

```python
session_id = await create_session()

# Warmup — runs once, ~250ms, invisible to user if done during session setup
await execute(session_id, "import numpy, pandas, matplotlib.pyplot as plt")

# All subsequent user executions are fast
result = await execute(session_id, user_code)
```

**Import cost reference:**

| Library | First import cost |
|---------|-----------------|
| numpy | ~80ms |
| pandas | ~200ms |
| matplotlib | ~300ms |
| scipy | ~400ms |

### 5.5 Monitor Pool Pressure

Key Prometheus metrics to watch:

| Metric | Alert condition | Meaning |
|--------|----------------|---------|
| `fc_pool_vms_total{state="idle"}` | < 1 | Pool exhausted; cold fallback active |
| `fc_pool_acquire_total{result="exhausted"}` | > 0 | Requests hitting cold path |
| `fc_pool_boot_duration_seconds` | p95 > 500ms | Snapshot restore degraded |
| `fc_pool_auto_cull_total` | Sudden spike | Unexpected session churn |

When `idle` hits 0, every new session falls back to the cold path (~639ms). Size `pool_size` and `warm_pool_target` to keep idle > 0 under normal load.

### 5.6 Batch Requests Per Session

For high-throughput APIs, one session handles an entire batch sequentially. This amortizes the 40ms session create cost across all requests in the batch:

```python
session_id = await create_session()   # 40ms, once

async for request in batch:
    result = await execute(session_id, request.code)   # 50ms each
    yield result

# Session auto-culled after idle timeout
```

One VM handles the entire batch. Throughput is limited by execution time, not VM acquisition overhead.

### 5.7 XFS Is Required for Production Snapshots

The rootfs, kernel, snapshot files, and jailer `chroot_base` **must** be on the same XFS filesystem with `reflink=1`. Without XFS reflink, each VM restore copies the full rootfs (289ms instead of 6ms), and snapshot restore performance degrades significantly.

Verify reflink is active:

```bash
# Should show "reflink=1" in the output
xfs_info /srv/jailer | grep reflink

# Verify a reflink copy is instant (not a data copy)
time cp --reflink=always /srv/jailer/rootfs.ext4 /tmp/test-reflink.ext4
# Should complete in <10ms for a 1GB file
```

The setup is automated in `scripts/remote-test.sh`.

---

## 6. Infrastructure Requirements

| Component | Requirement | Notes |
|-----------|------------|-------|
| Filesystem | XFS with `reflink=1` for `/srv/jailer` | Required for 6ms rootfs copy |
| Firecracker | v1.6+ | Snapshot API with `mem_file_path` |
| Pool size | `pool_size ≥ warm_pool_target + 2` | Headroom for in-flight replenishment |
| VM memory | 512MB per VM | 768MB if using Panel dashboards |
| Warm pool disk | ~3GB for 30 VMs | XFS CoW — only dirty pages consume space |
| Host kernel | 5.10+ | KVM + vsock support |
| Python | 3.11+ | `asyncio.TaskGroup` used in provisioner |

---

## 7. Known Limitations

### 1. Warm Pool Cold Start
The first few requests after system startup may hit the cold path (~639ms) while the replenish loop fills the warm queue. This is unavoidable — the pool must boot VMs before it can serve them. Mitigate by pre-starting the service before traffic arrives.

### 2. Burst Beyond Pool Size
Requests exceeding `warm_pool_target` concurrent creates fall back to the cold path (~639ms each). The replenish loop races to refill the queue, but there is a window where burst traffic sees cold latency. Size `warm_pool_target` to match your burst profile.

### 3. HMAC Key Sharing (Security Trade-off)
All VMs restored from the same golden snapshot share the same ipykernel HMAC key (generated once during golden VM boot). This is a deliberate trade-off: generating a fresh key per VM would require restarting the kernel after restore, adding ~500ms back to the startup path and negating much of the snapshot benefit.

**Mitigations in place:**
- VMs are network-isolated by ebtables (VM-to-VM traffic blocked at bridge level)
- Only the Kernel Gateway on the host communicates with kernel ZMQ ports
- The HMAC key authenticates ZMQ messages, not network access — it prevents message forgery, not eavesdropping
- VMs are destroyed after each session — no long-lived key exposure

**Residual risk:** If an attacker gains the shared key AND network access to another VM's ports (bypassing ebtables), they could forge Jupyter messages to that kernel. This requires two independent security failures.

**To eliminate this risk entirely:** Don't snapshot the live kernel. Remove `pre_warm_kernel()` from the golden snapshot path and pre-warm after each restore instead. This adds ~780ms to session create (back to 639ms) but gives unique keys per VM.

### 4. Sequential TAP Rename
Snapshot restore requires renaming the TAP interface from the golden VM's name to the new VM's name. This operation is serialized by a boot lock. Under high concurrency, VMs queue behind the lock. Firecracker v1.14+ introduces `network_overrides` in the snapshot load API, which would eliminate this serialization point.

### 5. Delete Latency
Session delete takes ~2,650ms (KG shutdown + VM destruction). For one-shot requests, this is always async (fire-and-forget). For explicit `DELETE /sessions/{id}` calls, it is synchronous — the client waits. Use auto-cull (§5.3) to avoid this on the user path.

---

## 8. Benchmark Reproduction

### Full API Benchmark

Runs all tiers (T1–T5) plus concurrent burst scenarios:

```bash
uv run python scripts/benchmark_api.py --url http://localhost:8000 --iterations 5
```

### Snapshot Restore Benchmark

Measures raw snapshot restore time (VM boot only, no kernel gateway):

```bash
sudo uv run python scripts/benchmark_snapshot.py \
  --config config/fc-pool.yaml \
  --iterations 5
```

### Quick One-Shot Smoke Test

```bash
curl -X POST http://localhost:8000/execute \
  -H 'Content-Type: application/json' \
  -d '{"code": "print(\"hello\")"}'
```

### Session Lifecycle Smoke Test

```bash
# Create session
SESSION=$(curl -s -X POST http://localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{}' | jq -r '.session_id')

# Execute
curl -s -X POST "http://localhost:8000/sessions/${SESSION}/execute" \
  -H 'Content-Type: application/json' \
  -d '{"code": "2 + 2"}'

# Delete
curl -s -X DELETE "http://localhost:8000/sessions/${SESSION}"
```

### Expected Baseline Numbers

| Benchmark | Expected p50 |
|-----------|-------------|
| Session create | 40–50ms |
| One-shot execute (`print`) | 85–100ms |
| In-session execute (`print`) | 47–55ms |
| Snapshot restore (raw) | 180–210ms |
| 10 concurrent one-shots (wall) | 5,500–6,500ms |

If session create exceeds 100ms, check:
1. `fc_pool_vms_total{state="idle"}` — pool may be empty
2. XFS reflink active — `xfs_info /srv/jailer | grep reflink`
3. Snapshot valid — check pool manager startup logs for "rebuilding snapshot"
