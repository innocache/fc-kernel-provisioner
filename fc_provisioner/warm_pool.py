import asyncio
import logging
from typing import Any, ClassVar, Optional

from .pool_client import PoolClient
from .provisioner import FirecrackerProvisioner

logger = logging.getLogger(__name__)

_DEFAULT_POOL_TARGET = 3
_REPLENISH_RETRY_DELAY = 5.0
_REPLENISH_POLL_INTERVAL = 1.0


class WarmPoolProvisioner(FirecrackerProvisioner):

    _warm_pool: ClassVar[Optional[asyncio.Queue]] = None
    _pool_target: ClassVar[int] = _DEFAULT_POOL_TARGET
    _pool_client: ClassVar[Optional[PoolClient]] = None
    _replenish_task: ClassVar[Optional[asyncio.Task]] = None
    _initialized: ClassVar[bool] = False
    _vcpu: ClassVar[int] = 1
    _mem_mib: ClassVar[int] = 512

    @classmethod
    def _ensure_initialized(cls, pool_socket: str, vcpu: int, mem_mib: int) -> None:
        if cls._initialized:
            return
        cls._initialized = True
        if cls._warm_pool is None:
            cls._warm_pool = asyncio.Queue()
        cls._pool_client = PoolClient(pool_socket)
        cls._vcpu = vcpu
        cls._mem_mib = mem_mib
        cls._replenish_task = asyncio.ensure_future(cls._replenish_loop())
        logger.info(
            "WarmPoolProvisioner initialized (target=%d, socket=%s)",
            cls._pool_target, pool_socket,
        )

    @classmethod
    async def _replenish_loop(cls) -> None:
        while True:
            while cls._warm_pool.qsize() < cls._pool_target:
                try:
                    vm = await cls._pool_client.acquire(
                        vcpu=cls._vcpu, mem_mib=cls._mem_mib,
                    )
                    if vm.get("kernel_key") and vm.get("kernel_ports"):
                        await cls._warm_pool.put(vm)
                        logger.info(
                            "Warm pool: added %s (pool=%d/%d)",
                            vm["id"], cls._warm_pool.qsize(), cls._pool_target,
                        )
                    else:
                        logger.warning(
                            "Warm pool: VM %s has no pre-warmed kernel, releasing",
                            vm["id"],
                        )
                        await cls._pool_client.release(vm["id"], destroy=True)
                except Exception as exc:
                    logger.warning("Warm pool replenish failed: %s", exc)
                    await asyncio.sleep(_REPLENISH_RETRY_DELAY)
                    break
            await asyncio.sleep(_REPLENISH_POLL_INTERVAL)

    @classmethod
    def _check_replenish_health(cls) -> None:
        if cls._replenish_task is not None and cls._replenish_task.done():
            exc = cls._replenish_task.exception() if not cls._replenish_task.cancelled() else None
            if exc:
                logger.error("Replenish loop died: %s — restarting", exc)
            else:
                logger.warning("Replenish loop exited — restarting")
            cls._replenish_task = asyncio.ensure_future(cls._replenish_loop())

    async def _get_valid_vm(self) -> dict | None:
        if self._warm_pool is None or self._warm_pool.empty():
            return None
        try:
            return self._warm_pool.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def pre_launch(self, **kwargs) -> dict[str, Any]:
        self._apply_config()
        self._ensure_initialized(self.pool_socket, self.vcpu_count, self.mem_size_mib)
        self._check_replenish_health()

        vm = await self._get_valid_vm()
        if vm is None:
            logger.warning("Warm pool empty or all stale, falling back to cold acquire")
            return await super().pre_launch(**kwargs)
        logger.info("Warm pool: serving %s to kernel (pool=%d)", vm["id"], self._warm_pool.qsize())

        self.pool_client = self._pool_client
        self.vm_id = vm["id"]
        self.vm_ip = vm["ip"]
        self.vsock_path = vm["vsock_path"]
        self.kernel_key = vm.get("kernel_key")
        self.kernel_ports = vm.get("kernel_ports")

        

        kwargs["cmd"] = []
        from jupyter_client.provisioning import KernelProvisionerBase
        result = await KernelProvisionerBase.pre_launch(self, **kwargs)

        self.connection_info["ip"] = self.vm_ip
        self.connection_info["transport"] = "tcp"
        if hasattr(self, "parent") and hasattr(self.parent, "session"):
            self.parent.session.key = b""
            self.connection_info["key"] = b""
            self.connection_info["signature_scheme"] = "hmac-sha256"

        if self.kernel_ports:
            for port_name, port_value in self.kernel_ports.items():
                self.connection_info[port_name] = port_value

        if self.vm_id and getattr(self, "kernel_id", None):
            try:
                await self._pool_client.bind_kernel(self.vm_id, self.kernel_id)
            except Exception:
                pass

        return result

    async def cleanup(self, restart: bool = False):
        if not restart and self.vm_id and self.pool_client:
            await self.pool_client.release(self.vm_id, destroy=True)
            self.vm_id = None
            self.vm_ip = None
            self.vsock_path = None
            self.kernel_key = None
            self.kernel_ports = None
            self.process = None
        else:
            await super().cleanup(restart=restart)
