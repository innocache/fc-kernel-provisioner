import subprocess
from unittest.mock import AsyncMock, call, patch

import pytest

from fc_pool_manager.network import NetworkManager


@pytest.fixture
def net():
    return NetworkManager(bridge="fcbr0", gateway="172.16.0.1", vm_ip_start=2)


class TestApplyVmRules:
    async def test_tc_rate_limit_applied(self, net):
        net._run = AsyncMock()
        await net.apply_vm_rules("tap-abc", "172.16.0.2", rate_limit_mbit=10, allowed_host_ports=())
        tc_call = net._run.call_args_list[0]
        assert tc_call == call(
            "tc", "qdisc", "add", "dev", "tap-abc", "root",
            "tbf", "rate", "10mbit", "burst", "32kbit", "latency", "400ms",
        )

    async def test_tc_skipped_when_rate_zero(self, net):
        net._run = AsyncMock()
        await net.apply_vm_rules("tap-abc", "172.16.0.2", rate_limit_mbit=0, allowed_host_ports=())
        tc_calls = [c for c in net._run.call_args_list if c.args[0] == "tc"]
        assert tc_calls == []

    async def test_iptables_whitelist_ports(self, net):
        net._run = AsyncMock()
        await net.apply_vm_rules("tap-abc", "172.16.0.2", rate_limit_mbit=0, allowed_host_ports=(53, 8888))
        ipt_calls = [c for c in net._run.call_args_list if c.args[0] == "iptables"]
        assert len(ipt_calls) == 6
        assert ipt_calls[0] == call(
            "iptables", "-I", "INPUT", "-i", "fcbr0", "-s", "172.16.0.2",
            "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
            "-j", "ACCEPT",
        )
        assert ipt_calls[1] == call(
            "iptables", "-I", "INPUT", "-i", "fcbr0", "-s", "172.16.0.2",
            "-p", "tcp", "--dport", "53", "-j", "ACCEPT",
        )
        assert ipt_calls[2] == call(
            "iptables", "-I", "INPUT", "-i", "fcbr0", "-s", "172.16.0.2",
            "-p", "udp", "--dport", "53", "-j", "ACCEPT",
        )
        assert ipt_calls[3] == call(
            "iptables", "-I", "INPUT", "-i", "fcbr0", "-s", "172.16.0.2",
            "-p", "tcp", "--dport", "8888", "-j", "ACCEPT",
        )
        assert ipt_calls[4] == call(
            "iptables", "-I", "INPUT", "-i", "fcbr0", "-s", "172.16.0.2",
            "-p", "udp", "--dport", "8888", "-j", "ACCEPT",
        )
        assert ipt_calls[5] == call(
            "iptables", "-A", "INPUT", "-i", "fcbr0", "-s", "172.16.0.2",
            "-j", "DROP",
        )

    async def test_drop_rule_always_added(self, net):
        net._run = AsyncMock()
        await net.apply_vm_rules("tap-abc", "172.16.0.2", rate_limit_mbit=0, allowed_host_ports=())
        ipt_calls = [c for c in net._run.call_args_list if c.args[0] == "iptables"]
        assert len(ipt_calls) == 2
        assert "ESTABLISHED,RELATED" in ipt_calls[0].args
        assert "DROP" in ipt_calls[1].args

    async def test_combined_tc_and_iptables(self, net):
        net._run = AsyncMock()
        await net.apply_vm_rules("tap-x", "172.16.0.5", rate_limit_mbit=100, allowed_host_ports=(53,))
        all_cmds = [c.args[0] for c in net._run.call_args_list]
        assert all_cmds[0] == "tc"
        assert all(c == "iptables" for c in all_cmds[1:])


class TestRemoveVmRules:
    async def test_tc_removed(self, net):
        net._run = AsyncMock()
        await net.remove_vm_rules("tap-abc", "172.16.0.2", rate_limit_mbit=10, allowed_host_ports=())
        tc_call = net._run.call_args_list[0]
        assert tc_call == call("tc", "qdisc", "del", "dev", "tap-abc", "root")

    async def test_tc_removal_failure_ignored(self, net):
        async def _run_side_effect(*cmd):
            if cmd[0] == "tc":
                raise subprocess.CalledProcessError(1, cmd)
        net._run = AsyncMock(side_effect=_run_side_effect)
        await net.remove_vm_rules("tap-abc", "172.16.0.2", rate_limit_mbit=10, allowed_host_ports=())

    async def test_iptables_rules_removed(self, net):
        net._run = AsyncMock()
        await net.remove_vm_rules("tap-abc", "172.16.0.2", rate_limit_mbit=0, allowed_host_ports=(53,))
        ipt_calls = [c for c in net._run.call_args_list if c.args[0] == "iptables"]
        assert len(ipt_calls) == 4
        assert ipt_calls[0] == call(
            "iptables", "-D", "INPUT", "-i", "fcbr0", "-s", "172.16.0.2",
            "-j", "DROP",
        )
        assert ipt_calls[1] == call(
            "iptables", "-D", "INPUT", "-i", "fcbr0", "-s", "172.16.0.2",
            "-p", "tcp", "--dport", "53", "-j", "ACCEPT",
        )
        assert ipt_calls[2] == call(
            "iptables", "-D", "INPUT", "-i", "fcbr0", "-s", "172.16.0.2",
            "-p", "udp", "--dport", "53", "-j", "ACCEPT",
        )
        assert ipt_calls[3] == call(
            "iptables", "-D", "INPUT", "-i", "fcbr0", "-s", "172.16.0.2",
            "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
            "-j", "ACCEPT",
        )

    async def test_iptables_removal_failure_ignored(self, net):
        net._run = AsyncMock(side_effect=subprocess.CalledProcessError(1, "iptables"))
        await net.remove_vm_rules("tap-abc", "172.16.0.2", rate_limit_mbit=0, allowed_host_ports=(53,))

    async def test_tc_skipped_when_rate_zero(self, net):
        net._run = AsyncMock()
        await net.remove_vm_rules("tap-abc", "172.16.0.2", rate_limit_mbit=0, allowed_host_ports=())
        tc_calls = [c for c in net._run.call_args_list if c.args[0] == "tc"]
        assert tc_calls == []


class TestDeleteTap:
    async def test_delete_tap_runs_ip_link_del(self, net):
        net._run = AsyncMock()
        await net.delete_tap("tap-abc")
        net._run.assert_awaited_once_with("ip", "link", "del", "tap-abc")

    async def test_delete_tap_failure_logged_not_raised(self, net):
        net._run = AsyncMock(side_effect=subprocess.CalledProcessError(1, "ip"))
        await net.delete_tap("tap-abc")


class TestConfigParsing:
    def test_rate_limit_default(self, tmp_path):
        from fc_pool_manager.config import PoolConfig
        yaml_content = """
pool:
  size: 3
  max_vms: 10
vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "ip={vm_ip}"
network:
  bridge: br0
  subnet: "10.0.0.0/24"
  gateway: "10.0.0.1"
  vm_ip_start: 2
jailer:
  enabled: true
  chroot_base: /tmp/j
  exec_path: /usr/bin/firecracker
  uid: 1000
  gid: 1000
"""
        f = tmp_path / "c.yaml"
        f.write_text(yaml_content)
        cfg = PoolConfig.from_yaml(str(f))
        assert cfg.rate_limit_mbit == 10
        assert cfg.allowed_host_ports == (53,)

    def test_rate_limit_explicit(self, tmp_path):
        from fc_pool_manager.config import PoolConfig
        yaml_content = """
pool:
  size: 3
  max_vms: 10
vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "ip={vm_ip}"
network:
  bridge: br0
  subnet: "10.0.0.0/24"
  gateway: "10.0.0.1"
  vm_ip_start: 2
  rate_limit_mbit: 50
  allowed_host_ports: [53, 8888]
jailer:
  enabled: true
  chroot_base: /tmp/j
  exec_path: /usr/bin/firecracker
  uid: 1000
  gid: 1000
"""
        f = tmp_path / "c.yaml"
        f.write_text(yaml_content)
        cfg = PoolConfig.from_yaml(str(f))
        assert cfg.rate_limit_mbit == 50
        assert cfg.allowed_host_ports == (53, 8888)

    def test_rate_limit_disabled(self, tmp_path):
        from fc_pool_manager.config import PoolConfig
        yaml_content = """
pool:
  size: 3
  max_vms: 10
vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/fc/vmlinux
  rootfs: /opt/fc/rootfs.ext4
  boot_args_template: "ip={vm_ip}"
network:
  bridge: br0
  subnet: "10.0.0.0/24"
  gateway: "10.0.0.1"
  vm_ip_start: 2
  rate_limit_mbit: 0
  allowed_host_ports: []
jailer:
  enabled: true
  chroot_base: /tmp/j
  exec_path: /usr/bin/firecracker
  uid: 1000
  gid: 1000
"""
        f = tmp_path / "c.yaml"
        f.write_text(yaml_content)
        cfg = PoolConfig.from_yaml(str(f))
        assert cfg.rate_limit_mbit == 0
        assert cfg.allowed_host_ports == ()
