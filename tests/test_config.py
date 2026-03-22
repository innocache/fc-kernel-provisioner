"""Tests for pool manager configuration loader."""

import pytest
from fc_pool_manager.config import PoolConfig


class TestPoolConfig:
    def test_load_from_yaml(self, tmp_path):
        yaml_content = """
pool:
  size: 3
  max_vms: 10
  health_check_interval: 15

vm_defaults:
  vcpu: 2
  mem_mib: 1024
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "console=ttyS0 init=/init ip={vm_ip}::10.0.0.1:255.255.255.0::eth0:off"

network:
  bridge: testbr0
  subnet: "10.0.0.0/24"
  gateway: "10.0.0.1"
  vm_ip_start: 2

jailer:
  enabled: true
  chroot_base: /tmp/jailer
  exec_path: /usr/bin/firecracker
  uid: 1000
  gid: 1000
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_content)

        cfg = PoolConfig.from_yaml(str(config_file))

        assert cfg.pool_size == 3
        assert cfg.max_vms == 10
        assert cfg.health_check_interval == 15
        assert cfg.vm_vcpu == 2
        assert cfg.vm_mem_mib == 1024
        assert cfg.vm_kernel == "/opt/fc/vmlinux"
        assert cfg.vm_rootfs == "/opt/fc/rootfs.ext4"
        assert "{vm_ip}" in cfg.boot_args_template
        assert cfg.bridge == "testbr0"
        assert cfg.subnet == "10.0.0.0/24"
        assert cfg.gateway == "10.0.0.1"
        assert cfg.vm_ip_start == 2
        assert cfg.jailer_enabled is True
        assert cfg.chroot_base == "/tmp/jailer"
        assert cfg.firecracker_path == "/usr/bin/firecracker"
        assert cfg.jailer_uid == 1000
        assert cfg.jailer_gid == 1000

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            PoolConfig.from_yaml("/nonexistent/config.yaml")
