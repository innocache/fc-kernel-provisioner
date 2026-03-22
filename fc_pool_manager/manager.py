"""Pool manager — maintains a pool of pre-warmed Firecracker microVMs."""

import asyncio
import logging
import os
import shutil
import uuid
from typing import Any, Optional

from .config import PoolConfig
from .firecracker_api import FirecrackerAPI
from .network import NetworkManager
from .vm import CIDAllocator, VMInstance, VMState

logger = logging.getLogger(__name__)


class PoolManager:
    """Manages a pool of Firecracker microVMs."""

    def __init__(self, config: PoolConfig):
        self._config = config
        self._vms: dict[str, VMInstance] = {}
        self._network = NetworkManager(
            bridge=config.bridge,
            gateway=config.gateway,
            vm_ip_start=config.vm_ip_start,
        )
        self._cid_alloc = CIDAllocator()
        self._boot_lock = asyncio.Lock()

    @property
    def idle_count(self) -> int:
        return sum(1 for vm in self._vms.values() if vm.state == VMState.IDLE)

    @property
    def total_count(self) -> int:
        return len(self._vms)

    def pool_status(self) -> dict[str, int]:
        counts: dict[str, Any] = {"idle": 0, "assigned": 0, "booting": 0}
        for vm in self._vms.values():
            key = vm.state.value
            if key in counts:
                counts[key] += 1
        counts["max"] = self._config.max_vms
        return counts

    async def acquire(self, vcpu: int, mem_mib: int) -> dict[str, Any]:
        """Claim an idle VM from the pool."""
        if vcpu != self._config.vm_vcpu or mem_mib != self._config.vm_mem_mib:
            raise ValueError(
                f"Requested resources (vcpu={vcpu}, mem_mib={mem_mib}) "
                f"do not match pool profile "
                f"(vcpu={self._config.vm_vcpu}, mem_mib={self._config.vm_mem_mib})"
            )

        for vm in self._vms.values():
            if vm.state == VMState.IDLE:
                vm.transition_to(VMState.ASSIGNED)
                logger.info("Acquired VM %s (ip=%s)", vm.vm_id, vm.ip)
                asyncio.create_task(self.replenish())  # backfill pool
                return {
                    "id": vm.vm_id,
                    "ip": vm.ip,
                    "vsock_path": vm.vsock_path,
                }

        if self.total_count >= self._config.max_vms:
            raise RuntimeError("pool_exhausted")

        # No idle VMs but under max — boot one on demand
        logger.info("No idle VMs, booting on demand")
        vm = await self._boot_vm()
        vm.transition_to(VMState.ASSIGNED)
        return {
            "id": vm.vm_id,
            "ip": vm.ip,
            "vsock_path": vm.vsock_path,
        }

    async def release(self, vm_id: str, destroy: bool = True) -> None:
        """Release a VM back to the pool or destroy it."""
        vm = self._vms.get(vm_id)
        if vm is None:
            logger.warning("Release called for unknown VM %s", vm_id)
            return

        if destroy:
            vm.transition_to(VMState.STOPPING)
            await self._destroy_vm(vm)
            del self._vms[vm_id]
            logger.info("Destroyed VM %s", vm_id)

    async def is_alive(self, vm_id: str) -> dict[str, Any]:
        """Check if a VM is alive by pinging the guest agent."""
        vm = self._vms.get(vm_id)
        if vm is None:
            return {"alive": False}

        try:
            from .vsock import vsock_request
            resp = await vsock_request(vm.vsock_path, {"action": "ping"}, timeout=5)
            return {
                "alive": resp.get("status") == "alive",
                "uptime": resp.get("uptime", 0),
                "kernel_alive": resp.get("kernel_alive", False),
            }
        except Exception:
            return {"alive": False}

    async def _boot_vm(self) -> VMInstance:
        """Boot a new jailed Firecracker VM."""
        short_id = uuid.uuid4().hex[:8]
        vm_id = f"vm-{short_id}"
        ip = self._network.allocate_ip()
        cid = self._cid_alloc.allocate()
        tap_name = self._network._tap_name(short_id)
        mac = self._network._mac_from_ip(ip)

        jail_path = os.path.join(
            self._config.chroot_base, "firecracker", vm_id, "root"
        )
        vsock_path = os.path.join(jail_path, "v.sock")

        vm = VMInstance(
            vm_id=vm_id, short_id=short_id, ip=ip, cid=cid,
            tap_name=tap_name, mac=mac,
            jail_path=jail_path, vsock_path=vsock_path,
        )
        self._vms[vm_id] = vm

        try:
            os.makedirs(jail_path, exist_ok=True)
            kernel_dest = os.path.join(jail_path, "vmlinux")
            if not os.path.exists(kernel_dest):
                os.link(self._config.vm_kernel, kernel_dest)
            overlay_dest = os.path.join(jail_path, "overlay.ext4")
            # Use cp --reflink=auto for CoW on supported filesystems (btrfs, xfs)
            await self._run_subprocess("cp", "--reflink=auto", self._config.vm_rootfs, overlay_dest)

            await self._network.create_tap(short_id)

            boot_args = self._config.boot_args_template.format(vm_ip=ip)
            jailer_cmd = [
                "jailer", "--id", vm_id,
                "--exec-file", self._config.firecracker_path,
                "--uid", str(self._config.jailer_uid),
                "--gid", str(self._config.jailer_gid),
                "--chroot-base-dir", self._config.chroot_base,
            ]
            jailer_proc = await asyncio.create_subprocess_exec(
                *jailer_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            vm.jailer_process = jailer_proc

            api_socket = os.path.join(jail_path, "run", "firecracker.socket")
            await self._wait_for_socket(api_socket, timeout=10)

            api = FirecrackerAPI(api_socket)
            await api.configure_machine(self._config.vm_vcpu, self._config.vm_mem_mib)
            await api.configure_boot_source("vmlinux", boot_args)
            await api.configure_drive("rootfs", "overlay.ext4", is_root=True)
            await api.configure_network("eth0", tap_name, mac)
            await api.configure_vsock(cid, "v.sock")
            await api.start()

            from .vsock import vsock_request
            resp = await vsock_request(vsock_path, {"action": "ping"}, timeout=30)
            if resp.get("status") != "alive":
                raise RuntimeError(f"Guest agent not ready: {resp}")

            vm.transition_to(VMState.IDLE)
            logger.info("VM %s booted (ip=%s, cid=%d)", vm_id, ip, cid)
            return vm

        except Exception:
            await self._destroy_vm(vm)
            del self._vms[vm_id]
            raise

    async def _destroy_vm(self, vm: VMInstance) -> None:
        """Tear down a VM: kill jailer, delete TAP, remove jail dir."""
        if vm.jailer_process and vm.jailer_process.returncode is None:
            vm.jailer_process.terminate()
            try:
                await asyncio.wait_for(vm.jailer_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                vm.jailer_process.kill()

        await self._network.delete_tap(vm.tap_name)
        self._network.release_ip(vm.ip)
        self._cid_alloc.release(vm.cid)

        if os.path.exists(vm.jail_path):
            await asyncio.to_thread(shutil.rmtree, vm.jail_path, ignore_errors=True)

    async def replenish(self) -> None:
        """Boot VMs until idle count meets pool_size."""
        async with self._boot_lock:
            while (
                self.idle_count < self._config.pool_size
                and self.total_count < self._config.max_vms
            ):
                count_before = self.total_count
                try:
                    await self._boot_vm()
                except Exception as e:
                    logger.error("Failed to boot VM: %s", e)
                    break
                # If total_count didn't increase, boot made no progress — stop.
                if self.total_count <= count_before:
                    break

    async def health_check_loop(self) -> None:
        """Periodically ping idle VMs and replace unhealthy ones."""
        while True:
            await asyncio.sleep(self._config.health_check_interval)
            for vm in list(self._vms.values()):
                if vm.state != VMState.IDLE:
                    continue
                health = await self.is_alive(vm.vm_id)
                if not health["alive"]:
                    logger.warning("VM %s unhealthy, replacing", vm.vm_id)
                    vm.transition_to(VMState.STOPPING)
                    await self._destroy_vm(vm)
                    del self._vms[vm.vm_id]
            await self.replenish()

    async def shutdown(self) -> None:
        """Gracefully stop all VMs."""
        logger.info("Shutting down pool manager, stopping %d VMs", len(self._vms))
        for vm in list(self._vms.values()):
            try:
                if vm.state != VMState.STOPPING:
                    vm.transition_to(VMState.STOPPING)
                await self._destroy_vm(vm)
            except Exception as e:
                logger.error("Error stopping VM %s: %s", vm.vm_id, e)
        self._vms.clear()

    async def _run_subprocess(self, *cmd: str) -> None:
        """Run an external command as a subprocess."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Command {cmd} failed: {stderr.decode()}")

    async def _wait_for_socket(self, path: str, timeout: float) -> None:
        """Wait for a Unix socket file to appear."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if os.path.exists(path):
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"Socket {path} did not appear within {timeout}s")
