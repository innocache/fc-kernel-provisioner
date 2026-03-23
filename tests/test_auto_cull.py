import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from fc_pool_manager.config import PoolConfig
from fc_pool_manager.manager import PoolManager
from fc_pool_manager.metrics import AUTO_CULL_TOTAL
from fc_pool_manager.vm import VMInstance, VMState


def make_test_config(tmp_path, vm_idle_timeout: int = 600) -> PoolConfig:
    yaml_content = f"""
pool:
  size: 2
  max_vms: 5
  health_check_interval: 30
  vm_idle_timeout: {vm_idle_timeout}
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


def make_vm(vm_id: str, state: VMState) -> VMInstance:
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
    for metric_family in AUTO_CULL_TOTAL.collect():
        for sample in metric_family.samples:
            if sample.name != name:
                continue
            if labels is not None and sample.labels != labels:
                continue
            return float(sample.value)
    return 0.0


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


async def test_cull_disabled_when_timeout_zero(tmp_path):
    manager = PoolManager(make_test_config(tmp_path, vm_idle_timeout=0))
    manager._destroy_vm = AsyncMock(return_value=None)
    await manager.auto_cull_loop()
    manager._destroy_vm.assert_not_awaited()


async def test_cull_skips_non_assigned(tmp_path):
    manager = PoolManager(make_test_config(tmp_path, vm_idle_timeout=1))
    manager._destroy_vm = AsyncMock(return_value=None)
    manager.replenish = AsyncMock(return_value=None)
    manager._vms["vm-idle"] = make_vm("vm-idle", VMState.IDLE)
    manager._vms["vm-booting"] = make_vm("vm-booting", VMState.BOOTING)

    await run_one_auto_cull_iteration(manager)

    manager._destroy_vm.assert_not_awaited()
    manager.replenish.assert_not_awaited()


async def test_cull_skips_fresh_assigned(tmp_path):
    manager = PoolManager(make_test_config(tmp_path, vm_idle_timeout=600))
    manager._destroy_vm = AsyncMock(return_value=None)
    manager.replenish = AsyncMock(return_value=None)
    vm = make_vm("vm-fresh", VMState.ASSIGNED)
    manager._vms[vm.vm_id] = vm

    await run_one_auto_cull_iteration(manager)

    assert vm.vm_id in manager._vms
    manager._destroy_vm.assert_not_awaited()
    manager.replenish.assert_not_awaited()


async def test_cull_stale_assigned(tmp_path):
    manager = PoolManager(make_test_config(tmp_path, vm_idle_timeout=1))
    manager._destroy_vm = AsyncMock(return_value=None)
    manager.replenish = AsyncMock(return_value=None)
    vm = make_vm("vm-stale", VMState.ASSIGNED)
    vm.assigned_at = time.monotonic() - 999
    manager._vms[vm.vm_id] = vm

    before = metric_value("fc_pool_auto_cull_total")
    with patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock:
        mock_vsock.return_value = {"status": "ok"}
        await run_one_auto_cull_iteration(manager)

    manager._destroy_vm.assert_awaited_once_with(vm)
    manager.replenish.assert_awaited_once()
    assert vm.vm_id not in manager._vms
    assert metric_value("fc_pool_auto_cull_total") == pytest.approx(before + 1)
    assert mock_vsock.await_args is not None
    assert mock_vsock.await_args.args[1] == {"action": "signal", "signum": 15}
    assert mock_vsock.await_args.kwargs["timeout"] == 5


async def test_cull_vsock_failure_proceeds(tmp_path):
    manager = PoolManager(make_test_config(tmp_path, vm_idle_timeout=1))
    manager._destroy_vm = AsyncMock(return_value=None)
    manager.replenish = AsyncMock(return_value=None)
    vm = make_vm("vm-vsock-fail", VMState.ASSIGNED)
    vm.assigned_at = time.monotonic() - 999
    manager._vms[vm.vm_id] = vm

    with patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock:
        mock_vsock.side_effect = OSError("socket down")
        await run_one_auto_cull_iteration(manager)

    manager._destroy_vm.assert_awaited_once_with(vm)
    assert vm.vm_id not in manager._vms


async def test_cull_multiple_vms(tmp_path):
    manager = PoolManager(make_test_config(tmp_path, vm_idle_timeout=1))
    manager._destroy_vm = AsyncMock(return_value=None)
    manager.replenish = AsyncMock(return_value=None)

    vm1 = make_vm("vm-stale-1", VMState.ASSIGNED)
    vm2 = make_vm("vm-stale-2", VMState.ASSIGNED)
    vm1.assigned_at = time.monotonic() - 999
    vm2.assigned_at = time.monotonic() - 999
    manager._vms[vm1.vm_id] = vm1
    manager._vms[vm2.vm_id] = vm2

    before = metric_value("fc_pool_auto_cull_total")
    with patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock:
        mock_vsock.return_value = {"status": "ok"}
        await run_one_auto_cull_iteration(manager)

    assert manager._destroy_vm.await_count == 2
    manager.replenish.assert_awaited_once()
    assert not manager._vms
    assert metric_value("fc_pool_auto_cull_total") == pytest.approx(before + 2)


async def test_cull_concurrent_release_safety(tmp_path):
    manager = PoolManager(make_test_config(tmp_path, vm_idle_timeout=1))
    manager._destroy_vm = AsyncMock(return_value=None)
    manager.replenish = AsyncMock(return_value=None)

    vm = make_vm("vm-race", VMState.ASSIGNED)
    vm.assigned_at = time.monotonic() - 999
    manager._vms[vm.vm_id] = vm

    with patch.object(vm, "transition_to", side_effect=ValueError("already stopping")):
        await run_one_auto_cull_iteration(manager)

    manager._destroy_vm.assert_not_awaited()
    manager.replenish.assert_not_awaited()
    assert vm.vm_id in manager._vms
