import json

from unittest.mock import AsyncMock, patch

from fc_pool_manager.firecracker_api import FirecrackerAPI
from fc_pool_manager.snapshot import SnapshotManager


def make_snapshot_manager(tmp_path) -> SnapshotManager:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.write_bytes(b"kernel-bytes")
    rootfs.write_bytes(b"rootfs-bytes")
    snapshot_dir = tmp_path / "snapshots"
    return SnapshotManager(
        snapshot_dir=str(snapshot_dir),
        kernel_path=str(kernel),
        rootfs_path=str(rootfs),
        firecracker_path="/usr/bin/firecracker",
    )


def test_has_valid_snapshot_true(tmp_path):
    manager = make_snapshot_manager(tmp_path)
    manager.save_metadata()
    vmstate = tmp_path / "snapshots" / "vmstate"
    memory = tmp_path / "snapshots" / "memory"
    vmstate.write_bytes(b"vmstate")
    memory.write_bytes(b"memory")

    assert manager.has_valid_snapshot() is True


def test_has_valid_snapshot_missing_metadata(tmp_path):
    manager = make_snapshot_manager(tmp_path)
    vmstate = tmp_path / "snapshots" / "vmstate"
    memory = tmp_path / "snapshots" / "memory"
    vmstate.parent.mkdir(parents=True, exist_ok=True)
    vmstate.write_bytes(b"vmstate")
    memory.write_bytes(b"memory")

    assert manager.has_valid_snapshot() is False


def test_has_valid_snapshot_stale_metadata(tmp_path):
    manager = make_snapshot_manager(tmp_path)
    manager.save_metadata()
    vmstate = tmp_path / "snapshots" / "vmstate"
    memory = tmp_path / "snapshots" / "memory"
    vmstate.write_bytes(b"vmstate")
    memory.write_bytes(b"memory")

    kernel = tmp_path / "vmlinux"
    kernel.write_bytes(b"new-kernel-bytes")

    assert manager.has_valid_snapshot() is False


def test_has_valid_snapshot_missing_vmstate(tmp_path):
    manager = make_snapshot_manager(tmp_path)
    manager.save_metadata()
    memory = tmp_path / "snapshots" / "memory"
    memory.write_bytes(b"memory")

    assert manager.has_valid_snapshot() is False


def test_save_metadata_creates_dir(tmp_path):
    manager = make_snapshot_manager(tmp_path)
    manager.save_metadata()
    meta_path = tmp_path / "snapshots" / "metadata.json"

    assert meta_path.exists()
    payload = json.loads(meta_path.read_text())
    assert "kernel_hash" in payload
    assert "rootfs_hash" in payload
    assert payload["firecracker_path"] == "/usr/bin/firecracker"


def test_invalidate_removes_files(tmp_path):
    manager = make_snapshot_manager(tmp_path)
    manager.save_metadata()
    vmstate = tmp_path / "snapshots" / "vmstate"
    memory = tmp_path / "snapshots" / "memory"
    vmstate.write_bytes(b"vmstate")
    memory.write_bytes(b"memory")

    manager.invalidate()

    assert not (tmp_path / "snapshots" / "metadata.json").exists()
    assert not vmstate.exists()
    assert not memory.exists()


def test_file_hash_deterministic(tmp_path):
    manager = make_snapshot_manager(tmp_path)
    path = tmp_path / "blob.bin"
    path.write_bytes(b"abcdef")

    first = manager._file_hash(str(path))
    second = manager._file_hash(str(path))
    assert first == second


async def test_pause_sends_patch_paused():
    api = FirecrackerAPI(socket_path="/tmp/test.sock")
    with patch.object(api, "_patch", new=AsyncMock()) as mock_patch:
        await api.pause()
    mock_patch.assert_awaited_once_with("/vm", {"state": "Paused"})


async def test_create_snapshot_sends_put():
    api = FirecrackerAPI(socket_path="/tmp/test.sock")
    with patch.object(api, "_put", new=AsyncMock()) as mock_put:
        await api.create_snapshot("vmstate", "memory")
    mock_put.assert_awaited_once_with(
        "/snapshot/create",
        {
            "snapshot_type": "Full",
            "snapshot_path": "vmstate",
            "mem_file_path": "memory",
        },
    )


async def test_load_snapshot_sends_put_with_mem_backend():
    api = FirecrackerAPI(socket_path="/tmp/test.sock")
    with patch.object(api, "_put", new=AsyncMock()) as mock_put:
        await api.load_snapshot("vmstate", "memory")
    mock_put.assert_awaited_once_with(
        "/snapshot/load",
        {
            "snapshot_path": "vmstate",
            "mem_backend": {
                "backend_path": "memory",
                "backend_type": "File",
            },
            "resume_vm": False,
        },
    )


async def test_load_snapshot_with_network_overrides():
    api = FirecrackerAPI(socket_path="/tmp/test.sock")
    overrides = [{"iface_id": "eth0", "host_dev_name": "tap-test"}]
    with patch.object(api, "_put", new=AsyncMock()) as mock_put:
        await api.load_snapshot("vmstate", "memory", network_overrides=overrides)
    mock_put.assert_awaited_once_with(
        "/snapshot/load",
        {
            "snapshot_path": "vmstate",
            "mem_backend": {
                "backend_path": "memory",
                "backend_type": "File",
            },
            "resume_vm": False,
            "network_overrides": overrides,
        },
    )


async def test_resume_sends_patch_resumed():
    api = FirecrackerAPI(socket_path="/tmp/test.sock")
    with patch.object(api, "_patch", new=AsyncMock()) as mock_patch:
        await api.resume()
    mock_patch.assert_awaited_once_with("/vm", {"state": "Resumed"})
