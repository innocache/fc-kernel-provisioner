from __future__ import annotations

from urllib.parse import unquote

import aiohttp


class PoolClient:
    def __init__(self, base_url: str):
        self._base_url = base_url
        self._session: aiohttp.ClientSession | None = None

        if base_url.startswith("http+unix://"):
            self._socket_path = unquote(base_url[len("http+unix://"):])
            self._api_origin = "http://localhost"
        else:
            self._socket_path = None
            self._api_origin = base_url.rstrip("/")

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.UnixConnector(path=self._socket_path) if self._socket_path else None
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def acquire(self, vcpu: int = 1, mem_mib: int = 512) -> dict:
        session = self._get_session()
        async with session.post(
            f"{self._api_origin}/api/vms/acquire",
            json={"vcpu": vcpu, "mem_mib": mem_mib},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def destroy(self, vm_id: str) -> None:
        session = self._get_session()
        async with session.delete(f"{self._api_origin}/api/vms/{vm_id}") as resp:
            if resp.status == 404:
                return
            resp.raise_for_status()

    async def health_check(self, vm_ip: str, port: int = 8888) -> bool:
        session = self._get_session()
        url = f"http://{vm_ip}:{port}/api/kernels"
        try:
            async with session.get(url) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
