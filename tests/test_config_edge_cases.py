"""Edge case tests for configuration loading."""

import pytest
from fc_pool_manager.config import PoolConfig


def write_config(tmp_path, content):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(content)
    return str(config_file)


VALID_CONFIG = """\
pool:
  size: 5
  max_vms: 30
  health_check_interval: 30
vm_defaults:
  vcpu: 2
  mem_mib: 1024
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "console=ttyS0 ip={vm_ip}::172.16.0.1:255.255.255.0::eth0:off"
network:
  bridge: fcbr0
  subnet: "172.16.0.0/24"
  gateway: "172.16.0.1"
  vm_ip_start: 2
jailer:
  enabled: true
  chroot_base: /srv/jailer
  exec_path: /usr/bin/firecracker
  uid: 1000
  gid: 1000
"""


class TestConfigEdgeCases:
    def test_valid_config_loads(self, tmp_path):
        path = write_config(tmp_path, VALID_CONFIG)
        config = PoolConfig.from_yaml(path)
        assert config.pool_size == 5
        assert config.max_vms == 30
        assert config.vm_vcpu == 2
        assert config.vm_mem_mib == 1024

    def test_missing_pool_section_raises(self, tmp_path):
        content = """\
vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "test"
network:
  bridge: fcbr0
  subnet: "172.16.0.0/24"
  gateway: "172.16.0.1"
  vm_ip_start: 2
jailer:
  enabled: true
  chroot_base: /srv/jailer
  exec_path: /usr/bin/firecracker
  uid: 1000
  gid: 1000
"""
        path = write_config(tmp_path, content)
        with pytest.raises(KeyError):
            PoolConfig.from_yaml(path)

    def test_missing_vm_defaults_raises(self, tmp_path):
        content = """\
pool:
  size: 5
  max_vms: 30
  health_check_interval: 30
network:
  bridge: fcbr0
  subnet: "172.16.0.0/24"
  gateway: "172.16.0.1"
  vm_ip_start: 2
jailer:
  enabled: true
  chroot_base: /srv/jailer
  exec_path: /usr/bin/firecracker
  uid: 1000
  gid: 1000
"""
        path = write_config(tmp_path, content)
        with pytest.raises(KeyError):
            PoolConfig.from_yaml(path)

    def test_missing_network_section_raises(self, tmp_path):
        content = """\
pool:
  size: 5
  max_vms: 30
  health_check_interval: 30
vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "test"
jailer:
  enabled: true
  chroot_base: /srv/jailer
  exec_path: /usr/bin/firecracker
  uid: 1000
  gid: 1000
"""
        path = write_config(tmp_path, content)
        with pytest.raises(KeyError):
            PoolConfig.from_yaml(path)

    def test_missing_jailer_section_raises(self, tmp_path):
        content = """\
pool:
  size: 5
  max_vms: 30
  health_check_interval: 30
vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "test"
network:
  bridge: fcbr0
  subnet: "172.16.0.0/24"
  gateway: "172.16.0.1"
  vm_ip_start: 2
"""
        path = write_config(tmp_path, content)
        with pytest.raises(KeyError):
            PoolConfig.from_yaml(path)

    def test_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError):
            PoolConfig.from_yaml("/nonexistent/path/config.yaml")

    def test_empty_file_raises(self, tmp_path):
        path = write_config(tmp_path, "")
        with pytest.raises((TypeError, AttributeError)):
            PoolConfig.from_yaml(path)

    def test_invalid_yaml_raises(self, tmp_path):
        path = write_config(tmp_path, "{{invalid yaml: [}")
        with pytest.raises(Exception):
            PoolConfig.from_yaml(path)

    def test_config_is_frozen(self, tmp_path):
        """PoolConfig should be immutable (frozen dataclass)."""
        path = write_config(tmp_path, VALID_CONFIG)
        config = PoolConfig.from_yaml(path)
        with pytest.raises(AttributeError):
            config.pool_size = 999

    def test_extra_fields_in_yaml_are_ignored(self, tmp_path):
        """Extra fields in YAML should not cause errors."""
        content = VALID_CONFIG + "\nextra_field: should_be_ignored\n"
        path = write_config(tmp_path, content)
        config = PoolConfig.from_yaml(path)
        assert config.pool_size == 5  # Still loads correctly

    def test_boot_args_template_with_placeholder(self, tmp_path):
        """boot_args_template should contain {vm_ip} placeholder."""
        path = write_config(tmp_path, VALID_CONFIG)
        config = PoolConfig.from_yaml(path)
        formatted = config.boot_args_template.replace("{vm_ip}", "172.16.0.2")
        assert "172.16.0.2" in formatted
