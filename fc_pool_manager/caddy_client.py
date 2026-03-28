"""CaddyClient — manage dashboard routes and vsock messages."""

import asyncio
import json
import struct
from typing import Any

import aiohttp

HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
GUEST_AGENT_PORT = 52


class CaddyClient:
    def __init__(self, admin_url: str = "http://localhost:2019"):
        self._admin_url = admin_url.rstrip("/")

    def _route_id(self, session_id: str) -> str:
        return f"dashboard_{session_id}"

    def _build_route(self, route_id: str, upstream: str) -> dict[str, Any]:
        return {
            "@id": self._route_id(route_id),
            "match": [{"path": [f"/dash/{route_id}/*"]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": upstream}],
                    "flush_interval": -1,
                },
            ],
        }

    async def _discover_server_key(self, http: aiohttp.ClientSession) -> str:
        resp = await http.get(f"{self._admin_url}/config/apps/http/servers")
        if resp.status != 200:
            return "srv0"
        servers = await resp.json()
        if isinstance(servers, dict) and servers:
            return next(iter(servers))
        return "srv0"

    async def add_route(self, route_id: str, upstream: str) -> None:
        route = self._build_route(route_id, upstream)
        rid = self._route_id(route_id)
        put_url = f"{self._admin_url}/id/{rid}"

        async with aiohttp.ClientSession() as http:
            resp = await http.put(put_url, json=route)
            if resp.status == 404:
                server_key = await self._discover_server_key(http)
                add_url = f"{self._admin_url}/config/apps/http/servers/{server_key}/routes/0"
                resp = await http.post(add_url, json=route)
            if resp.status not in (200, 201):
                body = await resp.text()
                raise RuntimeError(f"Caddy add_route failed ({resp.status}): {body}")

    async def remove_route(self, route_id: str) -> None:
        rid = self._route_id(route_id)
        del_url = f"{self._admin_url}/id/{rid}"
        async with aiohttp.ClientSession() as http:
            resp = await http.delete(del_url)
            if resp.status not in (200, 204, 404):
                body = await resp.text()
                raise RuntimeError(f"Caddy remove_route failed ({resp.status}): {body}")

    async def add_vm_route(self, vm_id: str, vm_ip: str, port: int = 8888) -> None:
        route = {
            "@id": f"vm_{vm_id}",
            "match": [{"path": [f"/vm/{vm_id}/*"]}],
            "handle": [
                {
                    "handler": "rewrite",
                    "strip_path_prefix": f"/vm/{vm_id}",
                },
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": f"{vm_ip}:{port}"}],
                    "flush_interval": -1,
                },
            ],
        }
        put_url = f"{self._admin_url}/id/vm_{vm_id}"
        async with aiohttp.ClientSession() as http:
            resp = await http.put(put_url, json=route)
            if resp.status == 404:
                server_key = await self._discover_server_key(http)
                add_url = f"{self._admin_url}/config/apps/http/servers/{server_key}/routes/0"
                resp = await http.post(add_url, json=route)
            if resp.status not in (200, 201):
                body = await resp.text()
                raise RuntimeError(f"Caddy add_vm_route failed ({resp.status}): {body}")

    async def remove_vm_route(self, vm_id: str) -> None:
        del_url = f"{self._admin_url}/id/vm_{vm_id}"
        async with aiohttp.ClientSession() as http:
            resp = await http.delete(del_url)
            if resp.status not in (200, 204, 404):
                body = await resp.text()
                raise RuntimeError(f"Caddy remove_vm_route failed ({resp.status}): {body}")


async def _vsock_request(vsock_uds_path: str, msg: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(vsock_uds_path)
    try:
        writer.write(f"CONNECT {GUEST_AGENT_PORT}\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line.startswith(b"OK"):
            raise ConnectionError(f"vsock handshake failed: {line.decode().strip()}")

        payload = json.dumps(msg).encode()
        writer.write(struct.pack(HEADER_FMT, len(payload)) + payload)
        await writer.drain()

        header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=timeout)
        length = struct.unpack(HEADER_FMT, header)[0]
        body = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(body)
    finally:
        writer.close()
        await writer.wait_closed()
