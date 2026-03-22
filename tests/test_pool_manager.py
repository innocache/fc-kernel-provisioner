"""Tests for pool manager core logic (mocked, no real VMs)."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from fc_pool_manager.manager import PoolManager
from fc_pool_manager.config import PoolConfig
from fc_pool_manager.vm import VMInstance, VMState


def make_test_config(tmp_path) -> PoolConfig:
    yaml_content = """
pool:
  size: 2
  max_vms: 5
  replenish_threshold: 1
  health_check_interval: 30
vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "console=ttyS0 ip={vm_ip}::172.16.0.1:255.255.255.0::eth0:off init=/init"
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


def make_idle_vm(vm_id="vm-test1234", ip="172.16.0.2", cid=3):
    vm = VMInstance(
        vm_id=vm_id,
        short_id=vm_id.replace("vm-", ""),
        ip=ip,
        cid=cid,
        tap_name=f"tap-{vm_id.replace('vm-', '')}",
        mac="AA:FC:00:00:00:02",
        jail_path="/tmp/jail",
        vsock_path="/tmp/jail/v.sock",
    )
    vm.transition_to(VMState.IDLE)
    return vm


class TestPoolManagerAcquireRelease:
    @pytest.fixture
    def manager(self, tmp_path):
        config = make_test_config(tmp_path)
        mgr = PoolManager(config)
        mgr._boot_vm = AsyncMock(return_value=None)
        mgr._destroy_vm = AsyncMock(return_value=None)
        return mgr

    async def test_acquire_from_idle_pool(self, manager):
        vm = make_idle_vm()
        manager._vms["vm-test1234"] = vm

        result = await manager.acquire(vcpu=1, mem_mib=512)
        assert result["id"] == "vm-test1234"
        assert result["ip"] == "172.16.0.2"
        assert manager._vms["vm-test1234"].state == VMState.ASSIGNED

    async def test_acquire_fails_on_resource_mismatch(self, manager):
        manager._vms["vm-test1234"] = make_idle_vm()
        with pytest.raises(ValueError, match="do not match"):
            await manager.acquire(vcpu=4, mem_mib=2048)

    async def test_acquire_raises_on_exhaustion(self, manager):
        for i in range(manager._config.max_vms):
            vm = make_idle_vm(vm_id=f"vm-{i:08x}", ip=f"172.16.0.{i+2}", cid=i+3)
            vm.transition_to(VMState.ASSIGNED)
            manager._vms[vm.vm_id] = vm

        with pytest.raises(RuntimeError, match="pool_exhausted"):
            await manager.acquire(vcpu=1, mem_mib=512)

    async def test_release_destroys_vm(self, manager):
        vm = make_idle_vm()
        vm.transition_to(VMState.ASSIGNED)
        manager._vms["vm-test1234"] = vm

        await manager.release("vm-test1234", destroy=True)
        assert "vm-test1234" not in manager._vms
        manager._destroy_vm.assert_awaited_once()

    async def test_idle_count(self, manager):
        for i in range(3):
            manager._vms[f"vm-{i:08x}"] = make_idle_vm(
                vm_id=f"vm-{i:08x}", ip=f"172.16.0.{i+2}", cid=i+3
            )
        assert manager.idle_count == 3


class TestPoolManagerReplenishThreshold:
    """replenish_threshold gates when background booting is triggered."""

    @pytest.fixture
    def manager(self, tmp_path):
        config = make_test_config(tmp_path)  # pool_size=2, replenish_threshold=1
        mgr = PoolManager(config)
        mgr._boot_vm = AsyncMock(return_value=None)
        mgr._destroy_vm = AsyncMock(return_value=None)
        return mgr

    async def test_replenish_skipped_when_idle_at_or_above_threshold(self, manager):
        """No booting should happen when idle_count >= replenish_threshold."""
        # threshold=1, so 1 idle VM means no replenishment needed
        manager._vms["vm-test1234"] = make_idle_vm()
        assert manager.idle_count == 1  # equals threshold

        await manager.replenish()
        manager._boot_vm.assert_not_awaited()

    async def test_replenish_boots_to_pool_size_when_below_threshold(self, manager):
        """When idle_count < replenish_threshold, boot VMs until idle reaches pool_size."""
        # threshold=1, pool_size=2; empty pool should trigger booting
        assert manager.idle_count == 0  # below threshold of 1

        booted_vms = []

        async def fake_boot():
            i = len(booted_vms)
            vm = make_idle_vm(vm_id=f"vm-{i:08x}", ip=f"172.16.0.{i+2}", cid=i+3)
            manager._vms[vm.vm_id] = vm
            booted_vms.append(vm)

        manager._boot_vm = AsyncMock(side_effect=fake_boot)
        await manager.replenish()

        # Should have booted exactly pool_size=2 VMs
        assert len(booted_vms) == 2
        assert manager.idle_count == 2

    async def test_acquire_triggers_replenish_below_threshold(self, manager):
        """Acquiring the last VM (idle drops below threshold) should trigger replenish."""
        # Start with 1 idle VM (= threshold=1); after acquire idle=0 < threshold
        vm = make_idle_vm()
        manager._vms["vm-test1234"] = vm

        booted_vms = []

        async def fake_boot():
            i = len(booted_vms)
            new_vm = make_idle_vm(vm_id=f"vm-{i:08x}", ip=f"172.16.0.{i+10}", cid=i+10)
            manager._vms[new_vm.vm_id] = new_vm
            booted_vms.append(new_vm)

        manager._boot_vm = AsyncMock(side_effect=fake_boot)

        result = await manager.acquire(vcpu=1, mem_mib=512)
        assert result["id"] == "vm-test1234"

        # Poll until background replenish task restores idle pool to pool_size=2
        import asyncio as _asyncio
        expected_idle = 2
        loop = _asyncio.get_event_loop()
        deadline = loop.time() + 1.0
        while manager.idle_count < expected_idle and loop.time() < deadline:
            await _asyncio.sleep(0.01)

        assert manager.idle_count == expected_idle
        assert manager._boot_vm.await_count == expected_idle
