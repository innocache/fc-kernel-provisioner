"""YAML configuration loader for the pool manager."""

from dataclasses import dataclass
import yaml


@dataclass(frozen=True)
class PoolConfig:
    """Typed, immutable configuration for the pool manager."""

    pool_size: int
    max_vms: int
    health_check_interval: int

    vm_vcpu: int
    vm_mem_mib: int
    vm_kernel: str
    vm_rootfs: str
    boot_args_template: str

    bridge: str
    subnet: str
    gateway: str
    vm_ip_start: int

    jailer_enabled: bool
    chroot_base: str
    firecracker_path: str
    jailer_uid: int
    jailer_gid: int

    @classmethod
    def from_yaml(cls, path: str) -> "PoolConfig":
        with open(path) as f:
            raw = yaml.safe_load(f)

        pool = raw["pool"]
        vm = raw["vm_defaults"]
        net = raw["network"]
        jail = raw["jailer"]

        return cls(
            pool_size=pool["size"],
            max_vms=pool["max_vms"],
            health_check_interval=pool.get("health_check_interval", 30),
            vm_vcpu=vm["vcpu"],
            vm_mem_mib=vm["mem_mib"],
            vm_kernel=vm["kernel"],
            vm_rootfs=vm["rootfs"],
            boot_args_template=vm["boot_args_template"],
            bridge=net["bridge"],
            subnet=net["subnet"],
            gateway=net["gateway"],
            vm_ip_start=net["vm_ip_start"],
            jailer_enabled=jail["enabled"],
            chroot_base=jail["chroot_base"],
            firecracker_path=jail["exec_path"],
            jailer_uid=jail["uid"],
            jailer_gid=jail["gid"],
        )
