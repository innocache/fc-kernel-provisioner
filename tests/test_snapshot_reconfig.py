from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call, patch

import pytest

from fc_pool_manager.manager import PoolManager
from fc_pool_manager.network import NetworkManager
from fc_pool_manager.vm import VMInstance
from tests.test_pool_manager import make_test_config


def _make_vm(tmp_path, tap_name: str = "tap-vm-123") -> VMInstance:
    jail_path = tmp_path / "jail"
    jail_path.mkdir(parents=True, exist_ok=True)
    return VMInstance(
        vm_id="vm-123",
        short_id="123",
        ip="172.16.0.22",
        cid=22,
        tap_name=tap_name,
        mac="AA:FC:00:00:00:16",
        jail_path=str(jail_path),
        vsock_path=str(jail_path / "v.sock"),
    )


def _set_snapshot(manager: PoolManager, tmp_path, golden_tap_name: str) -> None:
    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    vmstate_src = snapshot_dir / "vmstate"
    memory_src = snapshot_dir / "memory"
    vmstate_src.write_bytes(b"vmstate")
    memory_src.write_bytes(b"memory")
    setattr(
        cast(Any, manager),
        "_snapshot",
        SimpleNamespace(
            vmstate_path=str(vmstate_src),
            memory_path=str(memory_src),
            golden_tap_name=golden_tap_name,
        ),
    )


def _build_manager(tmp_path) -> PoolManager:
    return PoolManager(make_test_config(tmp_path))


async def test_restore_detaches_tap_before_resume(tmp_path):
    manager = _build_manager(tmp_path)
    vm = _make_vm(tmp_path)
    _set_snapshot(manager, tmp_path, golden_tap_name=vm.tap_name)

    events: list[str] = []
    manager._network.detach_from_bridge = AsyncMock(side_effect=lambda *_: events.append("detach"))
    manager._network.attach_to_bridge = AsyncMock()
    manager._start_jailer = AsyncMock(return_value=str(tmp_path / "firecracker.sock"))
    api = SimpleNamespace(
        load_snapshot=AsyncMock(),
        resume=AsyncMock(side_effect=lambda: events.append("resume")),
    )

    with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=api), \
         patch("fc_pool_manager.vsock.vsock_request", new=AsyncMock(return_value={"status": "ok"})), \
         patch("os.chown"):
        await manager._restore_from_snapshot(vm)

    assert events.index("detach") < events.index("resume")


async def test_restore_reattaches_tap_after_reconfig(tmp_path):
    manager = _build_manager(tmp_path)
    vm = _make_vm(tmp_path)
    _set_snapshot(manager, tmp_path, golden_tap_name=vm.tap_name)

    events: list[str] = []
    manager._network.detach_from_bridge = AsyncMock()
    manager._network.attach_to_bridge = AsyncMock(side_effect=lambda *_: events.append("attach"))
    manager._start_jailer = AsyncMock(return_value=str(tmp_path / "firecracker.sock"))
    api = SimpleNamespace(load_snapshot=AsyncMock(), resume=AsyncMock())
    async def _vsock_side_effect(*_args, **_kwargs):
        events.append("reconfig")
        return {"status": "ok", "key": "testkey", "ports": {}, "running": True}

    vsock = AsyncMock(side_effect=_vsock_side_effect)

    with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=api), \
         patch("fc_pool_manager.vsock.vsock_request", new=vsock), \
         patch("os.chown"):
        await manager._restore_from_snapshot(vm)

    assert events.index("reconfig") < events.index("attach")


async def test_restore_sends_correct_reconfig_message(tmp_path):
    manager = _build_manager(tmp_path)
    vm = _make_vm(tmp_path)
    _set_snapshot(manager, tmp_path, golden_tap_name=vm.tap_name)

    manager._network.detach_from_bridge = AsyncMock()
    manager._network.attach_to_bridge = AsyncMock()
    manager._start_jailer = AsyncMock(return_value=str(tmp_path / "firecracker.sock"))
    api = SimpleNamespace(load_snapshot=AsyncMock(), resume=AsyncMock())
    vsock = AsyncMock(return_value={"status": "ok"})

    with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=api), \
         patch("fc_pool_manager.vsock.vsock_request", new=vsock), \
         patch("os.chown"):
        await manager._restore_from_snapshot(vm)

    assert vsock.await_args_list[0] == call(
        vm.vsock_path,
        {
            "action": "reconfigure_network",
            "ip": vm.ip,
            "mac": vm.mac,
            "gateway": manager._config.gateway,
        },
        timeout=10,
    )
    assert vsock.await_args_list[1] == call(
        vm.vsock_path,
        {"action": "get_kernel_info"},
        timeout=10,
    )


async def test_restore_renames_tap_for_golden_snapshot(tmp_path):
    manager = _build_manager(tmp_path)
    vm = _make_vm(tmp_path, tap_name="tap-vm")
    _set_snapshot(manager, tmp_path, golden_tap_name="tap-golden")

    manager._network._run = AsyncMock()
    manager._network.detach_from_bridge = AsyncMock()
    manager._network.attach_to_bridge = AsyncMock()
    manager._start_jailer = AsyncMock(return_value=str(tmp_path / "firecracker.sock"))
    api = SimpleNamespace(load_snapshot=AsyncMock(), resume=AsyncMock())

    with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=api), \
         patch("fc_pool_manager.vsock.vsock_request", new=AsyncMock(return_value={"status": "ok"})), \
         patch("os.chown"):
        await manager._restore_from_snapshot(vm)

    assert manager._network._run.call_args_list == [
        call("ip", "link", "set", "tap-vm", "name", "tap-golden"),
        call("ip", "link", "set", "tap-golden", "name", "tap-vm"),
    ]


async def test_restore_skips_rename_when_tap_matches(tmp_path):
    manager = _build_manager(tmp_path)
    vm = _make_vm(tmp_path, tap_name="tap-same")
    _set_snapshot(manager, tmp_path, golden_tap_name="tap-same")

    manager._network._run = AsyncMock()
    manager._network.detach_from_bridge = AsyncMock()
    manager._network.attach_to_bridge = AsyncMock()
    manager._start_jailer = AsyncMock(return_value=str(tmp_path / "firecracker.sock"))
    api = SimpleNamespace(load_snapshot=AsyncMock(), resume=AsyncMock())

    with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=api), \
         patch("fc_pool_manager.vsock.vsock_request", new=AsyncMock(return_value={"status": "ok"})), \
         patch("os.chown"):
        await manager._restore_from_snapshot(vm)

    manager._network._run.assert_not_awaited()


async def test_restore_reconfig_failure_destroys_vm(tmp_path):
    manager = _build_manager(tmp_path)
    vm = _make_vm(tmp_path)
    _set_snapshot(manager, tmp_path, golden_tap_name=vm.tap_name)

    manager._network.detach_from_bridge = AsyncMock()
    manager._network.attach_to_bridge = AsyncMock()
    manager._start_jailer = AsyncMock(return_value=str(tmp_path / "firecracker.sock"))
    api = SimpleNamespace(load_snapshot=AsyncMock(), resume=AsyncMock())

    with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=api), \
         patch("fc_pool_manager.vsock.vsock_request", new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch("os.chown"):
        with pytest.raises(RuntimeError, match="Network reconfig failed"):
            await manager._restore_from_snapshot(vm)

    manager._network.attach_to_bridge.assert_not_awaited()


async def test_detach_from_bridge_runs_nomaster():
    net = NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=2)
    net._run = AsyncMock()

    await net.detach_from_bridge("tap-abc")

    net._run.assert_awaited_once_with("ip", "link", "set", "tap-abc", "nomaster")


async def test_attach_to_bridge_runs_master():
    net = NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=2)
    net._run = AsyncMock()

    await net.attach_to_bridge("tap-abc")

    net._run.assert_awaited_once_with("ip", "link", "set", "tap-abc", "master", "fcbr0")


async def test_full_restore_sequence_ordering(tmp_path):
    manager = _build_manager(tmp_path)
    vm = _make_vm(tmp_path, tap_name="tap-vm")
    _set_snapshot(manager, tmp_path, golden_tap_name="tap-golden")

    events: list[str] = []

    async def run_side_effect(*cmd):
        if cmd == ("ip", "link", "set", "tap-vm", "name", "tap-golden"):
            events.append("rename_to_golden")
        if cmd == ("ip", "link", "set", "tap-golden", "name", "tap-vm"):
            events.append("rename_back")

    async def detach_side_effect(*_):
        events.append("detach")

    async def start_jailer_side_effect(*_):
        events.append("start_jailer")
        return str(tmp_path / "firecracker.sock")

    async def load_snapshot_side_effect(*_, **__):
        events.append("load_snapshot")

    async def resume_side_effect():
        events.append("resume")

    async def vsock_side_effect(*_, **__):
        events.append("vsock_reconfig")
        return {"status": "ok", "key": "testkey", "ports": {}, "running": True}

    async def attach_side_effect(*_):
        events.append("attach")

    manager._network._run = AsyncMock(side_effect=run_side_effect)
    manager._network.detach_from_bridge = AsyncMock(side_effect=detach_side_effect)
    manager._network.attach_to_bridge = AsyncMock(side_effect=attach_side_effect)
    manager._start_jailer = AsyncMock(side_effect=start_jailer_side_effect)
    api = SimpleNamespace(
        load_snapshot=AsyncMock(side_effect=load_snapshot_side_effect),
        resume=AsyncMock(side_effect=resume_side_effect),
    )

    with patch("fc_pool_manager.manager.FirecrackerAPI", return_value=api), \
         patch("fc_pool_manager.vsock.vsock_request", new=AsyncMock(side_effect=vsock_side_effect)), \
         patch("os.chown"):
        await manager._restore_from_snapshot(vm)

    assert events.index("detach") < events.index("start_jailer")
    assert events.index("start_jailer") < events.index("load_snapshot")
    assert events.index("load_snapshot") < events.index("resume")
    assert events.index("resume") < events.index("rename_back")
    assert events.index("rename_back") < events.index("vsock_reconfig")
    assert events.index("vsock_reconfig") < events.index("attach")
