# Production Hardening — Design Specification

> **Date**: 2026-03-23
> **Status**: Approved
> **Issues**: #23 VM auto-cull, #22 Prometheus metrics
> **Approach**: Hard-timeout cull loop + `prometheus_client` gauge/histogram/counter metrics

---

## 1. Overview

Two production-hardening features that address resource leakage and observability gaps in the pool manager.

**#23 VM Auto-Cull:** Assigned VMs that are never explicitly released (e.g., provisioner crash, caller abandonment) accumulate indefinitely. A background cull loop scans for ASSIGNED VMs older than a configurable timeout and destroys them, freeing resources and triggering pool replenishment. The cull is "graceful" in that a SIGTERM is sent via vsock before the VM is destroyed.

**#22 Prometheus Metrics:** The pool manager has no external observability. A new `/api/metrics` endpoint exposes pool state, acquire/release rates, boot latency, and cull counts in standard Prometheus exposition format. This slots into any Prometheus scrape pipeline without additional exporters.

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Cull target | ASSIGNED VMs only | Pool manager has no visibility into kernel activity; ASSIGNED = in-use, so we time out from assignment, not last activity |
| Cull timeout semantics | Hard timeout from `assigned_at` | Simple, predictable, zero complexity in the guest |
| Graceful shutdown | vsock `{action: "signal", signum: 15}`, 5s wait, then destroy | Matches existing vsock protocol; gives kernel time to flush |
| Cull loop placement | New `auto_cull_loop()` in `PoolManager`, separate from `health_check_loop()` | Separation of concerns; different cadence (60s vs `health_check_interval`) |
| Lock strategy | Hold `_acquire_lock` while scanning ASSIGNED VMs | Prevents race with concurrent `acquire()`/`release()` calls |
| Prometheus library | `prometheus_client` (official PyPI package) | First-class aiohttp support via `make_aiohttp_handler()`; no custom exposition logic |
| Metrics placement | New `fc_pool_manager/metrics.py` module | Single definition point; avoids circular imports; manager imports metrics, server adds route |
| Multiprocess mode | Disabled (single-process) | Pool manager is a single asyncio process; no forking |
| Timestamp fields | `created_at: float` and `assigned_at: float | None` on `VMInstance` | `created_at` drives boot duration histogram; `assigned_at` drives cull timeout and (future) assignment duration metrics |

### What This Does NOT Include

- Per-VM usage tracking inside the guest (kernel activity, CPU/memory)
- Prometheus push gateway support
- Authentication on `/api/metrics`
- Cull of BOOTING VMs (those have their own timeout in `_boot_vm`)

---

## 2. Feature: VM Auto-Cull (#23)

### Timestamp Foundation

`VMInstance` gains two new fields in `fc_pool_manager/vm.py`:

```python
created_at: float = field(default_factory=time.monotonic)
assigned_at: float | None = field(default=None)
```

`assigned_at` is set by `transition_to()` when entering `ASSIGNED`, and cleared (set to `None`) when leaving `ASSIGNED` back to `IDLE` (i.e., on a `release(destroy=False)`). This makes timestamp management self-contained inside the state machine rather than scattered across callers.

`created_at` is set once at construction time and never changes. It is used by the metrics module to calculate boot duration (time from `VMInstance` creation to first `IDLE` transition).

### Configuration

One new key in the `pool:` section of `fc-pool.yaml`:

```yaml
pool:
  vm_idle_timeout: 600   # seconds; ASSIGNED VMs older than this are culled (0 = disabled)
```

`PoolConfig` gets a new `vm_idle_timeout: int` field (default `600`). Setting it to `0` disables auto-cull entirely.

### Auto-Cull Loop

New method `auto_cull_loop()` in `PoolManager`, spawned alongside `health_check_loop()` in `run_server()`:

```
every 60 seconds:
    if vm_idle_timeout == 0: skip
    acquire _acquire_lock
    for each ASSIGNED vm:
        age = now - vm.assigned_at
        if age > vm_idle_timeout:
            log warning (vm_id, age, timeout)
            increment fc_pool_auto_cull_total counter
            vm.transition_to(STOPPING)
            send vsock {action: "signal", signum: 15}  — best-effort, 5s timeout
            await asyncio.sleep(5)
            _destroy_vm(vm)
            del _vms[vm_id]
    release _acquire_lock
    await replenish()
```

The vsock shutdown signal is fire-and-forget with a 5-second timeout — if the guest is unresponsive, `_destroy_vm()` proceeds anyway. This is the same pattern used by `health_check_loop()` when replacing unhealthy VMs.

Replenish runs **after** releasing the lock, matching the existing `health_check_loop()` pattern (line 264 of `manager.py`).

### Log Messages

```
WARNING: Auto-culling VM vm-abc123 (assigned 743s ago, timeout=600s)
INFO:    Auto-culled 2 VMs, replenishing pool
```

### Graceful Shutdown Detail

```python
try:
    from .vsock import vsock_request
    await asyncio.wait_for(
        vsock_request(vm.vsock_path, {"action": "signal", "signum": 15}, timeout=5),
        timeout=5,
    )
    await asyncio.sleep(5)   # grace period for guest to exit cleanly
except Exception as exc:
    logger.debug("Vsock signal to %s failed (continuing): %s", vm.vm_id, exc)
```

The `vsock_request` already handles `ConnectionError`/`OSError`. The outer `wait_for` caps the total vsock overhead to 5s before the `asyncio.sleep(5)` grace period. Total latency from cull decision to `_destroy_vm()` is at most ~11s per VM.

---

## 3. Feature: Prometheus Metrics (#22)

### Metric Definitions

All metrics live in `fc_pool_manager/metrics.py`. The module is imported once at process start; metric objects are module-level singletons.

| Metric name | Type | Labels | Description |
|-------------|------|--------|-------------|
| `fc_pool_vms_total` | Gauge | `state` ∈ {idle, assigned, booting, stopping} | Current VM count per state |
| `fc_pool_max_vms` | Gauge | — | Configured `max_vms` ceiling |
| `fc_pool_acquire_duration_seconds` | Histogram | — | Wall-clock time from acquire() entry to VM returned |
| `fc_pool_boot_duration_seconds` | Histogram | — | Time from VMInstance creation to first IDLE transition |
| `fc_pool_acquire_total` | Counter | `result` ∈ {success, exhausted, error} | Acquire call outcomes |
| `fc_pool_release_total` | Counter | — | Successful release() calls |
| `fc_pool_health_check_failures_total` | Counter | — | VMs replaced by health_check_loop |
| `fc_pool_auto_cull_total` | Counter | — | VMs destroyed by auto_cull_loop |

**Histogram buckets** (same for both duration histograms):
`[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0]` seconds. These cover normal fast-path (acquire idle VM < 0.1s), on-demand boot (5–30s), and outliers up to 60s.

### Gauge Update Strategy

`fc_pool_vms_total` is a gauge with a `state` label. It is **recomputed from `_vms` directly** rather than incremented/decremented. A helper `_update_vm_gauges()` is called at the end of `acquire()`, `release()`, `health_check_loop()`, `auto_cull_loop()`, and `replenish()`. This avoids drift from any code path that modifies `_vms` without updating metrics.

```python
def _update_vm_gauges(self) -> None:
    counts = {s.value: 0 for s in VMState}
    for vm in self._vms.values():
        counts[vm.state.value] += 1
    for state, count in counts.items():
        POOL_VMS_TOTAL.labels(state=state).set(count)
```

`fc_pool_max_vms` is set once in `PoolManager.__init__()` from `config.max_vms`.

### Boot Duration Instrumentation

`_boot_vm()` records the boot duration using `VMInstance.created_at`:

```python
# at end of _boot_vm(), just before return vm
boot_secs = asyncio.get_event_loop().time() - vm.created_at
BOOT_DURATION.observe(boot_secs)
```

`created_at` uses `time.monotonic()` (same clock as `asyncio.get_event_loop().time()` on CPython), so subtraction is valid.

### Acquire Duration Instrumentation

`acquire()` wraps the fast path and the on-demand boot path:

```python
async def acquire(self, vcpu: int, mem_mib: int) -> dict[str, Any]:
    t0 = asyncio.get_event_loop().time()
    try:
        result = await self._acquire_inner(vcpu, mem_mib)
        ACQUIRE_DURATION.observe(asyncio.get_event_loop().time() - t0)
        ACQUIRE_TOTAL.labels(result="success").inc()
        return result
    except RuntimeError as e:
        if "pool_exhausted" in str(e):
            ACQUIRE_TOTAL.labels(result="exhausted").inc()
        else:
            ACQUIRE_TOTAL.labels(result="error").inc()
        raise
```

The existing acquire body moves into `_acquire_inner()` to keep the instrumentation wrapper clean.

### `/api/metrics` Endpoint

`server.py` adds one route:

```python
from prometheus_client import make_aiohttp_handler

app.router.add_get("/api/metrics", make_aiohttp_handler())
```

`make_aiohttp_handler()` returns an aiohttp-compatible handler that serialises the default registry in Prometheus text exposition format (Content-Type: `text/plain; version=0.0.4`). No custom serialisation code needed.

---

## 4. Configuration Changes

### `config/fc-pool.yaml` additions

```yaml
pool:
  size: 5
  max_vms: 30
  replenish_threshold: 2
  health_check_interval: 30
  vm_idle_timeout: 600      # NEW: seconds before an ASSIGNED VM is auto-culled (0 = disabled)
```

### `fc_pool_manager/config.py` additions

```python
@dataclass(frozen=True)
class PoolConfig:
    # ... existing fields ...
    vm_idle_timeout: int   # NEW

    @classmethod
    def from_yaml(cls, path: str) -> "PoolConfig":
        # ...
        return cls(
            # ... existing ...
            vm_idle_timeout=pool.get("vm_idle_timeout", 600),   # NEW
        )
```

---

## 5. Complete Code Sketches

### `fc_pool_manager/vm.py` (modified)

```python
"""VM instance state management and CID allocation."""

import time
from asyncio.subprocess import Process
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
    VMState.BOOTING:  {VMState.IDLE, VMState.STOPPING},
    VMState.IDLE:     {VMState.ASSIGNED, VMState.STOPPING},
    VMState.ASSIGNED: {VMState.IDLE, VMState.STOPPING},
    VMState.STOPPING: set(),
}


class CIDAllocator:
    # ... unchanged ...


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
    jailer_process: Optional[Process] = field(default=None, repr=False)

    # NEW: lifecycle timestamps (monotonic seconds)
    created_at: float = field(default_factory=time.monotonic)
    assigned_at: Optional[float] = field(default=None)

    def transition_to(self, new_state: VMState) -> None:
        if not self.state.can_transition_to(new_state):
            raise ValueError(
                f"Invalid state transition: {self.state.value} -> {new_state.value} "
                f"for VM {self.vm_id}"
            )
        self.state = new_state
        # NEW: maintain assigned_at alongside state
        if new_state == VMState.ASSIGNED:
            self.assigned_at = time.monotonic()
        elif new_state == VMState.IDLE:
            self.assigned_at = None
```

### `fc_pool_manager/config.py` (modified)

```python
"""YAML configuration loader for the pool manager."""

from dataclasses import dataclass
import yaml


@dataclass(frozen=True)
class PoolConfig:
    """Typed, immutable configuration for the pool manager."""

    pool_size: int
    max_vms: int
    health_check_interval: int
    vm_idle_timeout: int        # NEW: 0 = disabled

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
        vm   = raw["vm_defaults"]
        net  = raw["network"]
        jail = raw["jailer"]

        return cls(
            pool_size=pool["size"],
            max_vms=pool["max_vms"],
            health_check_interval=pool.get("health_check_interval", 30),
            vm_idle_timeout=pool.get("vm_idle_timeout", 600),   # NEW
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

### `fc_pool_manager/metrics.py` (new)

```python
"""Prometheus metric definitions for the pool manager.

All metric objects are module-level singletons. Import this module to
register metrics with the default prometheus_client registry.

Usage:
    from .metrics import POOL_VMS_TOTAL, ACQUIRE_DURATION, ACQUIRE_TOTAL, ...
    ACQUIRE_TOTAL.labels(result="success").inc()
    ACQUIRE_DURATION.observe(elapsed_seconds)
"""

from prometheus_client import Counter, Gauge, Histogram

# Bucket boundaries covering: fast idle acquire (<0.1s), on-demand boot (5-30s), outliers (60s)
_DURATION_BUCKETS = [0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0]

# --- Gauges ---

POOL_VMS_TOTAL = Gauge(
    "fc_pool_vms_total",
    "Current number of VMs in each lifecycle state",
    labelnames=["state"],
)

POOL_MAX_VMS = Gauge(
    "fc_pool_max_vms",
    "Configured hard ceiling on total VMs (max_vms)",
)

# --- Histograms ---

ACQUIRE_DURATION = Histogram(
    "fc_pool_acquire_duration_seconds",
    "Wall-clock time from acquire() entry to VM returned",
    buckets=_DURATION_BUCKETS,
)

BOOT_DURATION = Histogram(
    "fc_pool_boot_duration_seconds",
    "Time from VMInstance creation to first IDLE transition",
    buckets=_DURATION_BUCKETS,
)

# --- Counters ---

ACQUIRE_TOTAL = Counter(
    "fc_pool_acquire_total",
    "Total acquire() calls by outcome",
    labelnames=["result"],   # success | exhausted | error
)

RELEASE_TOTAL = Counter(
    "fc_pool_release_total",
    "Total successful release() calls",
)

HEALTH_CHECK_FAILURES_TOTAL = Counter(
    "fc_pool_health_check_failures_total",
    "Total VMs replaced by the health check loop",
)

AUTO_CULL_TOTAL = Counter(
    "fc_pool_auto_cull_total",
    "Total VMs destroyed by the auto-cull loop",
)
```

### `fc_pool_manager/manager.py` (modified — key diffs only)

```python
"""Pool manager — maintains a pool of pre-warmed Firecracker microVMs."""

import asyncio
import logging
import os
import secrets
import shutil
import time
from typing import Any, Optional

from .config import PoolConfig
from .firecracker_api import FirecrackerAPI
from .metrics import (
    ACQUIRE_DURATION, ACQUIRE_TOTAL, AUTO_CULL_TOTAL,
    BOOT_DURATION, HEALTH_CHECK_FAILURES_TOTAL,
    POOL_MAX_VMS, POOL_VMS_TOTAL, RELEASE_TOTAL,
)
from .network import NetworkManager
from .vm import CIDAllocator, VMInstance, VMState

logger = logging.getLogger(__name__)

_CULL_INTERVAL = 60  # seconds between auto-cull scans


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
        self._acquire_lock = asyncio.Lock()
        POOL_MAX_VMS.set(config.max_vms)    # set once; config is frozen

    # ------------------------------------------------------------------ #
    # Gauge helper                                                         #
    # ------------------------------------------------------------------ #

    def _update_vm_gauges(self) -> None:
        """Recompute fc_pool_vms_total from current _vms dict."""
        counts = {s.value: 0 for s in VMState}
        for vm in self._vms.values():
            counts[vm.state.value] += 1
        for state, count in counts.items():
            POOL_VMS_TOTAL.labels(state=state).set(count)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    @property
    def idle_count(self) -> int:
        return sum(1 for vm in self._vms.values() if vm.state == VMState.IDLE)

    @property
    def total_count(self) -> int:
        return len(self._vms)

    def pool_status(self) -> dict[str, Any]:
        counts: dict[str, Any] = {"idle": 0, "assigned": 0, "booting": 0}
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

        t0 = asyncio.get_event_loop().time()
        try:
            result = await self._acquire_inner(vcpu, mem_mib)
            ACQUIRE_DURATION.observe(asyncio.get_event_loop().time() - t0)
            ACQUIRE_TOTAL.labels(result="success").inc()
            self._update_vm_gauges()
            return result
        except RuntimeError as e:
            if "pool_exhausted" in str(e):
                ACQUIRE_TOTAL.labels(result="exhausted").inc()
            else:
                ACQUIRE_TOTAL.labels(result="error").inc()
            raise

    async def _acquire_inner(self, vcpu: int, mem_mib: int) -> dict[str, Any]:
        """Inner acquire logic (extracted for clean instrumentation wrapper)."""
        async with self._acquire_lock:
            for vm in self._vms.values():
                if vm.state == VMState.IDLE:
                    vm.transition_to(VMState.ASSIGNED)
                    logger.info("Acquired VM %s (ip=%s, cid=%d)", vm.vm_id, vm.ip, vm.cid)
                    asyncio.create_task(self.replenish())
                    return {"id": vm.vm_id, "ip": vm.ip, "vsock_path": vm.vsock_path}

            if self.total_count >= self._config.max_vms:
                raise RuntimeError("pool_exhausted")

            logger.info("No idle VMs, booting on demand")
            vm = await self._boot_vm()
            vm.transition_to(VMState.ASSIGNED)
            return {"id": vm.vm_id, "ip": vm.ip, "vsock_path": vm.vsock_path}

    async def release(self, vm_id: str, destroy: bool = True) -> None:
        """Release a VM back to the pool or destroy it."""
        vm = self._vms.get(vm_id)
        if vm is None:
            logger.warning("Release called for unknown VM %s", vm_id)
            return

        if destroy:
            if vm.state != VMState.STOPPING:
                vm.transition_to(VMState.STOPPING)
            await self._destroy_vm(vm)
            del self._vms[vm_id]
            logger.info("Destroyed VM %s", vm_id)
        else:
            try:
                from .vsock import vsock_request
                await vsock_request(vm.vsock_path, {"action": "reset"}, timeout=10)
            except Exception as exc:
                logger.warning(
                    "Failed to reset guest for VM %s before releasing to idle pool: %s",
                    vm_id, exc,
                )
            vm.transition_to(VMState.IDLE)
            logger.info("Released VM %s back to idle pool", vm_id)

        RELEASE_TOTAL.inc()
        self._update_vm_gauges()

    async def is_alive(self, vm_id: str) -> dict[str, Any]:
        # ... unchanged ...

    # ------------------------------------------------------------------ #
    # Boot / destroy                                                       #
    # ------------------------------------------------------------------ #

    async def _boot_vm(self) -> VMInstance:
        """Boot a new jailed Firecracker VM."""
        # ... existing setup unchanged up to the final transition ...

        vm.transition_to(VMState.IDLE)
        boot_secs = asyncio.get_event_loop().time() - vm.created_at  # NEW
        BOOT_DURATION.observe(boot_secs)                             # NEW
        logger.info("VM %s booted in %.1fs (ip=%s, cid=%d)", vm_id, boot_secs, ip, cid)
        self._update_vm_gauges()
        return vm

    async def _destroy_vm(self, vm: VMInstance) -> None:
        # ... unchanged ...

    # ------------------------------------------------------------------ #
    # Background loops                                                     #
    # ------------------------------------------------------------------ #

    async def replenish(self) -> None:
        """Boot VMs until idle count meets pool_size."""
        async with self._boot_lock:
            while (
                self.idle_count < self._config.pool_size
                and self.total_count < self._config.max_vms
            ):
                count_before = self.total_count
                try:
                    await self._boot_vm()
                except Exception as e:
                    logger.error("Failed to boot VM: %s", e)
                    break
                if self.total_count <= count_before:
                    break
        self._update_vm_gauges()

    async def health_check_loop(self) -> None:
        """Periodically ping idle VMs and replace unhealthy ones."""
        while True:
            await asyncio.sleep(self._config.health_check_interval)
            async with self._acquire_lock:
                for vm in list(self._vms.values()):
                    if vm.state != VMState.IDLE:
                        continue
                    health = await self.is_alive(vm.vm_id)
                    if not health["alive"]:
                        logger.warning("VM %s unhealthy, replacing", vm.vm_id)
                        HEALTH_CHECK_FAILURES_TOTAL.inc()   # NEW
                        vm.transition_to(VMState.STOPPING)
                        await self._destroy_vm(vm)
                        del self._vms[vm.vm_id]
            self._update_vm_gauges()
            await self.replenish()

    async def auto_cull_loop(self) -> None:                             # NEW
        """Destroy ASSIGNED VMs that have exceeded vm_idle_timeout."""
        if self._config.vm_idle_timeout == 0:
            logger.info("Auto-cull disabled (vm_idle_timeout=0)")
            return

        while True:
            await asyncio.sleep(_CULL_INTERVAL)
            culled = 0
            now = time.monotonic()

            async with self._acquire_lock:
                for vm in list(self._vms.values()):
                    if vm.state != VMState.ASSIGNED:
                        continue
                    if vm.assigned_at is None:
                        continue
                    age = now - vm.assigned_at
                    if age <= self._config.vm_idle_timeout:
                        continue

                    logger.warning(
                        "Auto-culling VM %s (assigned %.0fs ago, timeout=%ds)",
                        vm.vm_id, age, self._config.vm_idle_timeout,
                    )

                    # Graceful shutdown: SIGTERM via vsock, then 5s grace
                    vm.transition_to(VMState.STOPPING)
                    try:
                        from .vsock import vsock_request
                        await asyncio.wait_for(
                            vsock_request(
                                vm.vsock_path,
                                {"action": "signal", "signum": 15},
                                timeout=5,
                            ),
                            timeout=5,
                        )
                        await asyncio.sleep(5)
                    except Exception as exc:
                        logger.debug(
                            "Vsock signal to %s failed (continuing): %s", vm.vm_id, exc
                        )

                    await self._destroy_vm(vm)
                    del self._vms[vm.vm_id]
                    AUTO_CULL_TOTAL.inc()
                    culled += 1

            if culled:
                logger.info("Auto-culled %d VM(s), replenishing pool", culled)
                self._update_vm_gauges()
                await self.replenish()

    async def shutdown(self) -> None:
        # ... unchanged ...
```

### `fc_pool_manager/server.py` (modified — key diffs only)

```python
"""Unix domain socket HTTP server for the pool manager API."""

import argparse
import asyncio
import logging
import os
import signal

from aiohttp import web
from prometheus_client import make_aiohttp_handler

from .config import PoolConfig
from .manager import PoolManager

logger = logging.getLogger(__name__)


def create_app(manager: PoolManager) -> web.Application:
    """Create the aiohttp application with all routes."""
    app = web.Application()
    app["manager"] = manager

    app.router.add_post("/api/vms/acquire",       handle_acquire)
    app.router.add_delete("/api/vms/{vm_id}",     handle_release)
    app.router.add_get("/api/vms/{vm_id}/health", handle_health)
    app.router.add_get("/api/pool/status",        handle_pool_status)
    app.router.add_get("/api/metrics",            make_aiohttp_handler())   # NEW

    return app


# ... handle_acquire, handle_release, handle_health, handle_pool_status unchanged ...


async def run_server(config_path: str, socket_path: str) -> None:
    """Start the pool manager and HTTP server."""
    config = PoolConfig.from_yaml(config_path)
    manager = PoolManager(config)

    app = create_app(manager)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, socket_path)
    await site.start()

    os.chmod(socket_path, 0o666)
    logger.info("Pool manager listening on %s", socket_path)

    await manager.replenish()

    health_task = asyncio.create_task(manager.health_check_loop())
    cull_task   = asyncio.create_task(manager.auto_cull_loop())   # NEW

    stop_event = asyncio.Event()

    def on_signal():
        stop_event.set()

    running_loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        running_loop.add_signal_handler(sig, on_signal)

    await stop_event.wait()

    logger.info("Shutting down...")
    health_task.cancel()
    cull_task.cancel()      # NEW
    await manager.shutdown()
    await runner.cleanup()
```

---

## 6. Data Flow

### Acquire Path with Metrics

```
POST /api/vms/acquire
  └─ handle_acquire()
       └─ manager.acquire(vcpu, mem_mib)
            ├─ t0 = event_loop.time()
            ├─ _acquire_inner()
            │    ├─ [fast path] vm.transition_to(ASSIGNED)   → assigned_at = time.monotonic()
            │    │    └─ create_task(replenish())
            │    └─ [on-demand] _boot_vm()
            │         ├─ VMInstance(created_at=time.monotonic())
            │         ├─ [boot…]
            │         ├─ vm.transition_to(IDLE)
            │         ├─ BOOT_DURATION.observe(now - created_at)
            │         └─ vm.transition_to(ASSIGNED)
            ├─ ACQUIRE_DURATION.observe(now - t0)
            ├─ ACQUIRE_TOTAL.labels(result="success").inc()
            └─ _update_vm_gauges()
```

### Auto-Cull Path

```
auto_cull_loop() [every 60s]
  └─ for each ASSIGNED vm where (now - assigned_at) > vm_idle_timeout:
        ├─ vm.transition_to(STOPPING)
        ├─ vsock_request({action:"signal", signum:15})  [best-effort, 5s]
        ├─ asyncio.sleep(5)                             [grace period]
        ├─ _destroy_vm(vm)
        ├─ del _vms[vm_id]
        └─ AUTO_CULL_TOTAL.inc()
  └─ _update_vm_gauges()
  └─ replenish()                                        [after lock released]
```

### Metrics Scrape Path

```
GET /api/metrics
  └─ make_aiohttp_handler()            (prometheus_client built-in)
       └─ prometheus_client.generate_latest()
            └─ serialise default registry → text/plain Prometheus exposition format
```

---

## 7. Error Handling

### Auto-Cull

| Scenario | Behaviour |
|----------|-----------|
| vsock signal times out or fails | Logged at DEBUG; cull proceeds unconditionally |
| `_destroy_vm()` raises | Exception propagates out of cull loop iteration; logged by asyncio task error handler; remaining VMs in scan are not affected (per-VM try/except wraps destroy) |
| VM transitions to STOPPING by another path between scan and cull | `transition_to(STOPPING)` raises `ValueError` (invalid STOPPING→STOPPING); caught and logged; VM skipped |
| `vm_idle_timeout = 0` | `auto_cull_loop()` returns immediately after logging; `cull_task` completes cleanly |

The destroy step inside the cull loop should be wrapped individually:

```python
try:
    vm.transition_to(VMState.STOPPING)
except ValueError:
    logger.debug("VM %s already stopping, skipping cull", vm.vm_id)
    continue
```

### Metrics

| Scenario | Behaviour |
|----------|-----------|
| `prometheus_client` not installed | `ImportError` at startup; fail-fast is correct — metrics are required |
| Registry collision (double-import) | `prometheus_client` raises `ValueError`; prevented by module-level singleton pattern (module is imported once) |
| Metric update throws | `prometheus_client` operations are in-process and do not raise under normal conditions; no special handling needed |
| `/api/metrics` during shutdown | `make_aiohttp_handler()` reads registry at call time; returns last-known state; acceptable |

---

## 8. Testing Plan

### Unit Tests (no KVM required)

#### `tests/test_vm_timestamps.py` (new)

| Test | Validates |
|------|-----------|
| `test_assigned_at_set_on_assign` | `vm.assigned_at` is `None` initially; set to a float after `transition_to(ASSIGNED)` |
| `test_assigned_at_cleared_on_idle` | `vm.assigned_at` reset to `None` after `ASSIGNED → IDLE` |
| `test_assigned_at_unchanged_through_stopping` | `assigned_at` not cleared by `ASSIGNED → STOPPING` transition |
| `test_created_at_immutable` | `created_at` is set at construction; does not change across transitions |

#### `tests/test_auto_cull.py` (new)

Uses the same mock-heavy pattern as `test_pool_manager.py` (mock `_destroy_vm`, mock `vsock_request`).

| Test | Validates |
|------|-----------|
| `test_cull_disabled_when_timeout_zero` | `auto_cull_loop()` returns without culling when `vm_idle_timeout=0` |
| `test_cull_skips_non_assigned` | IDLE and BOOTING VMs are not culled regardless of age |
| `test_cull_skips_fresh_assigned` | ASSIGNED VM within timeout is not culled |
| `test_cull_stale_assigned` | ASSIGNED VM past timeout: `_destroy_vm` called, `AUTO_CULL_TOTAL` incremented, VM removed from `_vms`, `replenish` called |
| `test_cull_vsock_failure_proceeds` | `vsock_request` raises `OSError`; cull still destroys VM |
| `test_cull_multiple_vms` | Two stale VMs in one loop iteration: both culled, replenish called once |
| `test_cull_concurrent_release_safety` | VM transitioned to STOPPING between scan and cull: `ValueError` caught, VM skipped safely |

#### `tests/test_metrics.py` (new)

Uses `prometheus_client.REGISTRY` directly to read metric values after each operation.

| Test | Validates |
|------|-----------|
| `test_pool_max_vms_set_on_init` | `fc_pool_max_vms` equals `config.max_vms` after `PoolManager.__init__()` |
| `test_vm_gauges_after_boot` | `fc_pool_vms_total{state="idle"}` increments after `_boot_vm()` succeeds |
| `test_vm_gauges_after_acquire` | idle gauge decrements, assigned gauge increments after `acquire()` |
| `test_vm_gauges_after_release` | assigned gauge decrements after `release()` |
| `test_acquire_total_success` | `fc_pool_acquire_total{result="success"}` increments on success |
| `test_acquire_total_exhausted` | `fc_pool_acquire_total{result="exhausted"}` increments on pool exhausted |
| `test_acquire_duration_observed` | `fc_pool_acquire_duration_seconds_count` increments after acquire |
| `test_boot_duration_observed` | `fc_pool_boot_duration_seconds_count` increments after boot |
| `test_release_total_increments` | `fc_pool_release_total` increments after `release()` |
| `test_health_check_failure_increments` | `fc_pool_health_check_failures_total` increments when unhealthy VM replaced |
| `test_auto_cull_total_increments` | `fc_pool_auto_cull_total` increments after auto-cull |

#### `tests/test_server_metrics_route.py` (new, or add to `test_server.py`)

| Test | Validates |
|------|-----------|
| `test_metrics_endpoint_200` | `GET /api/metrics` returns 200 |
| `test_metrics_content_type` | `Content-Type: text/plain; version=0.0.4` |
| `test_metrics_body_contains_expected` | Response body contains `fc_pool_vms_total` and `fc_pool_max_vms` |

#### `tests/test_config.py` (extend existing)

| Test | Validates |
|------|-----------|
| `test_vm_idle_timeout_default` | `from_yaml()` with no `vm_idle_timeout` key → `config.vm_idle_timeout == 600` |
| `test_vm_idle_timeout_explicit` | `from_yaml()` with `vm_idle_timeout: 300` → `config.vm_idle_timeout == 300` |
| `test_vm_idle_timeout_zero` | `from_yaml()` with `vm_idle_timeout: 0` → `config.vm_idle_timeout == 0` |

### Integration Tests (KVM host)

Add to `tests/test_integration.py` under `@pytest.mark.integration`:

| Test | Validates |
|------|-----------|
| `test_metrics_endpoint_live` | `GET /api/metrics` via Unix socket returns 200 with `fc_pool_vms_total` in body |
| `test_auto_cull_live` | Set `vm_idle_timeout=5s`, acquire VM, wait 70s (one cull cycle), verify VM gone from pool status and `fc_pool_auto_cull_total` > 0 |

---

## 9. Dependencies

**New in `pyproject.toml`:**

```toml
dependencies = [
    # ... existing ...
    "prometheus_client>=0.20",   # NEW
]
```

Added to main dependencies (not dev-only) — `metrics.py` is imported at pool manager startup.

No other new dependencies. `prometheus_client` has no transitive dependencies.

---

## 10. File Inventory

```
fc_pool_manager/
├── vm.py                MODIFY — add created_at, assigned_at fields; update transition_to()
├── config.py            MODIFY — add vm_idle_timeout field and from_yaml() parsing
├── manager.py           MODIFY — add metrics instrumentation, _acquire_inner(), auto_cull_loop(), _update_vm_gauges()
├── server.py            MODIFY — add /api/metrics route, spawn cull_task
└── metrics.py           NEW    — all prometheus_client metric object definitions

config/
└── fc-pool.yaml         MODIFY — add vm_idle_timeout: 600 under pool:

tests/
├── test_vm_timestamps.py          NEW — VMInstance.created_at / assigned_at unit tests
├── test_auto_cull.py              NEW — auto_cull_loop() unit tests
├── test_metrics.py                NEW — metric value assertion tests
├── test_server.py                 MODIFY — add /api/metrics route tests
├── test_config.py                 MODIFY — add vm_idle_timeout parsing tests
└── test_integration.py            MODIFY — add live metrics and cull integration tests

pyproject.toml           MODIFY — add prometheus_client>=0.20 to dependencies
```
