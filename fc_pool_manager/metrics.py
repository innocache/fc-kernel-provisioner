"""Prometheus metric definitions for the pool manager.

All metric objects are module-level singletons registered with the
default prometheus_client registry on import.
"""

from prometheus_client import Counter, Gauge, Histogram

_DURATION_BUCKETS = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0)

# ── Gauges ───────────────────────────────────────────────────────────────

POOL_VMS_TOTAL = Gauge(
    "fc_pool_vms_total",
    "Current number of VMs in each lifecycle state",
    labelnames=["state"],
)

POOL_MAX_VMS = Gauge(
    "fc_pool_max_vms",
    "Configured hard ceiling on total VMs",
)

# ── Histograms ───────────────────────────────────────────────────────────

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

# ── Counters ─────────────────────────────────────────────────────────────

ACQUIRE_TOTAL = Counter(
    "fc_pool_acquire_total",
    "Total acquire() calls by outcome",
    labelnames=["result"],
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
