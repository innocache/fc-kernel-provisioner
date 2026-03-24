import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fc_pool_manager.config import PoolConfig
from fc_pool_manager.manager import PoolManager
from fc_pool_manager.vm import VMInstance, VMState


def make_test_config(tmp_path, snapshot_dir=None) -> PoolConfig:
    if snapshot_dir is None:
        snapshot_dir = str(tmp_path / "snapshots")
    kernel = str(tmp_path / "images" / "vmlinux")
    rootfs = str(tmp_path / "images" / "rootfs.ext4")
    yaml_content = f"""
pool:
  size: 2
  max_vms: 5
  health_check_interval: 30
  vm_idle_timeout: 600
  snapshot_dir: {snapshot_dir}
vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: {kernel}
  rootfs: {rootfs}
  boot_args_template: "console=ttyS0 ip={{vm_ip}}::172.16.0.1:255.255.255.0::eth0:off init=/init"
network:
  bridge: fcbr0
  subnet: "172.16.0.0/24"
  gateway: "172.16.0.1"
  vm_ip_start: 2
jailer:
  enabled: true
  chroot_base: {tmp_path}/jailer
  exec_path: {tmp_path}/bin/firecracker
  uid: 1000
  gid: 1000
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_content)
    return PoolConfig.from_yaml(str(config_file))


class TestCreateGoldenSnapshot:
    async def test_creates_snapshot_files(self, tmp_path):
        config = make_test_config(tmp_path)
        manager = PoolManager(config)

        fake_vm = VMInstance(
            vm_id="vm-snap-1", short_id="snap1", ip="172.16.0.2", cid=3,
            tap_name="tap-snap1", mac="AA:FC:00:00:00:02",
            jail_path=str(tmp_path / "jail"), vsock_path=str(tmp_path / "v.sock"),
        )
        os.makedirs(tmp_path / "jail", exist_ok=True)
        (tmp_path / "jail" / "vmstate").write_bytes(b"state-data")
        (tmp_path / "jail" / "memory").write_bytes(b"mem-data")

        manager._boot_ephemeral_vm = AsyncMock(return_value=fake_vm)
        manager._destroy_vm = AsyncMock()

        mock_api = MagicMock()
        mock_api.pause = AsyncMock()
        mock_api.create_snapshot = AsyncMock()

        with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=mock_api), \
             patch.object(manager._snapshot, "save_metadata"):
            await manager.create_golden_snapshot()

        assert manager._snapshot_valid is True
        manager._destroy_vm.assert_awaited_once()

    async def test_destroys_vm_on_failure(self, tmp_path):
        config = make_test_config(tmp_path)
        manager = PoolManager(config)

        fake_vm = VMInstance(
            vm_id="vm-snap-fail", short_id="snapf", ip="172.16.0.2", cid=3,
            tap_name="tap-snapf", mac="AA:FC:00:00:00:02",
            jail_path=str(tmp_path / "jail"), vsock_path=str(tmp_path / "v.sock"),
        )

        manager._boot_ephemeral_vm = AsyncMock(return_value=fake_vm)
        manager._destroy_vm = AsyncMock()

        mock_api = MagicMock()
        mock_api.pause = AsyncMock(side_effect=RuntimeError("pause failed"))

        with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=mock_api):
            with pytest.raises(RuntimeError, match="pause failed"):
                await manager.create_golden_snapshot()

        assert manager._snapshot_valid is False
        manager._destroy_vm.assert_awaited_once()

    async def test_ephemeral_vm_not_in_pool(self, tmp_path):
        config = make_test_config(tmp_path)
        manager = PoolManager(config)

        fake_vm = VMInstance(
            vm_id="vm-snap-eph", short_id="snapeph", ip="172.16.0.2", cid=3,
            tap_name="tap-snapeph", mac="AA:FC:00:00:00:02",
            jail_path=str(tmp_path / "jail"), vsock_path=str(tmp_path / "v.sock"),
        )
        os.makedirs(tmp_path / "jail", exist_ok=True)
        (tmp_path / "jail" / "vmstate").write_bytes(b"state")
        (tmp_path / "jail" / "memory").write_bytes(b"mem")

        manager._boot_ephemeral_vm = AsyncMock(return_value=fake_vm)
        manager._destroy_vm = AsyncMock()

        mock_api = MagicMock()
        mock_api.pause = AsyncMock()
        mock_api.create_snapshot = AsyncMock()

        with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=mock_api), \
             patch.object(manager._snapshot, "save_metadata"):
            await manager.create_golden_snapshot()

        assert fake_vm.vm_id not in manager._vms


class TestEnsureGoldenSnapshot:
    async def test_skips_if_already_checked(self, tmp_path):
        config = make_test_config(tmp_path)
        manager = PoolManager(config)
        manager._snapshot_checked = True
        manager.create_golden_snapshot = AsyncMock()

        await manager.ensure_golden_snapshot()
        manager.create_golden_snapshot.assert_not_awaited()

    async def test_skips_if_valid_snapshot_exists(self, tmp_path):
        config = make_test_config(tmp_path)
        manager = PoolManager(config)
        manager._snapshot.has_valid_snapshot = lambda: True
        manager.create_golden_snapshot = AsyncMock()

        await manager.ensure_golden_snapshot()
        assert manager._snapshot_valid is True
        assert manager._snapshot_checked is True
        manager.create_golden_snapshot.assert_not_awaited()

    async def test_skips_if_files_missing(self, tmp_path):
        config = make_test_config(tmp_path)
        manager = PoolManager(config)
        manager.create_golden_snapshot = AsyncMock()

        await manager.ensure_golden_snapshot()
        assert manager._snapshot_checked is True
        manager.create_golden_snapshot.assert_not_awaited()

    async def test_skips_if_different_filesystem(self, tmp_path):
        config = make_test_config(tmp_path, snapshot_dir=str(tmp_path / "other_fs_snapshots"))
        manager = PoolManager(config)
        manager.create_golden_snapshot = AsyncMock()

        os.makedirs(config.snapshot_dir, exist_ok=True)
        os.makedirs(config.chroot_base, exist_ok=True)
        for p in (config.vm_kernel, config.vm_rootfs, config.firecracker_path):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()

        real_stat = os.stat
        snap_dev = real_stat(config.snapshot_dir).st_dev

        orig_stat = os.stat.__wrapped__ if hasattr(os.stat, '__wrapped__') else os.stat

        class FakeStat:
            def __init__(self, real_result, override_dev=None):
                self._real = real_result
                self.st_dev = override_dev if override_dev is not None else real_result.st_dev

            def __getattr__(self, name):
                return getattr(self._real, name)

        def patched_stat(p, *a, **kw):
            r = real_stat(p, *a, **kw)
            if str(p) == config.chroot_base:
                return FakeStat(r, override_dev=snap_dev + 999)
            return r

        with patch("fc_pool_manager.manager.os.stat", side_effect=patched_stat):
            await manager.ensure_golden_snapshot()

        manager.create_golden_snapshot.assert_not_awaited()

    async def test_creates_snapshot_when_needed(self, tmp_path):
        config = make_test_config(tmp_path)
        manager = PoolManager(config)
        manager.create_golden_snapshot = AsyncMock()

        os.makedirs(config.snapshot_dir, exist_ok=True)
        os.makedirs(config.chroot_base, exist_ok=True)
        for p in (config.vm_kernel, config.vm_rootfs, config.firecracker_path):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()

        await manager.ensure_golden_snapshot()

        manager.create_golden_snapshot.assert_awaited_once()
