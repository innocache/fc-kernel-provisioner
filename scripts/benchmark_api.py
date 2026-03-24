#!/usr/bin/env python3
"""Performance profiler for the Execution API.

Measures latency (p50/p95/p99) across code execution tiers, dashboard
launch tiers, and concurrent session load. Requires a running Execution
API server + Kernel Gateway + pool manager.

Usage:
    uv run python scripts/benchmark_api.py [--url http://localhost:8000] [--iterations 5]
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

# ── Workload Definitions ─────────────────────────────────────────────────

CODE_TIERS = {
    "T1_trivial": 'print("hello")',
    "T2_compute": "print(sum(range(10**7)))",
    "T3_data": (
        "import pandas as pd, io\n"
        "csv = 'a,b\\n' + '\\n'.join(f'{i},{i*2}' for i in range(10000))\n"
        "df = pd.read_csv(io.StringIO(csv))\n"
        "print(df.groupby(df['a'] % 10).describe().to_string()[:500])"
    ),
    "T4_viz": (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "import numpy as np\n"
        "x = np.linspace(0, 10, 1000)\n"
        "fig, axes = plt.subplots(2, 2, figsize=(10, 8))\n"
        "for ax in axes.flat:\n"
        "    ax.plot(x, np.sin(x + np.random.rand()))\n"
        "plt.tight_layout()\n"
        "plt.savefig('/tmp/bench.png')\n"
        "print('plot saved')"
    ),
    "T5_heavy": (
        "import numpy as np\n"
        "from scipy import optimize\n"
        "def rosenbrock(x):\n"
        "    return sum(100*(x[1:]-x[:-1]**2)**2 + (1-x[:-1])**2)\n"
        "x0 = np.random.randn(50)\n"
        "result = optimize.minimize(rosenbrock, x0, method='L-BFGS-B')\n"
        "print(f'converged={result.success} iterations={result.nit} fval={result.fun:.6f}')"
    ),
}

DASHBOARD_TIERS = {
    "D1_static": 'import panel as pn\npn.panel("hello dashboard").servable()',
    "D2_table": (
        "import panel as pn, pandas as pd, numpy as np\n"
        "df = pd.DataFrame(np.random.randn(1000, 5), columns=list('ABCDE'))\n"
        "pn.widgets.Tabulator(df, page_size=20).servable()"
    ),
    "D3_interactive": (
        "import panel as pn, pandas as pd, numpy as np\n"
        "df = pd.DataFrame(np.random.randn(500, 4), columns=list('ABCD'))\n"
        "col_sel = pn.widgets.Select(name='Column', options=list(df.columns))\n"
        "bins_sl = pn.widgets.IntSlider(name='Bins', start=5, end=50, value=20)\n"
        "@pn.depends(col_sel, bins_sl)\n"
        "def plot(col, bins):\n"
        "    return df[col].plot.hist(bins=bins, title=f'{col} distribution')\n"
        "pn.Column(col_sel, bins_sl, plot, pn.widgets.Tabulator(df, page_size=10)).servable()"
    ),
}


# ── Data Collection ──────────────────────────────────────────────────────

@dataclass
class TimingResult:
    name: str
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0

    @property
    def count(self) -> int:
        return len(self.latencies_ms)

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        idx = int(len(s) * p)
        return s[min(idx, len(s) - 1)]

    def stats(self) -> dict:
        if not self.latencies_ms:
            return {"p50": 0, "p95": 0, "p99": 0, "mean": 0, "min": 0, "max": 0, "stdev": 0}
        return {
            "p50": self.percentile(0.50),
            "p95": self.percentile(0.95),
            "p99": self.percentile(0.99),
            "mean": statistics.mean(self.latencies_ms),
            "min": min(self.latencies_ms),
            "max": max(self.latencies_ms),
            "stdev": statistics.stdev(self.latencies_ms) if len(self.latencies_ms) > 1 else 0,
        }


# ── Benchmark Harness ────────────────────────────────────────────────────

async def create_session(client: httpx.AsyncClient) -> str:
    resp = await client.post("/sessions")
    resp.raise_for_status()
    return resp.json()["session_id"]


async def delete_session(client: httpx.AsyncClient, sid: str) -> None:
    try:
        await client.delete(f"/sessions/{sid}")
    except Exception:
        pass


async def bench_session_create(client: httpx.AsyncClient, n: int) -> TimingResult:
    result = TimingResult(name="session_create")
    for _ in range(n):
        t0 = time.monotonic()
        try:
            sid = await create_session(client)
            result.latencies_ms.append((time.monotonic() - t0) * 1000)
            await delete_session(client, sid)
        except Exception:
            result.errors += 1
    return result


async def bench_code_tier(
    client: httpx.AsyncClient, tier_name: str, code: str, n: int,
) -> TimingResult:
    result = TimingResult(name=f"execute_{tier_name}")
    sid = await create_session(client)
    try:
        for _ in range(n):
            t0 = time.monotonic()
            try:
                resp = await client.post(
                    f"/sessions/{sid}/execute",
                    json={"code": code},
                    timeout=60.0,
                )
                elapsed = (time.monotonic() - t0) * 1000
                if resp.status_code == 200:
                    result.latencies_ms.append(elapsed)
                else:
                    result.errors += 1
            except Exception:
                result.errors += 1
    finally:
        await delete_session(client, sid)
    return result


async def bench_one_shot(
    client: httpx.AsyncClient, tier_name: str, code: str, n: int,
) -> TimingResult:
    result = TimingResult(name=f"oneshot_{tier_name}")
    for _ in range(n):
        t0 = time.monotonic()
        try:
            resp = await client.post(
                "/execute", json={"code": code}, timeout=60.0,
            )
            elapsed = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                result.latencies_ms.append(elapsed)
            else:
                result.errors += 1
        except Exception:
            result.errors += 1
    return result


async def bench_dashboard_tier(
    client: httpx.AsyncClient, tier_name: str, code: str, n: int,
) -> TimingResult:
    result = TimingResult(name=f"dashboard_{tier_name}")
    for _ in range(n):
        sid = await create_session(client)
        try:
            t0 = time.monotonic()
            resp = await client.post(
                f"/sessions/{sid}/dashboard",
                json={"code": code},
                timeout=60.0,
            )
            elapsed = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                result.latencies_ms.append(elapsed)
            else:
                result.errors += 1
        except Exception:
            result.errors += 1
        finally:
            await delete_session(client, sid)
    return result


async def bench_concurrent_sessions(
    client: httpx.AsyncClient, num_sessions: int, executions_per: int,
) -> TimingResult:
    result = TimingResult(name=f"concurrent_{num_sessions}s_{executions_per}e")

    async def session_worker():
        sid = await create_session(client)
        try:
            for _ in range(executions_per):
                t0 = time.monotonic()
                try:
                    resp = await client.post(
                        f"/sessions/{sid}/execute",
                        json={"code": 'print("hello")'},
                        timeout=60.0,
                    )
                    elapsed = (time.monotonic() - t0) * 1000
                    if resp.status_code == 200:
                        result.latencies_ms.append(elapsed)
                    else:
                        result.errors += 1
                except Exception:
                    result.errors += 1
        finally:
            await delete_session(client, sid)

    await asyncio.gather(*(session_worker() for _ in range(num_sessions)))
    return result


async def bench_session_lifecycle(client: httpx.AsyncClient, n: int) -> TimingResult:
    result = TimingResult(name="full_lifecycle")
    for _ in range(n):
        t0 = time.monotonic()
        try:
            sid = await create_session(client)
            await client.post(
                f"/sessions/{sid}/execute",
                json={"code": 'print("hello")'},
                timeout=60.0,
            )
            await client.delete(f"/sessions/{sid}")
            result.latencies_ms.append((time.monotonic() - t0) * 1000)
        except Exception:
            result.errors += 1
    return result


# ── Report ───────────────────────────────────────────────────────────────

def print_results(results: list[TimingResult]) -> None:
    header = f"{'Benchmark':<35} {'n':>4} {'err':>4} {'p50':>8} {'p95':>8} {'p99':>8} {'mean':>8} {'stdev':>8}"
    print()
    print("=" * len(header))
    print(header)
    print("=" * len(header))

    for r in results:
        s = r.stats()
        print(
            f"{r.name:<35} {r.count:>4} {r.errors:>4} "
            f"{s['p50']:>7.0f}ms {s['p95']:>7.0f}ms {s['p99']:>7.0f}ms "
            f"{s['mean']:>7.0f}ms {s['stdev']:>7.0f}ms"
        )

    print("=" * len(header))


def save_results(results: list[TimingResult], path: str) -> None:
    data = {}
    for r in results:
        data[r.name] = {
            "count": r.count,
            "errors": r.errors,
            "latencies_ms": r.latencies_ms,
            **r.stats(),
        }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nResults saved to {path}")


# ── Main ─────────────────────────────────────────────────────────────────

async def run_profiler(url: str, iterations: int, output: str | None) -> None:
    results: list[TimingResult] = []

    async with httpx.AsyncClient(base_url=url, timeout=120.0) as client:
        # Verify API is up
        try:
            resp = await client.get("/sessions")
            resp.raise_for_status()
        except Exception as e:
            print(f"ERROR: Cannot reach Execution API at {url}: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Execution API: {url}")
        print(f"Iterations per benchmark: {iterations}")
        print()

        # --- Session Create ---
        print("[1/7] Session create...")
        results.append(await bench_session_create(client, iterations))

        # --- Code Execution Tiers ---
        print("[2/7] Code execution tiers (in-session)...")
        for tier, code in CODE_TIERS.items():
            print(f"  {tier}...")
            results.append(await bench_code_tier(client, tier, code, iterations))

        # --- One-Shot ---
        print("[3/7] One-shot execution (T1 only)...")
        results.append(await bench_one_shot(client, "T1_trivial", CODE_TIERS["T1_trivial"], iterations))

        # --- Full Lifecycle ---
        print("[4/7] Full lifecycle (create + execute + delete)...")
        results.append(await bench_session_lifecycle(client, iterations))

        # --- Dashboard Tiers ---
        print("[5/7] Dashboard launch tiers...")
        for tier, code in DASHBOARD_TIERS.items():
            print(f"  {tier}...")
            results.append(await bench_dashboard_tier(client, tier, code, iterations))

        # --- Concurrent Light ---
        print("[6/7] Concurrent: 5 sessions × 3 executions...")
        results.append(await bench_concurrent_sessions(client, 5, 3))

        # --- Concurrent Medium ---
        print("[7/7] Concurrent: 10 sessions × 5 executions...")
        results.append(await bench_concurrent_sessions(client, 10, 5))

    print_results(results)

    if output:
        save_results(results, output)


def main():
    parser = argparse.ArgumentParser(description="Profile Execution API performance")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", default=None, help="JSON output path")
    args = parser.parse_args()

    asyncio.run(run_profiler(args.url, args.iterations, args.output))


if __name__ == "__main__":
    main()
