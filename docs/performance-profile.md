# Performance Profile: Execution API

**Note:** This document has been superseded by [docs/performance-enhancements.md](performance-enhancements.md) which consolidates all performance work.

This document captures benchmark results from the Execution API profiler, covering in-session code execution latency, one-shot execution overhead, and VM cold-start costs. All numbers are from actual measurements — no synthetic estimates.

## Key Findings

| Metric | Value | Notes |
|--------|-------|-------|
| Warm in-session execution floor | 49ms | T1 trivial code, pure API overhead |
| Warm in-session compute | 168ms | CPU-bound Python, ~120ms kernel time |
| One-shot execution (create+run+destroy) | 3,862ms | ~80x slower than warm session |
| Cold VM boot | 1,764ms | Full ext4 boot |
| Snapshot restore (XFS reflink) | 155ms | 11.4x speedup over cold boot |

---

## Test Environment

### Hardware
- Remote KVM host (bare-metal hypervisor, hardware virtualisation enabled)

### Software
| Component | Version |
|-----------|---------|
| Firecracker | v1.6 |
| Python (guest) | 3.12 |
| Filesystem | XFS (with reflink support) |
| API framework | FastAPI + httpx |

### Pool Configuration
- Pre-warmed session pool with snapshot-restored VMs
- Kernel Gateway running inside each microVM
- WebSocket transport for code execution

---

## Code Execution Latency

All measurements taken against an **in-session, warm kernel** — the VM is already running and the Jupyter kernel is idle, ready to accept requests.

### Tier Results

| Tier | Code | p50 | min | max | Notes |
|------|------|-----|-----|-----|-------|
| T1: Trivial | `print("hello")` | 49ms | 48ms | 49ms | Pure API overhead |
| T2: Compute | `sum(range(10**7))` | 168ms | 162ms | 185ms | CPU-bound |
| T3: Data | pandas groupby+describe on 1K rows | 49ms (warm) | 49ms | 667ms | First run 667ms (import pandas), subsequent 49ms |
| T4: Visualization | matplotlib savefig | 90ms (warm) | 90ms | 940ms | First run 940ms (import matplotlib), subsequent 90ms |

### Analysis

**T1 — API floor (49ms)**  
`print("hello")` does no meaningful computation. The 49ms represents the irreducible overhead of the full round-trip: HTTP request → FastAPI routing → WebSocket send → kernel execute → WebSocket receive → output parsing → HTTP response. This is the minimum latency any request can achieve regardless of what code runs inside.

**T2 — Kernel computation time (~120ms)**  
`sum(range(10**7))` is CPU-bound Python. Subtracting the T1 floor (49ms) from the T2 p50 (168ms) isolates ~120ms of actual kernel computation time for iterating 10 million integers. The tight min/max spread (162–185ms) confirms stable, predictable CPU performance.

**T3/T4 — First-run import penalty**  
Python's import system loads and compiles modules on first use. This is a one-time cost per kernel session:

| Library | First-run penalty | Warm p50 | Import cost |
|---------|-------------------|----------|-------------|
| pandas | 667ms | 49ms | ~618ms |
| matplotlib | 940ms | 90ms | ~850ms |

After the first execution, subsequent calls drop to warm-session latency because the modules remain loaded in the kernel process. **Sessions amortise this cost across all subsequent executions** — a session that runs 10 pandas operations pays the 618ms import tax once, not ten times.

---

## One-Shot vs Session-Based Execution

| Mode | p50 | min | max |
|------|-----|-----|-----|
| In-session (warm kernel) | 49ms | 48ms | 49ms |
| One-shot (create + execute + destroy) | 3,862ms | 3,852ms | 3,873ms |

**One-shot is ~80x slower than a warm session.**

The 3,862ms one-shot cost breaks down across the full session lifecycle:

1. **VM acquisition** — pull a pre-warmed VM from the pool (or boot one cold)
2. **Kernel boot** — Jupyter Kernel Gateway starts, kernel process spawns, `kernel_info` handshake completes (~3.8s is dominated by this wait)
3. **WebSocket setup** — establish the execution channel
4. **Code execution** — the actual work (49ms at T1)
5. **Session teardown** — WebSocket close, VM reclaim

The execution itself is a rounding error. The session lifecycle is the cost. This is why one-shot execution is unsuitable for any latency-sensitive workload.

---

## Cold Start Optimization

VM boot time is a major contributor to one-shot latency and pool replenishment speed. See [docs/snapshot-optimization.md](snapshot-optimization.md) for full methodology and analysis.

### Boot Method Comparison

| Method | Median Boot | Speedup |
|--------|-------------|---------|
| Cold boot (ext4) | 1,764ms | baseline |
| Snapshot + ext4 | 446ms | 4.0x |
| Snapshot + XFS reflink | 155ms | 11.4x |

XFS reflink snapshots achieve an **11.4x speedup** over cold boot by copy-on-write cloning the root filesystem rather than copying it, and by restoring a pre-booted Firecracker memory snapshot rather than running the Linux boot sequence.

### Snapshot Restore Breakdown (XFS)

| Phase | Time | % of total |
|-------|------|------------|
| prepare_jail (reflink) | 6ms | 4% |
| snapshot_restore (jailer + load) | 116ms | 71% |
| wait_guest_agent | 16ms | 10% |
| Other (TAP, iptables, etc.) | 24ms | 15% |
| **Total** | **162ms** | **100%** |

The dominant cost (71%) is the Firecracker jailer startup and memory snapshot load. The filesystem clone itself is only 6ms thanks to XFS reflink.

---

## Bottleneck Analysis

Where time is spent across the stack:

| Component | Latency | Notes |
|-----------|---------|-------|
| FastAPI + httpx (API overhead) | ~15ms | Routing, serialisation, HTTP transport |
| WebSocket round-trip | ~20ms | Send + receive over loopback |
| Kernel execution | 10ms – 5s+ | Varies by workload |
| Python import tax | 600–900ms | One-time per session per library |
| Session create | ~3,800ms | Dominated by Kernel Gateway `kernel_info` wait |
| VM boot (cold) | ~1,764ms | Full Linux boot sequence |
| VM boot (snapshot, XFS) | ~155ms | Firecracker memory restore + reflink clone |

The T1 floor of 49ms = API overhead (~15ms) + WebSocket round-trip (~20ms) + minimal kernel execution (~10ms) + output parsing (~4ms).

---

## Recommendations

Based on the measured data:

### 1. Use session-based execution, not one-shot
One-shot costs 3,862ms vs 49ms for a warm session — an 80x difference. Any workload running more than one execution should use a persistent session.

### 2. Pre-warm sessions for latency-sensitive workloads
Session creation is ~3.8s. Pre-warming a pool of ready sessions eliminates this from the critical path entirely. The pool should be sized to expected concurrency.

### 3. Keep sessions alive to benefit from import caching
The first pandas import costs 618ms; the first matplotlib import costs 850ms. Sessions that stay alive amortise these costs. Avoid aggressive session timeouts for workloads that use heavy libraries.

### 4. Use XFS reflink for VM provisioning
Snapshot restore with XFS reflink reduces VM boot from 1,764ms to 155ms (11.4x). This directly improves pool replenishment speed and reduces one-shot latency. See [docs/snapshot-optimization.md](snapshot-optimization.md) for setup details.

---

## Running the Benchmark

Reproduce these results with the included benchmark scripts:

```bash
# Execution API benchmark (T1–T4 tiers + one-shot)
sudo uv run python scripts/benchmark_api.py --url http://localhost:8000 --iterations 5

# Snapshot boot benchmark (cold vs snapshot vs XFS reflink)
sudo uv run python scripts/benchmark_snapshot.py --config config/fc-pool.yaml
```

`sudo` is required for Firecracker jailer operations. Run on the KVM host directly — results over SSH tunnels will include additional network latency not present in the numbers above.
