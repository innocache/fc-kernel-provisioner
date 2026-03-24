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

    def _build_route(self, session_id: str, upstream: str) -> dict[str, Any]:
        return {
            "@id": self._route_id(session_id),
            "match": [{"path": [f"/dash/{session_id}/*"]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": upstream}],
                    "flush_interval": -1,
                }
            ],
        }

    async def add_route(self, session_id: str, upstream: str) -> None:
        route = self._build_route(session_id, upstream)
        route_id = self._route_id(session_id)
        put_url = f"{self._admin_url}/id/{route_id}"
        add_url = f"{self._admin_url}/config/apps/http/servers/main/routes/0"

        async with aiohttp.ClientSession() as http:
            resp = await http.put(put_url, json=route)
            if resp.status == 404:
                resp = await http.post(add_url, json=route)
            if resp.status not in (200, 201):
                body = await resp.text()
                raise RuntimeError(f"Caddy add_route failed ({resp.status}): {body}")

    async def remove_route(self, session_id: str) -> None:
        route_id = self._route_id(session_id)
        del_url = f"{self._admin_url}/id/{route_id}"
        async with aiohttp.ClientSession() as http:
            resp = await http.delete(del_url)
            if resp.status not in (200, 204, 404):
                body = await resp.text()
                raise RuntimeError(f"Caddy remove_route failed ({resp.status}): {body}")


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
