#!/usr/bin/env python3
"""Benchmark: full VM boot vs snapshot restore.

Run on a KVM host with Firecracker + rootfs configured:
    sudo uv run python scripts/benchmark_snapshot.py --config config/fc-pool.yaml

Measures:
  1. Full boot (cold): jail → configure → start → guest agent ready
  2. Golden snapshot creation: full boot → pause → snapshot/create
  3. Snapshot restore (warm): jail → load_snapshot → resume → guest agent ready
"""

import argparse
import asyncio
import statistics
import time

from fc_pool_manager.config import PoolConfig
from fc_pool_manager.manager import PoolManager


async def time_full_boot(manager: PoolManager) -> float:
    t0 = time.monotonic()
    vm = await manager._boot_vm(use_snapshot=False)
    elapsed = time.monotonic() - t0
    await manager._destroy_vm(vm)
    manager._vms.pop(vm.vm_id, None)
    return elapsed


async def time_snapshot_restore(manager: PoolManager) -> float:
    t0 = time.monotonic()
    vm = await manager._boot_vm(use_snapshot=True)
    elapsed = time.monotonic() - t0
    await manager._destroy_vm(vm)
    manager._vms.pop(vm.vm_id, None)
    return elapsed


async def run_benchmark(config_path: str, warmup: int, iterations: int) -> None:
    config = PoolConfig.from_yaml(config_path)
    manager = PoolManager(config)

    print("=" * 60)
    print("Firecracker Snapshot Benchmark")
    print("=" * 60)
    print(f"Config: {config_path}")
    print(f"Iterations: {iterations} (warmup: {warmup})")
    print()

    # Phase 1: Full boot baseline
    print("Phase 1: Full boot (cold)")
    print("-" * 40)
    full_times = []
    for i in range(warmup + iterations):
        elapsed = await time_full_boot(manager)
        label = "(warmup)" if i < warmup else ""
        print(f"  Run {i + 1}: {elapsed * 1000:.1f}ms {label}")
        if i >= warmup:
            full_times.append(elapsed)

    # Phase 2: Create golden snapshot
    print()
    print("Phase 2: Creating golden snapshot...")
    print("-" * 40)
    t0 = time.monotonic()
    await manager.create_golden_snapshot()
    snap_create_time = time.monotonic() - t0
    print(f"  Golden snapshot created in {snap_create_time * 1000:.1f}ms")

    # Phase 3: Snapshot restore
    print()
    print("Phase 3: Snapshot restore (warm)")
    print("-" * 40)
    restore_times = []
    for i in range(warmup + iterations):
        elapsed = await time_snapshot_restore(manager)
        label = "(warmup)" if i < warmup else ""
        print(f"  Run {i + 1}: {elapsed * 1000:.1f}ms {label}")
        if i >= warmup:
            restore_times.append(elapsed)

    # Results
    print()
    print("=" * 60)
    print("Results")
    print("=" * 60)
    print()

    def stats(times):
        return {
            "mean": statistics.mean(times) * 1000,
            "median": statistics.median(times) * 1000,
            "stdev": statistics.stdev(times) * 1000 if len(times) > 1 else 0,
            "min": min(times) * 1000,
            "max": max(times) * 1000,
        }

    full = stats(full_times)
    restore = stats(restore_times)

    print(f"{'Metric':<20} {'Full Boot':>12} {'Snapshot':>12} {'Speedup':>10}")
    print("-" * 56)
    print(f"{'Mean':<20} {full['mean']:>10.1f}ms {restore['mean']:>10.1f}ms {full['mean'] / restore['mean']:>8.1f}x")
    print(f"{'Median':<20} {full['median']:>10.1f}ms {restore['median']:>10.1f}ms {full['median'] / restore['median']:>8.1f}x")
    print(f"{'Min':<20} {full['min']:>10.1f}ms {restore['min']:>10.1f}ms")
    print(f"{'Max':<20} {full['max']:>10.1f}ms {restore['max']:>10.1f}ms")
    print(f"{'Stdev':<20} {full['stdev']:>10.1f}ms {restore['stdev']:>10.1f}ms")
    print()
    print(f"Golden snapshot creation: {snap_create_time * 1000:.1f}ms (one-time cost)")
    print()

    target_met = restore["median"] < 50
    print(f"Target (<50ms restore): {'✅ MET' if target_met else '❌ NOT MET'} (median={restore['median']:.1f}ms)")

    # Cleanup
    await manager.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Benchmark snapshot restore vs full boot")
    parser.add_argument("--config", default="config/fc-pool.yaml")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    args = parser.parse_args()

    asyncio.run(run_benchmark(args.config, args.warmup, args.iterations))


if __name__ == "__main__":
    main()
