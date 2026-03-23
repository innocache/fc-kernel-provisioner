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
        health = await self.pool_client.is_alive(self.vm_id)
        if not health.get("alive", False):
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

    PORT_NAMES = ("shell_port", "iopub_port", "stdin_port", "control_port", "hb_port")
    _DEFAULT_PORTS = {
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
        result = await super().pre_launch(**kwargs)

        # Populate connection_info so that the dict returned from launch_kernel
        # matches what KernelManager.get_connection_info() produces after
        # _reconcile_connection_info loads it.  The key must be bytes and
        # signature_scheme must be present, otherwise the equality check in
        # _equal_connections fails (str != bytes, None != "hmac-sha256").
        self.connection_info["ip"] = self.vm_ip
        self.connection_info["transport"] = "tcp"
        if hasattr(self, "parent") and hasattr(self.parent, "session"):
            self.connection_info["key"] = self.parent.session.key
            self.connection_info["signature_scheme"] = (
                self.parent.session.signature_scheme
            )
        return result

    def _kernel_ports(self) -> dict[str, int]:
        """Extract kernel port assignments from connection_info (set by KernelManager).

        Falls back to default fixed ports when called outside the full KM flow
        (e.g. in unit tests or direct usage).
        """
        return {k: self.connection_info.get(k, self._DEFAULT_PORTS[k]) for k in self.PORT_NAMES}

    def _connection_key_text(self) -> str:
        key = self.connection_info.get("key", "")
        if isinstance(key, bytes):
            return key.decode()
        return key

    async def _start_guest_kernel(self) -> None:
        """Send start_kernel to guest agent and update connection info."""
        if self.vsock_path is None:
            raise RuntimeError("Cannot start guest kernel without a vsock path")
        if self.vm_id is None or self.pool_client is None:
            raise RuntimeError("Cannot start guest kernel before VM acquisition")

        key = self._connection_key_text()
        ports = self._kernel_ports()

        resp = await vsock_request(
            self.vsock_path,
            {"action": "start_kernel", "ports": ports, "key": key, "ip": self.vm_ip},
            timeout=120,
        )

        if resp.get("status") != "ready":
            error_msg = resp.get("message") or resp.get("error") or "unknown"
            raise RuntimeError(
                f"Guest agent failed to start kernel: {error_msg}"
            )

        if self.vm_ip:
            self.connection_info["ip"] = self.vm_ip
        self.connection_info["transport"] = "tcp"
        self.connection_info.update(ports)
        self.process = FirecrackerProcess(self.vm_id, self.pool_client)

    async def launch_kernel(self, cmd: list[str], **kwargs) -> dict[str, Any]:
        """Launch the kernel inside the Firecracker VM via vsock and return connection info."""
        await self._start_guest_kernel()
        return self.connection_info

    async def launch_process(self, cmd: list[str], **kwargs) -> FirecrackerProcess:
        """Alias kept for backward compatibility / direct testing."""
        await self._start_guest_kernel()
        assert self.process is not None
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
            if self.vm_id is None or self.pool_client is None:
                raise RuntimeError("Cannot restart guest kernel before VM acquisition")
            resp = await vsock_request(
                self.vsock_path,
                {
                    "action": "restart_kernel",
                    "ports": self._kernel_ports(),
                    "key": self._connection_key_text(),
                },
                timeout=120,
            )
            if resp.get("status") != "ready":
                error_msg = resp.get("message") or resp.get("error") or "unknown"
                raise RuntimeError(f"Guest agent failed to restart kernel: {error_msg}")
            self.process = FirecrackerProcess(self.vm_id, self.pool_client)
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
