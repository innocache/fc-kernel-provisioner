from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import importlib

import pytest

from fc_pool_manager.config import PoolConfig
from fc_pool_manager.manager import PoolManager
from fc_pool_manager.vm import VMInstance, VMState

jupyter_client = pytest.importorskip("jupyter_client")
KernelSpec = importlib.import_module("jupyter_client.kernelspec").KernelSpec
Configurable = importlib.import_module("traitlets.config").Configurable

from fc_provisioner.provisioner import FirecrackerProvisioner  # noqa: E402


def make_test_config(tmp_path) -> PoolConfig:
    yaml_content = """
pool:
  size: 2
  max_vms: 5
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


def make_vm(vm_id: str = "vm-test", state: VMState = VMState.BOOTING) -> VMInstance:
    vm = VMInstance(
        vm_id=vm_id,
        short_id=vm_id.replace("vm-", ""),
        ip="172.16.0.2",
        cid=3,
        tap_name="tap-test",
        mac="AA:FC:00:00:00:02",
        jail_path="/tmp/jail",
        vsock_path="/tmp/jail/v.sock",
    )
    if state != VMState.BOOTING:
        vm.transition_to(VMState.IDLE)
        if state == VMState.ASSIGNED:
            vm.transition_to(VMState.ASSIGNED)
    return vm


def make_provisioner() -> FirecrackerProvisioner:
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
    p.connection_info = {
        "key": "orig-hmac-key",
        "ip": "127.0.0.1",
        "transport": "tcp",
    }
    return p


async def test_boot_vm_prewarms_kernel_after_guest_ready(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    manager._snapshot_valid = False
    manager._prepare_jail_root = AsyncMock()
    manager._full_boot = AsyncMock()
    manager._destroy_vm = AsyncMock()
    manager._network.create_tap = AsyncMock()
    manager._network.apply_vm_rules = AsyncMock()

    ready = False

    async def mark_ready(_vm):
        nonlocal ready
        ready = True

    async def vsock_side_effect(_path, payload, timeout=0):
        if payload.get("action") == "pre_warm_kernel":
            assert ready
            assert timeout == 120
            return {
                "status": "ok",
                "key": "abc123",
                "ports": {"shell_port": 5555},
            }
        raise AssertionError(f"Unexpected action: {payload}")

    manager._wait_for_guest_agent = AsyncMock(side_effect=mark_ready)

    with patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock:
        mock_vsock.side_effect = vsock_side_effect
        await manager._boot_vm(use_snapshot=False)

    actions = [call.args[1]["action"] for call in mock_vsock.await_args_list]
    assert actions == ["pre_warm_kernel"]


async def test_boot_vm_stores_kernel_key_and_ports(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    manager._snapshot_valid = False
    manager._prepare_jail_root = AsyncMock()
    manager._full_boot = AsyncMock()
    manager._wait_for_guest_agent = AsyncMock()
    manager._destroy_vm = AsyncMock()
    manager._network.create_tap = AsyncMock()
    manager._network.apply_vm_rules = AsyncMock()

    with patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock:
        mock_vsock.return_value = {
            "status": "ok",
            "key": "warm-key-123",
            "ports": {"shell_port": 5501, "iopub_port": 5502},
        }
        vm = await manager._boot_vm(use_snapshot=False)

    assert vm.kernel_key == "warm-key-123"
    assert vm.kernel_ports == {"shell_port": 5501, "iopub_port": 5502}


async def test_restore_from_snapshot_gets_kernel_info(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    vm = make_vm()
    manager._start_jailer = AsyncMock(return_value="/tmp/fc.socket")
    manager._network.detach_from_bridge = AsyncMock()
    manager._network.attach_to_bridge = AsyncMock()
    manager._network._run = AsyncMock()

    fake_api = MagicMock()
    fake_api.load_snapshot = AsyncMock()
    fake_api.resume = AsyncMock()

    with (
        patch("fc_pool_manager.manager.FirecrackerAPI", return_value=fake_api),
        patch("fc_pool_manager.manager.os.path.exists", return_value=False),
        patch("fc_pool_manager.manager.os.link"),
        patch("fc_pool_manager.manager.os.chown"),
        patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock,
    ):
        mock_vsock.side_effect = [
            {"status": "ok"},
            {
                "status": "ok",
                "running": True,
                "key": "restored-key",
                "ports": {"shell_port": 5601, "hb_port": 5605},
            },
        ]
        await manager._restore_from_snapshot(vm)

    actions = [call.args[1]["action"] for call in mock_vsock.await_args_list]
    assert actions == ["reconfigure_network", "get_kernel_info"]
    assert vm.kernel_key == "restored-key"
    assert vm.kernel_ports == {"shell_port": 5601, "hb_port": 5605}


async def test_acquire_returns_kernel_info_when_available(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    vm = make_vm(state=VMState.IDLE)
    vm.kernel_key = "warm-key"
    vm.kernel_ports = {"shell_port": 5701}
    manager._vms[vm.vm_id] = vm

    result = await manager._acquire_inner(vcpu=1, mem_mib=512)

    assert result["kernel_key"] == "warm-key"
    assert result["kernel_ports"] == {"shell_port": 5701}


async def test_prewarm_failure_is_nonfatal_for_boot(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    manager._snapshot_valid = False
    manager._prepare_jail_root = AsyncMock()
    manager._full_boot = AsyncMock()
    manager._wait_for_guest_agent = AsyncMock()
    manager._destroy_vm = AsyncMock()
    manager._network.create_tap = AsyncMock()
    manager._network.apply_vm_rules = AsyncMock()

    with patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock:
        mock_vsock.side_effect = ConnectionError("prewarm unavailable")
        vm = await manager._boot_vm(use_snapshot=False)

    assert vm.state == VMState.IDLE
    assert vm.kernel_key is None
    assert vm.kernel_ports is None


async def test_restore_kernel_info_failure_is_nonfatal(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    vm = make_vm()
    manager._start_jailer = AsyncMock(return_value="/tmp/fc.socket")
    manager._network.detach_from_bridge = AsyncMock()
    manager._network.attach_to_bridge = AsyncMock()
    manager._network._run = AsyncMock()

    fake_api = MagicMock()
    fake_api.load_snapshot = AsyncMock()
    fake_api.resume = AsyncMock()

    with (
        patch("fc_pool_manager.manager.FirecrackerAPI", return_value=fake_api),
        patch("fc_pool_manager.manager.os.path.exists", return_value=False),
        patch("fc_pool_manager.manager.os.link"),
        patch("fc_pool_manager.manager.os.chown"),
        patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock,
    ):
        mock_vsock.side_effect = [
            {"status": "ok"},
            ConnectionError("kernel info unavailable"),
        ]
        await manager._restore_from_snapshot(vm)

    assert vm.kernel_key is None
    assert vm.kernel_ports is None


async def test_ephemeral_vm_prewarms_before_snapshot(tmp_path):
    manager = PoolManager(make_test_config(tmp_path))
    manager._prepare_jail_root = AsyncMock()
    manager._full_boot = AsyncMock()
    manager._wait_for_guest_agent = AsyncMock()
    manager._destroy_vm = AsyncMock()
    manager._network.create_tap = AsyncMock()
    manager._network.apply_vm_rules = AsyncMock()

    with patch("fc_pool_manager.vsock.vsock_request", new_callable=AsyncMock) as mock_vsock:
        mock_vsock.return_value = {"status": "ok", "key": "snap-key", "ports": {}}
        await manager._boot_ephemeral_vm()

    actions = [call.args[1]["action"] for call in mock_vsock.await_args_list]
    assert actions == ["pre_warm_kernel"]


@patch("fc_provisioner.provisioner.PoolClient")
async def test_provisioner_uses_prewarm_key_and_ports(MockPoolClient):
    p = make_provisioner()

    class DummyParent(Configurable):
        pass

    parent = DummyParent()
    parent.session = SimpleNamespace(
        key=b"orig-session-key",
        signature_scheme="hmac-sha256",
    )
    p.parent = parent

    mock_client = MagicMock()
    mock_client.acquire = AsyncMock(return_value={
        "id": "vm-abc12345",
        "ip": "172.16.0.2",
        "vsock_path": "/srv/jailer/firecracker/vm-abc12345/root/v.sock",
        "kernel_key": "prewarmed-key",
        "kernel_ports": {
            "shell_port": 6001,
            "iopub_port": 6002,
            "stdin_port": 6003,
            "control_port": 6004,
            "hb_port": 6005,
        },
    })
    mock_client.bind_kernel = AsyncMock()
    MockPoolClient.return_value = mock_client

    with patch.object(
        FirecrackerProvisioner.__bases__[0],
        "pre_launch",
        new_callable=AsyncMock,
        return_value={},
    ):
        await p.pre_launch()

    assert p.parent.session.key == b"prewarmed-key"
    assert p.connection_info["key"] == b"prewarmed-key"
    assert p.connection_info["shell_port"] == 6001
    assert p.connection_info["hb_port"] == 6005


@patch("fc_provisioner.provisioner.vsock_request")
async def test_launch_kernel_skips_start_when_prewarmed(mock_vsock):
    p = make_provisioner()
    p.vm_id = "vm-prewarmed"
    p.vm_ip = "172.16.0.33"
    p.vsock_path = "/tmp/v.sock"
    p.pool_client = MagicMock()
    p.kernel_key = "prewarmed-key"
    p.kernel_ports = {
        "shell_port": 6101,
        "iopub_port": 6102,
        "stdin_port": 6103,
        "control_port": 6104,
        "hb_port": 6105,
    }

    result = await p.launch_kernel(cmd=[])

    mock_vsock.assert_not_awaited()
    assert result["shell_port"] == 6101
    assert result["hb_port"] == 6105
    assert p.process is not None
