"""Edge case tests for pool manager core logic."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fc_pool_manager.manager import PoolManager
from fc_pool_manager.config import PoolConfig
from fc_pool_manager.vm import VMInstance, VMState


def make_test_config(tmp_path, **overrides) -> PoolConfig:
    pool = overrides.get("pool", {})
    yaml_content = f"""
pool:
  size: {pool.get('size', 2)}
  max_vms: {pool.get('max_vms', 5)}
  replenish_threshold: 1
  health_check_interval: 30
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


def make_vm(vm_id="vm-test1234", ip="172.16.0.2", cid=3, state=VMState.IDLE):
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
    if state != VMState.BOOTING:
        vm.transition_to(VMState.IDLE)
        if state == VMState.ASSIGNED:
            vm.transition_to(VMState.ASSIGNED)
        elif state == VMState.STOPPING:
            vm.transition_to(VMState.STOPPING)
    return vm


class TestAcquireEdgeCases:
    @pytest.fixture
    def manager(self, tmp_path):
        config = make_test_config(tmp_path)
        mgr = PoolManager(config)
        mgr._boot_vm = AsyncMock(return_value=None)
        mgr._destroy_vm = AsyncMock(return_value=None)
        return mgr

    async def test_acquire_skips_booting_vms(self, manager):
        """BOOTING VMs should not be acquired; they should be skipped in favor of new boots."""
        vm = make_vm(state=VMState.BOOTING)
        manager._vms["vm-test1234"] = vm

        # With only a BOOTING VM present, total_count < max_vms, so acquire()
        # should skip the BOOTING VM and attempt to boot a new one via _boot_vm.
        # Our mock _boot_vm returns None, so acquire() will ultimately raise pool_exhausted.
        with pytest.raises(RuntimeError, match="pool_exhausted"):
            await manager.acquire(vcpu=1, mem_mib=512)

        # Ensure that we actually attempted to boot a new VM, proving BOOTING VMs were skipped.
        manager._boot_vm.assert_awaited()
    async def test_acquire_skips_assigned_vms(self, manager):
        """Already ASSIGNED VMs should not be double-acquired."""
        vm = make_vm(state=VMState.ASSIGNED)
        manager._vms["vm-test1234"] = vm
        # Fill to max
        for i in range(manager._config.max_vms - 1):
            m = make_vm(vm_id=f"vm-fill{i:04d}", ip=f"172.16.0.{i+10}", cid=i+10, state=VMState.ASSIGNED)
            manager._vms[m.vm_id] = m
        with pytest.raises(RuntimeError, match="pool_exhausted"):
            await manager.acquire(vcpu=1, mem_mib=512)

    async def test_acquire_returns_vsock_path(self, manager):
        """Acquire should return vsock_path for provisioner communication."""
        vm = make_vm()
        manager._vms["vm-test1234"] = vm
        result = await manager.acquire(vcpu=1, mem_mib=512)
        assert "vsock_path" in result
        assert result["vsock_path"] == "/tmp/jail/v.sock"

    async def test_acquire_with_zero_vcpu_raises(self, manager):
        """Zero resources should raise ValueError."""
        manager._vms["vm-test1234"] = make_vm()
        with pytest.raises(ValueError, match="do not match"):
            await manager.acquire(vcpu=0, mem_mib=0)

    async def test_acquire_partial_resource_mismatch(self, manager):
        """Only vcpu matches but not mem_mib should still raise."""
        manager._vms["vm-test1234"] = make_vm()
        with pytest.raises(ValueError, match="do not match"):
            await manager.acquire(vcpu=1, mem_mib=1024)

    async def test_acquire_from_empty_pool_at_max(self, manager):
        """Empty pool at max capacity should raise pool_exhausted."""
        for i in range(manager._config.max_vms):
            m = make_vm(vm_id=f"vm-{i:08x}", ip=f"172.16.0.{i+2}", cid=i+3, state=VMState.ASSIGNED)
            manager._vms[m.vm_id] = m
        with pytest.raises(RuntimeError, match="pool_exhausted"):
            await manager.acquire(vcpu=1, mem_mib=512)


class TestReleaseEdgeCases:
    @pytest.fixture
    def manager(self, tmp_path):
        config = make_test_config(tmp_path)
        mgr = PoolManager(config)
        mgr._boot_vm = AsyncMock(return_value=None)
        mgr._destroy_vm = AsyncMock(return_value=None)
        return mgr

    async def test_release_unknown_vm_is_noop(self, manager):
        """Releasing a VM that doesn't exist should not raise."""
        await manager.release("vm-nonexistent", destroy=True)
        manager._destroy_vm.assert_not_awaited()

    async def test_release_same_vm_twice(self, manager):
        """Releasing the same VM twice should not raise."""
        vm = make_vm(state=VMState.ASSIGNED)
        manager._vms["vm-test1234"] = vm
        await manager.release("vm-test1234", destroy=True)
        assert "vm-test1234" not in manager._vms
        # Second release should be a noop
        await manager.release("vm-test1234", destroy=True)
        assert manager._destroy_vm.await_count == 1

    async def test_release_booting_vm_transitions_to_stopping(self, manager):
        """Releasing a BOOTING VM should transition to STOPPING."""
        vm = make_vm(state=VMState.BOOTING)
        manager._vms["vm-test1234"] = vm
        await manager.release("vm-test1234", destroy=True)
        assert "vm-test1234" not in manager._vms
        manager._destroy_vm.assert_awaited_once()

    async def test_release_destroy_false_assigned_to_idle_invalid(self, manager):
        """Using destroy=False on an ASSIGNED VM (attempting ASSIGNED -> IDLE) raises."""
        vm = make_vm(state=VMState.ASSIGNED)
        manager._vms["vm-test1234"] = vm
        # destroy=False tries vsock reset then attempts to transition to IDLE
        # ASSIGNED -> IDLE is not a valid transition, so this should raise
        with patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock:
            mock_vsock.side_effect = ConnectionError("no VM")
            with pytest.raises(ValueError, match="Invalid state transition"):
                await manager.release("vm-test1234", destroy=False)


class TestPoolStatus:
    @pytest.fixture
    def manager(self, tmp_path):
        config = make_test_config(tmp_path)
        mgr = PoolManager(config)
        mgr._boot_vm = AsyncMock(return_value=None)
        mgr._destroy_vm = AsyncMock(return_value=None)
        return mgr

    async def test_pool_status_empty(self, manager):
        status = manager.pool_status()
        assert status == {"idle": 0, "assigned": 0, "booting": 0, "max": 5}

    async def test_pool_status_mixed(self, manager):
        manager._vms["vm-1"] = make_vm(vm_id="vm-1", ip="172.16.0.2", cid=3, state=VMState.IDLE)
        manager._vms["vm-2"] = make_vm(vm_id="vm-2", ip="172.16.0.3", cid=4, state=VMState.ASSIGNED)
        manager._vms["vm-3"] = make_vm(vm_id="vm-3", ip="172.16.0.4", cid=5, state=VMState.BOOTING)
        status = manager.pool_status()
        assert status["idle"] == 1
        assert status["assigned"] == 1
        assert status["booting"] == 1
        assert status["max"] == 5

    async def test_pool_status_stopping_not_counted(self, manager):
        """STOPPING VMs should not appear in idle/assigned/booting counts."""
        vm = make_vm(state=VMState.ASSIGNED)
        vm.transition_to(VMState.STOPPING)
        manager._vms["vm-1"] = vm
        status = manager.pool_status()
        assert status["idle"] == 0
        assert status["assigned"] == 0
        assert status["booting"] == 0

    async def test_total_count_includes_all_states(self, manager):
        manager._vms["vm-1"] = make_vm(vm_id="vm-1", ip="172.16.0.2", cid=3, state=VMState.IDLE)
        manager._vms["vm-2"] = make_vm(vm_id="vm-2", ip="172.16.0.3", cid=4, state=VMState.ASSIGNED)
        assert manager.total_count == 2


class TestIsAlive:
    @pytest.fixture
    def manager(self, tmp_path):
        config = make_test_config(tmp_path)
        mgr = PoolManager(config)
        return mgr

    async def test_is_alive_unknown_vm(self, manager):
        result = await manager.is_alive("vm-nonexistent")
        assert result == {"alive": False}

    @patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock)
    async def test_is_alive_vsock_error_returns_false(self, mock_vsock, manager):
        mock_vsock.side_effect = ConnectionError("no connection")
        manager._vms["vm-1"] = make_vm()
        result = await manager.is_alive("vm-1")
        assert result["alive"] is False

    @patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock)
    async def test_is_alive_timeout_returns_false(self, mock_vsock, manager):
        mock_vsock.side_effect = asyncio.TimeoutError()
        manager._vms["vm-1"] = make_vm()
        result = await manager.is_alive("vm-1")
        assert result["alive"] is False

    @patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock)
    async def test_is_alive_unexpected_response(self, mock_vsock, manager):
        """Guest responds but with unexpected status."""
        mock_vsock.return_value = {"status": "confused", "uptime": 0}
        manager._vms["vm-1"] = make_vm()
        result = await manager.is_alive("vm-1")
        assert result["alive"] is False


class TestShutdown:
    @pytest.fixture
    def manager(self, tmp_path):
        config = make_test_config(tmp_path)
        mgr = PoolManager(config)
        mgr._destroy_vm = AsyncMock(return_value=None)
        return mgr

    async def test_shutdown_empty_pool(self, manager):
        await manager.shutdown()
        assert len(manager._vms) == 0

    async def test_shutdown_clears_all_vms(self, manager):
        for i in range(3):
            manager._vms[f"vm-{i}"] = make_vm(vm_id=f"vm-{i}", ip=f"172.16.0.{i+2}", cid=i+3)
        await manager.shutdown()
        assert len(manager._vms) == 0
        assert manager._destroy_vm.await_count == 3

    async def test_shutdown_continues_on_destroy_error(self, manager):
        """If one VM fails to destroy, others should still be cleaned up."""
        manager._destroy_vm = AsyncMock(side_effect=[Exception("oops"), None, None])
        for i in range(3):
            manager._vms[f"vm-{i}"] = make_vm(vm_id=f"vm-{i}", ip=f"172.16.0.{i+2}", cid=i+3)
        await manager.shutdown()
        assert len(manager._vms) == 0
        assert manager._destroy_vm.await_count == 3

    async def test_shutdown_handles_stopping_state_vms(self, manager):
        """VMs already in STOPPING should still be destroyed."""
        vm = make_vm(state=VMState.ASSIGNED)
        vm.transition_to(VMState.STOPPING)
        manager._vms["vm-1"] = vm
        await manager.shutdown()
        assert len(manager._vms) == 0


class TestReplenish:
    async def test_replenish_stops_on_boot_failure(self, tmp_path):
        config = make_test_config(tmp_path, pool={"size": 5, "max_vms": 10})
        mgr = PoolManager(config)
        mgr._boot_vm = AsyncMock(side_effect=RuntimeError("boot failed"))
        await mgr.replenish()
        # Should have tried once and stopped
        assert mgr._boot_vm.await_count == 1

    async def test_replenish_respects_max_vms(self, tmp_path):
        config = make_test_config(tmp_path, pool={"size": 10, "max_vms": 2})
        mgr = PoolManager(config)

        boot_count = 0

        async def fake_boot():
            nonlocal boot_count
            boot_count += 1
            vm = make_vm(vm_id=f"vm-boot{boot_count}", ip=f"172.16.0.{boot_count+1}", cid=boot_count+2)
            mgr._vms[vm.vm_id] = vm
            return vm

        mgr._boot_vm = AsyncMock(side_effect=fake_boot)
        await mgr.replenish()
        assert boot_count == 2  # Stopped at max_vms=2
