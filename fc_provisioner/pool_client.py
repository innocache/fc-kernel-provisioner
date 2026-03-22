"""Async client for the pool manager's Unix socket API."""

import aiohttp
from typing import Any


class PoolClient:
    """Communicates with the pool manager daemon over a Unix domain socket."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._base_url = "http://localhost"

    def _connector(self) -> aiohttp.UnixConnector:
        return aiohttp.UnixConnector(path=self.socket_path)

    async def acquire(self, vcpu: int = 1, mem_mib: int = 512) -> dict[str, Any]:
        """Claim a pre-warmed VM from the pool."""
        async with aiohttp.ClientSession(connector=self._connector()) as session:
            resp = await session.post(
                f"{self._base_url}/api/vms/acquire",
                json={"vcpu": vcpu, "mem_mib": mem_mib},
            )
            if resp.status == 503:
                data = await resp.json()
                raise RuntimeError(data.get("error", "pool_exhausted"))
            if resp.status == 400:
                data = await resp.json()
                raise ValueError(data.get("error", "bad request"))
            resp.raise_for_status()
            return await resp.json()

    async def release(self, vm_id: str, destroy: bool = True) -> None:
        """Release a VM back to the pool."""
        async with aiohttp.ClientSession(connector=self._connector()) as session:
            resp = await session.delete(
                f"{self._base_url}/api/vms/{vm_id}",
                json={"destroy": destroy},
            )
            resp.raise_for_status()

    async def is_alive(self, vm_id: str) -> dict[str, Any]:
        """Check if a VM is still running. Returns full health dict."""
        async with aiohttp.ClientSession(connector=self._connector()) as session:
            resp = await session.get(f"{self._base_url}/api/vms/{vm_id}/health")
            if resp.status == 200:
                return await resp.json()
            return {"alive": False}
