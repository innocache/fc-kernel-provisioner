"""Tests for the Firecracker REST API client."""

import pytest
from fc_pool_manager.firecracker_api import FirecrackerAPI


class TestFirecrackerAPI:
    @pytest.fixture
    def api(self):
        return FirecrackerAPI(socket_path="/tmp/test-fc.sock")

    def test_build_machine_config(self, api):
        body = api._machine_config_body(vcpu=2, mem_mib=1024)
        assert body == {"vcpu_count": 2, "mem_size_mib": 1024}

    def test_build_boot_source(self, api):
        body = api._boot_source_body(
            kernel_path="vmlinux",
            boot_args="console=ttyS0 init=/init",
        )
        assert body["kernel_image_path"] == "vmlinux"
        assert body["boot_args"] == "console=ttyS0 init=/init"

    def test_build_drive(self, api):
        body = api._drive_body("rootfs", "overlay.ext4", is_root=True)
        assert body["drive_id"] == "rootfs"
        assert body["path_on_host"] == "overlay.ext4"
        assert body["is_root_device"] is True
        assert body["is_read_only"] is False

    def test_build_network_interface(self, api):
        body = api._network_body("eth0", "tap-abc", "AA:FC:00:00:00:02")
        assert body["iface_id"] == "eth0"
        assert body["host_dev_name"] == "tap-abc"
        assert body["guest_mac"] == "AA:FC:00:00:00:02"

    def test_build_vsock(self, api):
        body = api._vsock_body(cid=3, uds_path="v.sock")
        assert body["guest_cid"] == 3
        assert body["uds_path"] == "v.sock"
