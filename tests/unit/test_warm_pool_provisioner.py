import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from traitlets.config import LoggingConfigurable

from fc_provisioner.provisioner import FirecrackerProvisioner
from fc_provisioner.warm_pool import WarmPoolProvisioner


def _make_warm_vm(vm_id="vm-warm-1"):
    return {
        "id": vm_id,
        "ip": "172.16.0.10",
        "vsock_path": "/tmp/v.sock",
        "kernel_ports": {
            "shell_port": 5555,
            "iopub_port": 5556,
            "stdin_port": 5557,
            "control_port": 5558,
            "hb_port": 5559,
        },
    }


class _FakeParent(LoggingConfigurable):
    def __init__(self):
        super().__init__()
        self.session = MagicMock(key=b"old-key", signature_scheme="hmac-sha256")


def _make_prov(**overrides):
    parent = overrides.pop("parent", _FakeParent())
    prov = WarmPoolProvisioner.__new__(WarmPoolProvisioner)
    prov.parent = parent
    prov.kernel_id = overrides.get("kernel_id", "kid-1")
    prov.vm_id = overrides.get("vm_id")
    prov.vm_ip = overrides.get("vm_ip")
    prov.vsock_path = overrides.get("vsock_path")
    prov.kernel_ports = overrides.get("kernel_ports")
    prov.process = None
    prov.pool_client = overrides.get("pool_client")
    prov.pool_socket = "/var/run/fc-pool.sock"
    prov.vcpu_count = 1
    prov.mem_size_mib = 512
    prov.connection_info = {}

    from jupyter_client.kernelspec import KernelSpec
    prov.kernel_spec = KernelSpec(
        display_name="test",
        argv=[],
        metadata={"kernel_provisioner": {"config": {}}},
    )
    return prov


@pytest.fixture(autouse=True)
async def reset_class_state():
    WarmPoolProvisioner._initialized = False
    WarmPoolProvisioner._warm_pool = asyncio.Queue()
    WarmPoolProvisioner._pool_client = None
    WarmPoolProvisioner._replenish_task = None
    yield
    if WarmPoolProvisioner._replenish_task and not WarmPoolProvisioner._replenish_task.done():
        WarmPoolProvisioner._replenish_task.cancel()
        try:
            await WarmPoolProvisioner._replenish_task
        except asyncio.CancelledError:
            pass
    WarmPoolProvisioner._initialized = False
    WarmPoolProvisioner._warm_pool = None


class TestWarmPoolPreLaunch:
    async def test_pop_from_warm_pool(self):
        vm = _make_warm_vm()
        await WarmPoolProvisioner._warm_pool.put(vm)

        prov = _make_prov()
        with patch.object(WarmPoolProvisioner, "_ensure_initialized"):
            WarmPoolProvisioner._pool_client = AsyncMock()
            await prov.pre_launch()

        assert prov.vm_id == "vm-warm-1"
        assert prov.vm_ip == "172.16.0.10"
        assert prov.connection_info["ip"] == "172.16.0.10"
        assert prov.connection_info["shell_port"] == 5555
        assert prov.parent.session.key == b""

    async def test_fallback_on_empty_pool(self):
        prov = _make_prov()
        with patch.object(WarmPoolProvisioner, "_ensure_initialized"), \
             patch.object(FirecrackerProvisioner, "pre_launch", new=AsyncMock(return_value={})) as mock_super:
            WarmPoolProvisioner._warm_pool = asyncio.Queue()
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                await prov.pre_launch()
            mock_super.assert_awaited_once()

    async def test_pool_size_decrements(self):
        await WarmPoolProvisioner._warm_pool.put(_make_warm_vm("vm-1"))
        await WarmPoolProvisioner._warm_pool.put(_make_warm_vm("vm-2"))
        assert WarmPoolProvisioner._warm_pool.qsize() == 2

        prov = _make_prov()

        with patch.object(WarmPoolProvisioner, "_ensure_initialized"):
            WarmPoolProvisioner._pool_client = AsyncMock()
            await prov.pre_launch()

        assert WarmPoolProvisioner._warm_pool.qsize() == 1


class TestWarmPoolReplenish:
    async def test_replenish_adds_to_pool(self):
        mock_client = AsyncMock()
        mock_client.acquire = AsyncMock(return_value=_make_warm_vm())
        WarmPoolProvisioner._pool_client = mock_client
        WarmPoolProvisioner._pool_target = 1

        task = asyncio.create_task(WarmPoolProvisioner._replenish_loop())
        await asyncio.sleep(0.1)
        task.cancel()

        assert WarmPoolProvisioner._warm_pool.qsize() >= 1
        vm = await WarmPoolProvisioner._warm_pool.get()
        assert vm["id"] == "vm-warm-1"

    async def test_replenish_rejects_vm_without_kernel(self):
        no_kernel_vm = {"id": "vm-cold", "ip": "172.16.0.2", "vsock_path": "/tmp/v.sock"}
        call_count = 0

        async def acquire_then_fail(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return no_kernel_vm
            raise ConnectionError("stop")

        mock_client = AsyncMock()
        mock_client.acquire = acquire_then_fail
        mock_client.release = AsyncMock()
        WarmPoolProvisioner._pool_client = mock_client
        WarmPoolProvisioner._pool_target = 1

        with patch("fc_provisioner.warm_pool._REPLENISH_RETRY_DELAY", 0.01):
            task = asyncio.create_task(WarmPoolProvisioner._replenish_loop())
            await asyncio.sleep(0.1)
            task.cancel()

        mock_client.release.assert_awaited()
        assert WarmPoolProvisioner._warm_pool.qsize() == 0

    async def test_replenish_retries_on_error(self):
        mock_client = AsyncMock()
        results = [ConnectionError("pool down"), _make_warm_vm()]
        call_idx = 0

        async def acquire_side_effect(**kwargs):
            nonlocal call_idx
            idx = call_idx
            call_idx += 1
            if idx < len(results) and isinstance(results[idx], Exception):
                raise results[idx]
            return results[min(idx, len(results) - 1)]

        mock_client.acquire = acquire_side_effect
        WarmPoolProvisioner._pool_client = mock_client
        WarmPoolProvisioner._pool_target = 1

        with patch("fc_provisioner.warm_pool._REPLENISH_RETRY_DELAY", 0.01), \
             patch("fc_provisioner.warm_pool._REPLENISH_POLL_INTERVAL", 0.01):
            task = asyncio.create_task(WarmPoolProvisioner._replenish_loop())
            for _ in range(40):
                await asyncio.sleep(0.05)
                if WarmPoolProvisioner._warm_pool.qsize() > 0:
                    break
            task.cancel()

        assert call_idx >= 2
        assert WarmPoolProvisioner._warm_pool.qsize() == 1


class TestWarmPoolCleanup:
    async def test_cleanup_destroys_vm(self):
        prov = _make_prov(vm_id="vm-cleanup", pool_client=AsyncMock())
        await prov.cleanup(restart=False)
        prov.pool_client.release.assert_awaited_once_with("vm-cleanup", destroy=True)
        assert prov.vm_id is None

    async def test_cleanup_restart_delegates_to_super(self):
        prov = _make_prov(vm_id="vm-restart", vsock_path="/tmp/v.sock", pool_client=AsyncMock())
        prov.connection_info = {
            "shell_port": 5555, "iopub_port": 5556,
            "stdin_port": 5557, "control_port": 5558, "hb_port": 5559,
        }

        with patch("fc_provisioner.provisioner.vsock_request", new=AsyncMock(return_value={"status": "ready"})):
            await prov.cleanup(restart=True)

        assert prov.vm_id == "vm-restart"


class TestInitialization:
    def test_ensure_initialized_once(self):
        def close_coro(coro):
            coro.close()
            return MagicMock()

        with patch("fc_provisioner.warm_pool.asyncio.ensure_future", side_effect=close_coro) as mock_ef:
            WarmPoolProvisioner._ensure_initialized("/tmp/pool.sock", 1, 512)
            WarmPoolProvisioner._ensure_initialized("/tmp/pool.sock", 1, 512)
        mock_ef.assert_called_once()
        assert WarmPoolProvisioner._initialized is True

    def test_ensure_initialized_sets_config(self):
        def close_coro(coro):
            coro.close()
            return MagicMock()

        with patch("fc_provisioner.warm_pool.asyncio.ensure_future", side_effect=close_coro):
            WarmPoolProvisioner._ensure_initialized("/tmp/pool.sock", 2, 1024)
        assert WarmPoolProvisioner._vcpu == 2
        assert WarmPoolProvisioner._mem_mib == 1024
