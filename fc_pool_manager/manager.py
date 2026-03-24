"""Pool manager — maintains a pool of pre-warmed Firecracker microVMs."""

import asyncio
import logging
import os
import secrets
import shutil
import time
from typing import Any, Optional

from .config import PoolConfig
from .firecracker_api import FirecrackerAPI
from .metrics import (
    ACQUIRE_DURATION,
    ACQUIRE_TOTAL,
    AUTO_CULL_TOTAL,
    BOOT_DURATION,
    HEALTH_CHECK_FAILURES_TOTAL,
    POOL_MAX_VMS,
    POOL_VMS_TOTAL,
    RELEASE_TOTAL,
)
from .network import NetworkManager
from .snapshot import SnapshotManager
from .vm import CIDAllocator, VMInstance, VMState

logger = logging.getLogger(__name__)

_CULL_INTERVAL = 60


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
        self._acquire_lock = asyncio.Lock()
        self._kernel_to_vm: dict[str, str] = {}
        self._snapshot = SnapshotManager(
            snapshot_dir=config.snapshot_dir,
            kernel_path=config.vm_kernel,
            rootfs_path=config.vm_rootfs,
            firecracker_path=config.firecracker_path,
        )
        self._snapshot_checked = False
        self._snapshot_valid = False
        POOL_MAX_VMS.set(config.max_vms)

    def _update_vm_gauges(self) -> None:
        counts = {s.value: 0 for s in VMState}
        for vm in self._vms.values():
            counts[vm.state.value] += 1
        for state, count in counts.items():
            POOL_VMS_TOTAL.labels(state=state).set(count)

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

    def bind_kernel(self, vm_id: str, kernel_id: str) -> None:
        self._kernel_to_vm[kernel_id] = vm_id

    def vm_by_kernel(self, kernel_id: str) -> dict[str, Any] | None:
        vm_id = self._kernel_to_vm.get(kernel_id)
        if vm_id is None:
            return None
        vm = self._vms.get(vm_id)
        if vm is None or vm.state != VMState.ASSIGNED:
            return None
        return {"vm_id": vm.vm_id, "ip": vm.ip, "vsock_path": vm.vsock_path}

    def clear_kernel_bindings_for_vm(self, vm_id: str) -> None:
        stale = [kid for kid, mapped in self._kernel_to_vm.items() if mapped == vm_id]
        for kid in stale:
            self._kernel_to_vm.pop(kid, None)

    async def acquire(self, vcpu: int, mem_mib: int) -> dict[str, Any]:
        """Claim an idle VM from the pool."""
        t0 = asyncio.get_event_loop().time()
        try:
            if vcpu != self._config.vm_vcpu or mem_mib != self._config.vm_mem_mib:
                raise ValueError(
                    f"Requested resources (vcpu={vcpu}, mem_mib={mem_mib}) "
                    f"do not match pool profile "
                    f"(vcpu={self._config.vm_vcpu}, mem_mib={self._config.vm_mem_mib})"
                )
            result = await self._acquire_inner(vcpu, mem_mib)
            ACQUIRE_DURATION.observe(asyncio.get_event_loop().time() - t0)
            ACQUIRE_TOTAL.labels(result="success").inc()
            self._update_vm_gauges()
            return result
        except ValueError:
            ACQUIRE_TOTAL.labels(result="invalid_request").inc()
            raise
        except RuntimeError as e:
            if "pool_exhausted" in str(e):
                ACQUIRE_TOTAL.labels(result="exhausted").inc()
            else:
                ACQUIRE_TOTAL.labels(result="error").inc()
            raise
        except Exception:
            ACQUIRE_TOTAL.labels(result="error").inc()
            raise

    async def _acquire_inner(self, vcpu: int, mem_mib: int) -> dict[str, Any]:
        async with self._acquire_lock:
            for vm in self._vms.values():
                if vm.state == VMState.IDLE:
                    vm.transition_to(VMState.ASSIGNED)
                    logger.info("Acquired VM %s (ip=%s, cid=%d)", vm.vm_id, vm.ip, vm.cid)
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
        if destroy:
            async with self._acquire_lock:
                vm = self._vms.pop(vm_id, None)
                if vm is None:
                    logger.warning("Release called for unknown VM %s", vm_id)
                    return
                if vm.state != VMState.STOPPING:
                    vm.transition_to(VMState.STOPPING)
            self.clear_kernel_bindings_for_vm(vm_id)
            await self._destroy_vm(vm)
            logger.info("Destroyed VM %s", vm_id)
        else:
            vm = self._vms.get(vm_id)
            if vm is None:
                logger.warning("Release called for unknown VM %s", vm_id)
                return
            try:
                from .vsock import vsock_request
                await vsock_request(vm.vsock_path, {"action": "reset"}, timeout=10)
            except Exception as exc:
                logger.warning(
                    "Failed to reset guest for VM %s before releasing to idle pool: %s",
                    vm_id,
                    exc,
                )
            vm.transition_to(VMState.IDLE)
            logger.info("Released VM %s back to idle pool", vm_id)

        RELEASE_TOTAL.inc()
        self._update_vm_gauges()

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

    async def _boot_vm(self, use_snapshot: bool = True) -> VMInstance:
        short_id = secrets.token_hex(8)
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
            await self._prepare_jail_root(vm)

            await self._network.create_tap(short_id)
            await self._network.apply_vm_rules(
                tap_name, ip,
                self._config.rate_limit_mbit,
                self._config.allowed_host_ports,
            )

            if use_snapshot and self._snapshot_valid:
                await self._restore_from_snapshot(vm)
            else:
                await self._full_boot(vm)

            await self._wait_for_guest_agent(vm)

            vm.transition_to(VMState.IDLE)
            boot_secs = asyncio.get_event_loop().time() - vm.created_at
            BOOT_DURATION.observe(boot_secs)
            logger.info("VM %s booted (ip=%s, cid=%d)", vm_id, ip, cid)
            self._update_vm_gauges()
            return vm

        except Exception:
            await self._destroy_vm(vm)
            del self._vms[vm_id]
            raise

    async def _prepare_jail_root(self, vm: VMInstance) -> None:
        os.makedirs(vm.jail_path, exist_ok=True)
        kernel_dest = os.path.join(vm.jail_path, "vmlinux")
        if not os.path.exists(kernel_dest):
            os.link(self._config.vm_kernel, kernel_dest)
        overlay_dest = os.path.join(vm.jail_path, "overlay.ext4")
        await self._run_subprocess("cp", "--reflink=auto", self._config.vm_rootfs, overlay_dest)

        uid = self._config.jailer_uid
        gid = self._config.jailer_gid
        os.chown(kernel_dest, uid, gid)
        os.chown(overlay_dest, uid, gid)

    async def _start_jailer(self, vm: VMInstance) -> str:
        jailer_cmd = [
            "jailer", "--id", vm.vm_id,
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

        api_socket = os.path.join(vm.jail_path, "run", "firecracker.socket")
        await self._wait_for_socket(api_socket, timeout=10)
        return api_socket

    async def _full_boot(self, vm: VMInstance) -> None:
        boot_args = self._config.boot_args_template.replace("{vm_ip}", vm.ip)
        api_socket = await self._start_jailer(vm)

        api = FirecrackerAPI(api_socket)
        await api.configure_machine(self._config.vm_vcpu, self._config.vm_mem_mib)
        await api.configure_boot_source("vmlinux", boot_args)
        await api.configure_drive("rootfs", "overlay.ext4", is_root=True)
        await api.configure_network("eth0", vm.tap_name, vm.mac)
        await api.configure_vsock(vm.cid, "v.sock")
        await api.configure_entropy()
        await api.start()

    async def _restore_from_snapshot(self, vm: VMInstance) -> None:
        vmstate_dest = os.path.join(vm.jail_path, "vmstate")
        memory_dest = os.path.join(vm.jail_path, "memory")
        for path in (vmstate_dest, memory_dest):
            if os.path.exists(path):
                os.remove(path)
        os.link(self._snapshot.vmstate_path, vmstate_dest)
        os.link(self._snapshot.memory_path, memory_dest)
        os.chown(vmstate_dest, self._config.jailer_uid, self._config.jailer_gid)
        os.chown(memory_dest, self._config.jailer_uid, self._config.jailer_gid)

        golden_tap = self._snapshot.golden_tap_name
        if golden_tap and golden_tap != vm.tap_name:
            await self._network._run(
                "ip", "link", "set", vm.tap_name, "name", golden_tap,
            )

        current_tap = golden_tap if golden_tap and golden_tap != vm.tap_name else vm.tap_name
        await self._network.detach_from_bridge(current_tap)

        api_socket = await self._start_jailer(vm)
        api = FirecrackerAPI(api_socket)
        await api.load_snapshot(
            snapshot_path="vmstate",
            mem_path="memory",
        )
        await api.resume()

        if golden_tap and golden_tap != vm.tap_name:
            await self._network._run(
                "ip", "link", "set", golden_tap, "name", vm.tap_name,
            )

        from .vsock import vsock_request
        try:
            resp = await vsock_request(
                vm.vsock_path,
                {
                    "action": "reconfigure_network",
                    "ip": vm.ip,
                    "mac": vm.mac,
                    "gateway": self._config.gateway,
                },
                timeout=10,
            )
            if resp.get("status") != "ok":
                logger.warning("reconfigure_network failed for %s: %s", vm.vm_id, resp)
            else:
                logger.info("Reconfigured network for %s: ip=%s mac=%s", vm.vm_id, vm.ip, vm.mac)
        except Exception as exc:
            logger.warning("reconfigure_network vsock error for %s: %s", vm.vm_id, exc)
        finally:
            await self._network.attach_to_bridge(vm.tap_name)

    async def _wait_for_guest_agent(self, vm: VMInstance) -> None:
        from .vsock import vsock_request

        boot_deadline = asyncio.get_event_loop().time() + 30
        while True:
            try:
                resp = await vsock_request(vm.vsock_path, {"action": "ping"}, timeout=5)
                if resp.get("status") == "alive":
                    return
                raise RuntimeError(f"Guest agent not ready: {resp}")
            except (ConnectionError, OSError):
                if asyncio.get_event_loop().time() > boot_deadline:
                    raise RuntimeError("Guest agent did not start within 30s")
                await asyncio.sleep(0.5)

    async def create_golden_snapshot(self) -> None:
        vm: VMInstance | None = None
        try:
            vm = await self._boot_ephemeral_vm()
            api_socket = os.path.join(vm.jail_path, "run", "firecracker.socket")
            api = FirecrackerAPI(api_socket)
            await api.pause()
            await api.create_snapshot("vmstate", "memory")

            os.makedirs(self._config.snapshot_dir, exist_ok=True)
            await asyncio.to_thread(
                shutil.copy2,
                os.path.join(vm.jail_path, "vmstate"),
                self._snapshot.vmstate_path,
            )
            await asyncio.to_thread(
                shutil.copy2,
                os.path.join(vm.jail_path, "memory"),
                self._snapshot.memory_path,
            )
            self._snapshot.save_metadata(golden_tap_name=vm.tap_name)
            self._snapshot_valid = True
            logger.info("Created golden snapshot in %s", self._config.snapshot_dir)
        except Exception:
            self._snapshot.invalidate()
            self._snapshot_valid = False
            raise
        finally:
            if vm is not None:
                await self._destroy_vm(vm)

    async def _boot_ephemeral_vm(self) -> VMInstance:
        short_id = secrets.token_hex(8)
        vm_id = f"vm-snap-{short_id}"
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

        try:
            await self._prepare_jail_root(vm)
            await self._network.create_tap(short_id)
            await self._network.apply_vm_rules(
                tap_name, ip,
                self._config.rate_limit_mbit,
                self._config.allowed_host_ports,
            )
            await self._full_boot(vm)
            await self._wait_for_guest_agent(vm)
            return vm
        except Exception:
            await self._destroy_vm(vm)
            raise

    async def ensure_golden_snapshot(self) -> None:
        if self._snapshot_checked:
            return
        self._snapshot_checked = True
        if self._snapshot.has_valid_snapshot():
            self._snapshot_valid = True
            return
        if not (
            os.path.isfile(self._config.vm_kernel)
            and os.path.isfile(self._config.vm_rootfs)
            and os.path.isfile(self._config.firecracker_path)
        ):
            return
        os.makedirs(self._config.snapshot_dir, exist_ok=True)
        if os.stat(self._config.snapshot_dir).st_dev != os.stat(self._config.chroot_base).st_dev:
            logger.info("Snapshot dir and chroot_base on different filesystems — skipping snapshots")
            return
        logger.info("Creating golden snapshot...")
        try:
            await self.create_golden_snapshot()
        except Exception as exc:
            logger.warning("Failed to create golden snapshot: %s", exc)

    async def _destroy_vm(self, vm: VMInstance) -> None:
        """Tear down a VM: kill jailer, delete TAP, remove jail dir."""
        if vm.jailer_process and vm.jailer_process.returncode is None:
            vm.jailer_process.terminate()
            try:
                await asyncio.wait_for(vm.jailer_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                vm.jailer_process.kill()
                await vm.jailer_process.wait()

        await self._network.remove_vm_rules(
            vm.tap_name, vm.ip,
            self._config.rate_limit_mbit,
            self._config.allowed_host_ports,
        )
        await self._network.delete_tap(vm.tap_name)
        self._network.release_ip(vm.ip)
        self._cid_alloc.release(vm.cid)

        if os.path.exists(vm.jail_path):
            await asyncio.to_thread(shutil.rmtree, vm.jail_path, ignore_errors=True)

    async def replenish(self) -> None:
        """Boot VMs until idle count meets pool_size."""
        await self.ensure_golden_snapshot()
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
        self._update_vm_gauges()

    async def health_check_loop(self) -> None:
        """Periodically ping idle VMs and replace unhealthy ones."""
        while True:
            await asyncio.sleep(self._config.health_check_interval)
            async with self._acquire_lock:
                for vm in list(self._vms.values()):
                    if vm.state != VMState.IDLE:
                        continue
                    health = await self.is_alive(vm.vm_id)
                    if not health["alive"]:
                        logger.warning("VM %s unhealthy, replacing", vm.vm_id)
                        HEALTH_CHECK_FAILURES_TOTAL.inc()
                        vm.transition_to(VMState.STOPPING)
                        await self._destroy_vm(vm)
                        del self._vms[vm.vm_id]
            self._update_vm_gauges()
            await self.replenish()

    async def auto_cull_loop(self) -> None:
        if self._config.vm_idle_timeout == 0:
            logger.info("Auto-cull disabled (vm_idle_timeout=0)")
            return

        while True:
            await asyncio.sleep(_CULL_INTERVAL)
            now = time.monotonic()
            to_cull: list[VMInstance] = []

            async with self._acquire_lock:
                for vm in list(self._vms.values()):
                    if vm.state != VMState.ASSIGNED:
                        continue
                    if vm.assigned_at is None:
                        continue
                    if now - vm.assigned_at <= self._config.vm_idle_timeout:
                        continue
                    try:
                        vm.transition_to(VMState.STOPPING)
                    except ValueError:
                        logger.debug("VM %s already stopping, skipping cull", vm.vm_id)
                        continue
                    del self._vms[vm.vm_id]
                    to_cull.append(vm)

            for vm in to_cull:
                age = now - (vm.assigned_at or now)
                logger.warning(
                    "Auto-culling VM %s (assigned %.0fs ago, timeout=%ds)",
                    vm.vm_id, age, self._config.vm_idle_timeout,
                )
                try:
                    from .vsock import vsock_request
                    await asyncio.wait_for(
                        vsock_request(
                            vm.vsock_path,
                            {"action": "signal", "signum": 15},
                            timeout=5,
                        ),
                        timeout=5,
                    )
                    await asyncio.sleep(5)
                except Exception as exc:
                    logger.debug(
                        "Vsock signal to %s failed (continuing): %s", vm.vm_id, exc
                    )
                try:
                    await self._destroy_vm(vm)
                except Exception as exc:
                    logger.warning("Failed to destroy culled VM %s: %s", vm.vm_id, exc)
                AUTO_CULL_TOTAL.inc()

            if to_cull:
                logger.info("Auto-culled %d VM(s), replenishing pool", len(to_cull))
                self._update_vm_gauges()
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
        self._kernel_to_vm.clear()

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
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if os.path.exists(path):
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"Socket {path} did not appear within {timeout}s")
