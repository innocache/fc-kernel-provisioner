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
        self._state_lock = asyncio.Lock()

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

        async with self._state_lock:
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

        # No idle VMs but under max — boot one on demand.
        # _boot_lock serializes concurrent on-demand boots so total_count
        # cannot exceed max_vms even with concurrent acquire() calls.
        logger.info("No idle VMs, booting on demand")
        async with self._boot_lock:
            if self.total_count >= self._config.max_vms:
                raise RuntimeError("pool_exhausted")
            vm = await self._boot_vm()

        # The freshly booted VM is IDLE in _vms.  Re-acquire _state_lock to
        # atomically verify it was not claimed by a concurrent acquire() and
        # then transition it to ASSIGNED.
        async with self._state_lock:
            if self._vms.get(vm.vm_id) is not vm or vm.state != VMState.IDLE:
                raise RuntimeError("pool_exhausted")
            vm.transition_to(VMState.ASSIGNED)
        return {
            "id": vm.vm_id,
            "ip": vm.ip,
            "vsock_path": vm.vsock_path,
        }

    async def release(self, vm_id: str) -> None:
        """Destroy and remove a VM from the pool."""
        async with self._state_lock:
            vm = self._vms.get(vm_id)
            if vm is None:
                logger.warning("Release called for unknown VM %s", vm_id)
                return
            vm.transition_to(VMState.STOPPING)
            del self._vms[vm_id]
            logger.info("Destroyed VM %s", vm_id)
        else:
            # Best-effort reset/stop of the guest before returning to the idle pool.
            try:
                from .vsock import vsock_request
                await vsock_request(vm.vsock_path, {"action": "reset"}, timeout=10)
            except Exception as exc:
                logger.warning(
                    "Failed to reset guest for VM %s before releasing to idle pool: %s",
                    vm_id,
                    exc,
                )
            async with self._pool_lock:
                vm.transition_to(VMState.IDLE)
            logger.info("Released VM %s back to idle pool", vm_id)
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
                try:
                    os.link(self._config.vm_kernel, kernel_dest)
                except OSError:
                    shutil.copy2(self._config.vm_kernel, kernel_dest)
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
        """Boot VMs up to pool_size when idle count drops below replenish_threshold."""
        if self.idle_count >= self._config.replenish_threshold:
            return
        async with self._boot_lock:
            if self.idle_count >= self._config.replenish_threshold:
                return
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
            async with self._state_lock:
                idle_vms = [vm for vm in self._vms.values() if vm.state == VMState.IDLE]
            for vm in idle_vms:
                health = await self.is_alive(vm.vm_id)
                if not health["alive"]:
                    logger.warning("VM %s unhealthy, replacing", vm.vm_id)
                    should_destroy = False
                    async with self._state_lock:
                        current = self._vms.get(vm.vm_id)
                        if current is vm and vm.state == VMState.IDLE:
                            vm.transition_to(VMState.STOPPING)
                            del self._vms[vm.vm_id]
                            should_destroy = True
                    if should_destroy:
                        await self._destroy_vm(vm)
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
