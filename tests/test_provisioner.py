"""Tests for FirecrackerProvisioner (mocked, no real VMs)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from jupyter_client.kernelspec import KernelSpec
from fc_provisioner.provisioner import FirecrackerProvisioner, FirecrackerProcess


class TestFirecrackerProcess:
    async def test_poll_alive(self):
        pool_client = MagicMock()
        pool_client.is_alive = AsyncMock(return_value=True)
        proc = FirecrackerProcess("vm-test", pool_client)
        assert await proc.poll() is None

    async def test_poll_dead(self):
        pool_client = MagicMock()
        pool_client.is_alive = AsyncMock(return_value=False)
        proc = FirecrackerProcess("vm-test", pool_client)
        assert await proc.poll() == 1

    async def test_poll_caches_exit_code(self):
        pool_client = MagicMock()
        pool_client.is_alive = AsyncMock(return_value=False)
        proc = FirecrackerProcess("vm-test", pool_client)
        await proc.poll()
        pool_client.is_alive = AsyncMock(return_value=True)
        assert await proc.poll() == 1

    async def test_kill(self):
        pool_client = MagicMock()
        pool_client.release = AsyncMock()
        proc = FirecrackerProcess("vm-test", pool_client)
        await proc.kill()
        pool_client.release.assert_awaited_once_with("vm-test", destroy=True)
        assert proc._exit_code == -9


class TestFirecrackerProvisioner:
    @pytest.fixture
    def provisioner(self):
        ks = KernelSpec()
        ks.metadata = {
            "kernel_provisioner": {
                "config": {
                    "pool_socket": "/var/run/fc-pool.sock",
                    "vcpu_count": 1,
                    "mem_size_mib": 512,
                }
            }
        }
        p = FirecrackerProvisioner(kernel_spec=ks, kernel_id="test-kernel")
        p.pool_socket = "/var/run/fc-pool.sock"
        p.vcpu_count = 1
        p.mem_size_mib = 512
        p.vm_id = None
        p.vm_ip = None
        p.vsock_path = None
        p.process = None
        p.pool_client = None
        p.connection_info = {
            "key": "test-hmac-key",
            "ip": "127.0.0.1",
            "transport": "tcp",
        }
        return p

    @patch("fc_provisioner.provisioner.PoolClient")
    async def test_pre_launch_acquires_vm(self, MockPoolClient, provisioner):
        mock_client = MagicMock()
        mock_client.acquire = AsyncMock(return_value={
            "id": "vm-abc12345",
            "ip": "172.16.0.2",
            "vsock_path": "/srv/jailer/firecracker/vm-abc12345/root/v.sock",
        })
        MockPoolClient.return_value = mock_client

        with patch.object(
            FirecrackerProvisioner.__bases__[0], "pre_launch",
            new_callable=AsyncMock, return_value={}
        ):
            await provisioner.pre_launch()

        assert provisioner.vm_id == "vm-abc12345"
        assert provisioner.vm_ip == "172.16.0.2"
        mock_client.acquire.assert_awaited_once_with(vcpu=1, mem_mib=512)

    @patch("fc_provisioner.provisioner.vsock_request")
    async def test_launch_process_starts_kernel(self, mock_vsock, provisioner):
        provisioner.vm_id = "vm-abc12345"
        provisioner.vm_ip = "172.16.0.2"
        provisioner.vsock_path = "/tmp/v.sock"
        provisioner.pool_client = MagicMock()

        mock_vsock.return_value = {"status": "ready", "pid": 42}

        proc = await provisioner.launch_process(cmd=[])

        assert isinstance(proc, FirecrackerProcess)
        assert provisioner.connection_info["ip"] == "172.16.0.2"
        assert provisioner.connection_info["shell_port"] == 5555

        call_args = mock_vsock.call_args
        msg = call_args[0][1]
        assert msg["action"] == "start_kernel"
        assert msg["key"] == "test-hmac-key"

    async def test_cleanup_releases_vm(self, provisioner):
        provisioner.vm_id = "vm-abc12345"
        provisioner.pool_client = MagicMock()
        provisioner.pool_client.release = AsyncMock()
        provisioner.process = MagicMock()

        await provisioner.cleanup(restart=False)

        provisioner.pool_client.release.assert_awaited_once_with(
            "vm-abc12345", destroy=True
        )
        assert provisioner.vm_id is None
        assert provisioner.process is None

    @patch("fc_provisioner.provisioner.vsock_request")
    async def test_cleanup_restart(self, mock_vsock, provisioner):
        provisioner.vm_id = "vm-abc12345"
        provisioner.vsock_path = "/tmp/v.sock"
        provisioner.pool_client = MagicMock()
        mock_vsock.return_value = {"status": "ready", "pid": 43}

        await provisioner.cleanup(restart=True)

        msg = mock_vsock.call_args[0][1]
        assert msg["action"] == "restart_kernel"

    async def test_get_provisioner_info(self, provisioner):
        provisioner.vm_id = "vm-abc12345"
        provisioner.vm_ip = "172.16.0.2"
        provisioner.vsock_path = "/tmp/v.sock"

        with patch.object(
            FirecrackerProvisioner.__bases__[0], "get_provisioner_info",
            new_callable=AsyncMock,
            return_value={"provisioner_name": "firecracker-provisioner"},
        ):
            info = await provisioner.get_provisioner_info()

        assert info["vm_id"] == "vm-abc12345"
        assert info["vm_ip"] == "172.16.0.2"
