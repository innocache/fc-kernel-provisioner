"""FirecrackerProvisioner — launches Jupyter kernels inside Firecracker microVMs."""

import asyncio
from typing import Any, Optional

from jupyter_client.provisioning import KernelProvisionerBase

from .pool_client import PoolClient
from .vsock_client import vsock_request, vsock_send_only


class FirecrackerProcess:
    """Process-like handle for a kernel running inside a Firecracker VM."""

    def __init__(self, vm_id: str, pool_client: PoolClient):
        self.vm_id = vm_id
        self.pool_client = pool_client
        self._exit_code: Optional[int] = None

    async def poll(self) -> Optional[int]:
        if self._exit_code is not None:
            return self._exit_code
        alive = await self.pool_client.is_alive(self.vm_id)
        if not alive:
            self._exit_code = 1
            return 1
        return None

    async def kill(self):
        await self.pool_client.release(self.vm_id, destroy=True)
        self._exit_code = -9

    async def terminate(self):
        await self.kill()

    def send_signal(self, signum: int):
        pass


class FirecrackerProvisioner(KernelProvisionerBase):
    """Jupyter kernel provisioner that runs kernels in Firecracker microVMs."""

    pool_socket: str = "/var/run/fc-pool.sock"
    vcpu_count: int = 1
    mem_size_mib: int = 512

    vm_id: Optional[str] = None
    vm_ip: Optional[str] = None
    vsock_path: Optional[str] = None
    process: Optional[FirecrackerProcess] = None
    pool_client: Optional[PoolClient] = None

    KERNEL_PORTS = {
        "shell_port": 5555,
        "iopub_port": 5556,
        "stdin_port": 5557,
        "control_port": 5558,
        "hb_port": 5559,
    }

    def _apply_config(self):
        config = self.kernel_spec.metadata.get(
            "kernel_provisioner", {}
        ).get("config", {})
        self.pool_socket = config.get("pool_socket", self.pool_socket)
        self.vcpu_count = config.get("vcpu_count", self.vcpu_count)
        self.mem_size_mib = config.get("mem_size_mib", self.mem_size_mib)

    async def pre_launch(self, **kwargs) -> dict[str, Any]:
        self._apply_config()
        self.pool_client = PoolClient(self.pool_socket)

        vm = await self.pool_client.acquire(
            vcpu=self.vcpu_count, mem_mib=self.mem_size_mib,
        )
        self.vm_id = vm["id"]
        self.vm_ip = vm["ip"]
        self.vsock_path = vm["vsock_path"]

        kwargs["cmd"] = []
        return await super().pre_launch(**kwargs)

    async def launch_kernel(self, cmd: list[str], **kwargs) -> dict:
        """Launch the kernel inside the Firecracker VM (required abstract method)."""
        return await self.launch_process(cmd, **kwargs)

    async def launch_process(self, cmd: list[str], **kwargs) -> FirecrackerProcess:
        conn_info = self.connection_info
        key = conn_info.get("key", "")

        resp = await vsock_request(
            self.vsock_path,
            {"action": "start_kernel", "ports": self.KERNEL_PORTS, "key": key},
            timeout=30,
        )

        if resp.get("status") != "ready":
            raise RuntimeError(
                f"Guest agent failed to start kernel: {resp.get('error', 'unknown')}"
            )

        self.connection_info["ip"] = self.vm_ip
        self.connection_info["transport"] = "tcp"
        self.connection_info.update(self.KERNEL_PORTS)

        self.process = FirecrackerProcess(self.vm_id, self.pool_client)
        return self.process

    @property
    def has_process(self) -> bool:
        return self.process is not None

    async def poll(self) -> Optional[int]:
        if self.process is None:
            return 0
        return await self.process.poll()

    async def wait(self) -> Optional[int]:
        if self.process is None:
            return 0
        while True:
            status = await self.process.poll()
            if status is not None:
                return status
            await asyncio.sleep(1)

    async def send_signal(self, signum: int):
        if self.vsock_path:
            await vsock_send_only(
                self.vsock_path, {"action": "signal", "signum": signum},
            )

    async def kill(self, restart: bool = False):
        if self.process:
            await self.process.kill()

    async def terminate(self, restart: bool = False):
        await self.kill(restart=restart)

    async def cleanup(self, restart: bool = False):
        if restart and self.vsock_path:
            await vsock_request(
                self.vsock_path,
                {
                    "action": "restart_kernel",
                    "ports": self.KERNEL_PORTS,
                    "key": self.connection_info.get("key", ""),
                },
                timeout=30,
            )
        elif self.vm_id and self.pool_client:
            await self.pool_client.release(self.vm_id, destroy=True)
            self.vm_id = None
            self.vm_ip = None
            self.vsock_path = None
            self.process = None

    async def get_provisioner_info(self) -> dict[str, Any]:
        info = await super().get_provisioner_info()
        info["vm_id"] = self.vm_id
        info["vm_ip"] = self.vm_ip
        info["vsock_path"] = self.vsock_path
        info["pool_socket"] = self.pool_socket
        return info

    async def load_provisioner_info(self, info: dict[str, Any]):
        await super().load_provisioner_info(info)
        self.vm_id = info.get("vm_id")
        self.vm_ip = info.get("vm_ip")
        self.vsock_path = info.get("vsock_path")
        self.pool_socket = info.get("pool_socket", self.pool_socket)
        if self.vm_id:
            self.pool_client = PoolClient(self.pool_socket)
            self.process = FirecrackerProcess(self.vm_id, self.pool_client)
