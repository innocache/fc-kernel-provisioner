import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fc_pool_manager.config import PoolConfig
from fc_pool_manager.manager import PoolManager
from fc_pool_manager.metrics import (
    ACQUIRE_DURATION,
    ACQUIRE_TOTAL,
    AUTO_CULL_TOTAL,
    BOOT_DURATION,
    HEALTH_CHECK_FAILURES_TOTAL,
    POOL_MAX_VMS,
    POOL_VMS_TOTAL,
    RELEASE_TOTAL,
)
from fc_pool_manager.vm import VMInstance, VMState


def make_test_config(tmp_path, **pool_overrides) -> PoolConfig:
    yaml_content = f"""
pool:
  size: {pool_overrides.get('size', 2)}
  max_vms: {pool_overrides.get('max_vms', 5)}
  health_check_interval: {pool_overrides.get('health_check_interval', 30)}
  vm_idle_timeout: {pool_overrides.get('vm_idle_timeout', 600)}
vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "console=ttyS0 ip={{vm_ip}}::172.16.0.1:255.255.255.0::eth0:off init=/init"
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


def make_vm(vm_id: str, state: VMState = VMState.IDLE) -> VMInstance:
    vm = VMInstance(
        vm_id=vm_id,
        short_id=vm_id.replace("vm-", ""),
        ip="172.16.0.2",
        cid=3,
        tap_name=f"tap-{vm_id.replace('vm-', '')}",
        mac="AA:FC:00:00:00:02",
        jail_path="/tmp/jail",
        vsock_path="/tmp/jail/v.sock",
    )
    if state != VMState.BOOTING:
        vm.transition_to(VMState.IDLE)
        if state == VMState.ASSIGNED:
            vm.transition_to(VMState.ASSIGNED)
        elif state == VMState.STOPPING:
            vm.transition_to(VMState.STOPPING)
    return vm


def metric_value(name: str, labels: dict[str, str] | None = None) -> float:
    collectors = (
        POOL_VMS_TOTAL,
        POOL_MAX_VMS,
        ACQUIRE_DURATION,
        BOOT_DURATION,
        ACQUIRE_TOTAL,
        RELEASE_TOTAL,
        HEALTH_CHECK_FAILURES_TOTAL,
        AUTO_CULL_TOTAL,
    )
    for collector in collectors:
        for metric_family in collector.collect():
            for sample in metric_family.samples:
                if sample.name != name:
                    continue
                if labels is not None and sample.labels != labels:
                    continue
                return float(sample.value)
    return 0.0


async def run_one_health_check_iteration(manager: PoolManager) -> None:
    ticks = 0

    async def fake_sleep(_: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= 2:
            raise asyncio.CancelledError

    with patch("fc_pool_manager.manager.asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await manager.health_check_loop()


async def run_one_auto_cull_iteration(manager: PoolManager) -> None:
    interval_ticks = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal interval_ticks
        if seconds == 5:
            return
        interval_ticks += 1
        if interval_ticks >= 2:
            raise asyncio.CancelledError

    with patch("fc_pool_manager.manager._CULL_INTERVAL", 0):
        with patch("fc_pool_manager.manager.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await manager.auto_cull_loop()


async def boot_vm_with_mocks(manager: PoolManager) -> VMInstance:
    manager._run_subprocess = AsyncMock(return_value=None)
    manager._wait_for_socket = AsyncMock(return_value=None)
    manager._network.create_tap = AsyncMock(return_value=None)
    manager._network.apply_vm_rules = AsyncMock(return_value=None)
    manager._network.remove_vm_rules = AsyncMock(return_value=None)

    fake_proc = MagicMock()
    fake_proc.returncode = None

    api = MagicMock()
    api.configure_machine = AsyncMock(return_value=None)
    api.configure_boot_source = AsyncMock(return_value=None)
    api.configure_drive = AsyncMock(return_value=None)
    api.configure_network = AsyncMock(return_value=None)
    api.configure_vsock = AsyncMock(return_value=None)
    api.configure_entropy = AsyncMock(return_value=None)
    api.start = AsyncMock(return_value=None)

    with patch("fc_pool_manager.manager.os.makedirs", return_value=None):
        with patch("fc_pool_manager.manager.os.path.exists", return_value=True):
            with patch("fc_pool_manager.manager.os.link", return_value=None):
                with patch("fc_pool_manager.manager.os.chown", return_value=None):
                    with patch(
                        "fc_pool_manager.manager.asyncio.create_subprocess_exec",
                        new_callable=AsyncMock,
                    ) as mock_subprocess:
                        mock_subprocess.return_value = fake_proc
                        with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=api):
                            with patch(
                                "fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock
                            ) as mock_vsock:
                                mock_vsock.return_value = {"status": "alive"}
                                return await manager._boot_vm()


async def test_pool_max_vms_set_on_init(tmp_path):
    config = make_test_config(tmp_path, max_vms=17)
    PoolManager(config)
    assert metric_value("fc_pool_max_vms") == 17


async def test_vm_gauges_after_boot(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    before_idle = metric_value("fc_pool_vms_total", {"state": "idle"})
    await boot_vm_with_mocks(manager)
    after_idle = metric_value("fc_pool_vms_total", {"state": "idle"})
    assert after_idle == pytest.approx(before_idle + 1)


async def test_vm_gauges_after_acquire(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    vm = make_vm("vm-acq", VMState.IDLE)
    manager._vms[vm.vm_id] = vm
    manager.replenish = AsyncMock(return_value=None)

    idle_before = metric_value("fc_pool_vms_total", {"state": "idle"})
    assigned_before = metric_value("fc_pool_vms_total", {"state": "assigned"})

    await manager.acquire(vcpu=1, mem_mib=512)

    idle_after = metric_value("fc_pool_vms_total", {"state": "idle"})
    assigned_after = metric_value("fc_pool_vms_total", {"state": "assigned"})
    assert idle_after == pytest.approx(idle_before - 1)
    assert assigned_after == pytest.approx(assigned_before + 1)


async def test_vm_gauges_after_release(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    vm = make_vm("vm-rel", VMState.ASSIGNED)
    manager._vms[vm.vm_id] = vm
    manager._update_vm_gauges()

    assigned_before = metric_value("fc_pool_vms_total", {"state": "assigned"})
    idle_before = metric_value("fc_pool_vms_total", {"state": "idle"})

    with patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock:
        mock_vsock.return_value = {"status": "ok"}
        await manager.release(vm.vm_id, destroy=False)

    assigned_after = metric_value("fc_pool_vms_total", {"state": "assigned"})
    idle_after = metric_value("fc_pool_vms_total", {"state": "idle"})
    assert assigned_after == pytest.approx(assigned_before - 1)
    assert idle_after == pytest.approx(idle_before + 1)


async def test_acquire_total_success(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    manager._vms["vm-ok"] = make_vm("vm-ok", VMState.IDLE)
    manager.replenish = AsyncMock(return_value=None)
    before = metric_value("fc_pool_acquire_total", {"result": "success"})
    await manager.acquire(vcpu=1, mem_mib=512)
    assert metric_value("fc_pool_acquire_total", {"result": "success"}) == pytest.approx(before + 1)


async def test_acquire_total_invalid_request(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    before = metric_value("fc_pool_acquire_total", {"result": "invalid_request"})
    with pytest.raises(ValueError, match="do not match pool profile"):
        await manager.acquire(vcpu=99, mem_mib=99)
    assert metric_value("fc_pool_acquire_total", {"result": "invalid_request"}) == pytest.approx(before + 1)


async def test_acquire_total_exhausted(tmp_path):
    manager = PoolManager(make_test_config(tmp_path, max_vms=1))
    vm = make_vm("vm-full", VMState.ASSIGNED)
    manager._vms[vm.vm_id] = vm
    before = metric_value("fc_pool_acquire_total", {"result": "exhausted"})
    with pytest.raises(RuntimeError, match="pool_exhausted"):
        await manager.acquire(vcpu=1, mem_mib=512)
    assert metric_value("fc_pool_acquire_total", {"result": "exhausted"}) == pytest.approx(before + 1)


async def test_acquire_duration_observed(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    manager._vms["vm-dur"] = make_vm("vm-dur", VMState.IDLE)
    manager.replenish = AsyncMock(return_value=None)
    before = metric_value("fc_pool_acquire_duration_seconds_count")
    await manager.acquire(vcpu=1, mem_mib=512)
    assert metric_value("fc_pool_acquire_duration_seconds_count") == pytest.approx(before + 1)


async def test_boot_duration_observed(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    before = metric_value("fc_pool_boot_duration_seconds_count")
    await boot_vm_with_mocks(manager)
    assert metric_value("fc_pool_boot_duration_seconds_count") == pytest.approx(before + 1)


async def test_release_total_increments(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    vm = make_vm("vm-release", VMState.ASSIGNED)
    manager._vms[vm.vm_id] = vm
    manager._destroy_vm = AsyncMock(return_value=None)
    before = metric_value("fc_pool_release_total")
    await manager.release(vm.vm_id, destroy=True)
    assert metric_value("fc_pool_release_total") == pytest.approx(before + 1)


async def test_health_check_failure_increments(tmp_path):
    manager = PoolManager(make_test_config(tmp_path, health_check_interval=1))
    vm = make_vm("vm-unhealthy", VMState.IDLE)
    manager._vms[vm.vm_id] = vm
    manager.is_alive = AsyncMock(return_value={"alive": False})
    manager._destroy_vm = AsyncMock(return_value=None)
    manager.replenish = AsyncMock(return_value=None)

    before = metric_value("fc_pool_health_check_failures_total")
    await run_one_health_check_iteration(manager)
    assert metric_value("fc_pool_health_check_failures_total") == pytest.approx(before + 1)


async def test_auto_cull_total_increments(tmp_path):
    manager = PoolManager(make_test_config(tmp_path, vm_idle_timeout=1))
    vm = make_vm("vm-cull-metric", VMState.ASSIGNED)
    vm.assigned_at = time.monotonic() - 999
    manager._vms[vm.vm_id] = vm
    manager._destroy_vm = AsyncMock(return_value=None)

    before = metric_value("fc_pool_auto_cull_total")
    with patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock:
        mock_vsock.return_value = {"status": "ok"}
        await run_one_auto_cull_iteration(manager)

    assert metric_value("fc_pool_auto_cull_total") == pytest.approx(before + 1)
