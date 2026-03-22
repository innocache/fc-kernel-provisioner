"""Edge case tests for the Firecracker provisioner."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from jupyter_client.kernelspec import KernelSpec
from fc_provisioner.provisioner import FirecrackerProvisioner, FirecrackerProcess


def make_provisioner(**overrides):
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
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


class TestFirecrackerProcessEdgeCases:
    async def test_terminate_delegates_to_kill(self):
        """terminate() should behave like kill()."""
        pool_client = MagicMock()
        pool_client.release = AsyncMock()
        proc = FirecrackerProcess("vm-test", pool_client)
        await proc.terminate()
        pool_client.release.assert_awaited_once_with("vm-test", destroy=True)
        assert proc._exit_code == -9

    async def test_send_signal_is_noop(self):
        """send_signal on FirecrackerProcess is a no-op (signals go via vsock)."""
        pool_client = MagicMock()
        proc = FirecrackerProcess("vm-test", pool_client)
        proc.send_signal(15)  # Should not raise

    async def test_poll_after_kill_returns_neg9(self):
        """After kill(), poll should return -9 without querying pool."""
        pool_client = MagicMock()
        pool_client.release = AsyncMock()
        proc = FirecrackerProcess("vm-test", pool_client)
        await proc.kill()
        pool_client.is_alive = AsyncMock(return_value=True)  # Should not be called
        assert await proc.poll() == -9


class TestProvisionerLaunchEdgeCases:
    @patch("fc_provisioner.provisioner.vsock_request")
    async def test_launch_kernel_failure_raises(self, mock_vsock):
        """launch_kernel should raise when guest agent reports failure."""
        p = make_provisioner(
            vm_id="vm-test",
            vm_ip="172.16.0.2",
            vsock_path="/tmp/v.sock",
            pool_client=MagicMock(),
        )
        mock_vsock.return_value = {"status": "error", "error": "ipykernel not found"}

        with pytest.raises(RuntimeError, match="failed to start kernel"):
            await p.launch_kernel(cmd=[])

    @patch("fc_provisioner.provisioner.vsock_request")
    async def test_launch_process_failure_raises(self, mock_vsock):
        """launch_process should raise when guest agent reports failure."""
        p = make_provisioner(
            vm_id="vm-test",
            vm_ip="172.16.0.2",
            vsock_path="/tmp/v.sock",
            pool_client=MagicMock(),
        )
        mock_vsock.return_value = {"status": "error", "message": "ipykernel not found"}

        with pytest.raises(RuntimeError, match="failed to start kernel"):
            await p.launch_process(cmd=[])

    @patch("fc_provisioner.provisioner.vsock_request")
    async def test_launch_kernel_updates_connection_info(self, mock_vsock):
        """After launch, connection_info should have VM IP and ports."""
        p = make_provisioner(
            vm_id="vm-test",
            vm_ip="172.16.0.99",
            vsock_path="/tmp/v.sock",
            pool_client=MagicMock(),
        )
        mock_vsock.return_value = {"status": "ready", "pid": 42}

        result = await p.launch_kernel(cmd=[])

        assert result["ip"] == "172.16.0.99"
        assert result["transport"] == "tcp"
        assert result["shell_port"] == 5555
        assert result["hb_port"] == 5559


class TestProvisionerCleanupEdgeCases:
    async def test_cleanup_without_pool_client_is_safe(self):
        """cleanup with no pool_client and no vm_id should not crash."""
        p = make_provisioner()
        await p.cleanup(restart=False)
        # No exception means success

    async def test_double_cleanup_is_safe(self):
        """Calling cleanup twice should not raise."""
        p = make_provisioner(
            vm_id="vm-test",
            pool_client=MagicMock(release=AsyncMock()),
            process=MagicMock(),
        )
        await p.cleanup(restart=False)
        assert p.vm_id is None
        await p.cleanup(restart=False)  # vm_id is None now, should be noop

    @patch("fc_provisioner.provisioner.vsock_request")
    async def test_cleanup_restart_sends_restart_action(self, mock_vsock):
        """cleanup(restart=True) should send restart_kernel to guest agent."""
        p = make_provisioner(
            vm_id="vm-test",
            vsock_path="/tmp/v.sock",
            pool_client=MagicMock(),
        )
        mock_vsock.return_value = {"status": "ready", "pid": 43}

        await p.cleanup(restart=True)

        msg = mock_vsock.call_args[0][1]
        assert msg["action"] == "restart_kernel"
        assert msg["key"] == "test-hmac-key"

    async def test_cleanup_restart_without_vsock_path_falls_through(self):
        """If vsock_path is None, restart=True should release instead."""
        pool_client = MagicMock(release=AsyncMock())
        p = make_provisioner(
            vm_id="vm-test",
            vsock_path=None,
            pool_client=pool_client,
        )
        await p.cleanup(restart=True)
        pool_client.release.assert_awaited_once()


class TestProvisionerStateEdgeCases:
    async def test_poll_no_process_returns_zero(self):
        """poll() with no process should return 0 (not running)."""
        p = make_provisioner()
        result = await p.poll()
        assert result == 0

    async def test_wait_no_process_returns_zero(self):
        """wait() with no process should return 0 immediately."""
        p = make_provisioner()
        result = await p.wait()
        assert result == 0

    def test_has_process_false_initially(self):
        p = make_provisioner()
        assert p.has_process is False

    def test_has_process_true_with_process(self):
        p = make_provisioner(process=MagicMock())
        assert p.has_process is True

    @patch("fc_provisioner.provisioner.vsock_send_only")
    async def test_send_signal_via_vsock(self, mock_send):
        """send_signal should send via vsock when path is set."""
        p = make_provisioner(vsock_path="/tmp/v.sock")
        await p.send_signal(2)
        mock_send.assert_awaited_once()
        msg = mock_send.call_args[0][1]
        assert msg["action"] == "signal"
        assert msg["signum"] == 2

    async def test_send_signal_no_vsock_path_is_noop(self):
        """send_signal with no vsock_path should not crash."""
        p = make_provisioner(vsock_path=None)
        await p.send_signal(15)  # Should not raise


class TestProvisionerInfoEdgeCases:
    async def test_load_provisioner_info_roundtrip(self):
        """get_provisioner_info -> load_provisioner_info should preserve state."""
        p = make_provisioner(
            vm_id="vm-roundtrip",
            vm_ip="172.16.0.42",
            vsock_path="/tmp/v.sock",
        )

        with patch.object(
            FirecrackerProvisioner.__bases__[0],
            "get_provisioner_info",
            new_callable=AsyncMock,
            return_value={"provisioner_name": "firecracker-provisioner"},
        ):
            info = await p.get_provisioner_info()

        p2 = make_provisioner()

        with patch.object(
            FirecrackerProvisioner.__bases__[0],
            "load_provisioner_info",
            new_callable=AsyncMock,
        ):
            await p2.load_provisioner_info(info)

        assert p2.vm_id == "vm-roundtrip"
        assert p2.vm_ip == "172.16.0.42"
        assert p2.vsock_path == "/tmp/v.sock"
        assert p2.process is not None  # Created because vm_id was set

    async def test_load_provisioner_info_no_vm_id(self):
        """If loaded info has no vm_id, no process should be created."""
        p = make_provisioner()

        with patch.object(
            FirecrackerProvisioner.__bases__[0],
            "load_provisioner_info",
            new_callable=AsyncMock,
        ):
            await p.load_provisioner_info({"provisioner_name": "test"})

        assert p.process is None
        assert p.vm_id is None


class TestProvisionerConfigEdgeCases:
    def test_apply_config_reads_kernel_spec_metadata(self):
        ks = KernelSpec()
        ks.metadata = {
            "kernel_provisioner": {
                "config": {
                    "pool_socket": "/custom/path.sock",
                    "vcpu_count": 4,
                    "mem_size_mib": 2048,
                }
            }
        }
        p = FirecrackerProvisioner(kernel_spec=ks, kernel_id="test")
        p._apply_config()
        assert p.pool_socket == "/custom/path.sock"
        assert p.vcpu_count == 4
        assert p.mem_size_mib == 2048

    def test_apply_config_uses_defaults_when_missing(self):
        ks = KernelSpec()
        ks.metadata = {}
        p = FirecrackerProvisioner(kernel_spec=ks, kernel_id="test")
        p._apply_config()
        assert p.pool_socket == "/var/run/fc-pool.sock"
        assert p.vcpu_count == 1
        assert p.mem_size_mib == 512
