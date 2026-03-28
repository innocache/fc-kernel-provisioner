from __future__ import annotations

from urllib.parse import unquote

import aiohttp


class PoolClient:
    def __init__(self, base_url: str):
        """base_url can be http+unix:///var/run/fc-pool.sock or http://host:port"""
        self._base_url = base_url
        self._session: aiohttp.ClientSession

        if base_url.startswith("http+unix://"):
            socket_path = unquote(base_url[len("http+unix://") :])
            connector = aiohttp.UnixConnector(path=socket_path)
            self._api_origin = "http://localhost"
            self._session = aiohttp.ClientSession(connector=connector)
        else:
            self._api_origin = base_url.rstrip("/")
            self._session = aiohttp.ClientSession()

    async def acquire(self, vcpu: int = 1, mem_mib: int = 512) -> dict:
        """POST /api/vms/acquire -> {vm_id, ip, ...}"""
        async with self._session.post(
            f"{self._api_origin}/api/vms/acquire",
            json={"vcpu": vcpu, "mem_mib": mem_mib},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def destroy(self, vm_id: str) -> None:
        """DELETE /api/vms/{vm_id}"""
        async with self._session.delete(f"{self._api_origin}/api/vms/{vm_id}") as resp:
            if resp.status == 404:
                return
            resp.raise_for_status()

    async def health_check(self, vm_ip: str, port: int = 8888) -> bool:
        """GET http://{vm_ip}:{port}/api/kernels -> True if 200"""
        url = f"http://{vm_ip}:{port}/api/kernels"
        try:
            async with self._session.get(url) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._session.close()
