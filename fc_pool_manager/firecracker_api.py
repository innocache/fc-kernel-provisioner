"""Async REST client for the Firecracker microVM API.

Firecracker exposes a REST API on a Unix domain socket. Each VM has its own
socket inside the jailed directory. All paths in API calls are relative to
the chroot root (not absolute host paths).
"""

import aiohttp
from typing import Any


class FirecrackerAPI:
    """Client for a single Firecracker VM's REST API socket."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._base_url = "http://localhost"

    def _connector(self) -> aiohttp.UnixConnector:
        return aiohttp.UnixConnector(path=self.socket_path)

    async def _put(self, path: str, body: dict[str, Any]) -> None:
        async with aiohttp.ClientSession(connector=self._connector()) as session:
            resp = await session.put(f"{self._base_url}{path}", json=body)
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(
                    f"Firecracker API error: PUT {path} -> {resp.status}: {text}"
                )

    def _machine_config_body(self, vcpu: int, mem_mib: int) -> dict:
        return {"vcpu_count": vcpu, "mem_size_mib": mem_mib}

    def _boot_source_body(self, kernel_path: str, boot_args: str) -> dict:
        return {"kernel_image_path": kernel_path, "boot_args": boot_args}

    def _drive_body(self, drive_id: str, path: str, is_root: bool) -> dict:
        return {
            "drive_id": drive_id,
            "path_on_host": path,
            "is_root_device": is_root,
            "is_read_only": False,
        }

    def _network_body(self, iface_id: str, tap_name: str, mac: str) -> dict:
        return {
            "iface_id": iface_id,
            "host_dev_name": tap_name,
            "guest_mac": mac,
        }

    def _vsock_body(self, cid: int, uds_path: str) -> dict:
        return {"guest_cid": cid, "uds_path": uds_path}

    async def configure_machine(self, vcpu: int, mem_mib: int) -> None:
        await self._put("/machine-config", self._machine_config_body(vcpu, mem_mib))

    async def configure_boot_source(self, kernel_path: str, boot_args: str) -> None:
        await self._put("/boot-source", self._boot_source_body(kernel_path, boot_args))

    async def configure_drive(self, drive_id: str, path: str, is_root: bool = True) -> None:
        await self._put(f"/drives/{drive_id}", self._drive_body(drive_id, path, is_root))

    async def configure_network(self, iface_id: str, tap_name: str, mac: str) -> None:
        await self._put(
            f"/network-interfaces/{iface_id}",
            self._network_body(iface_id, tap_name, mac),
        )

    async def configure_vsock(self, cid: int, uds_path: str) -> None:
        await self._put("/vsock", self._vsock_body(cid, uds_path))

    async def start(self) -> None:
        await self._put("/actions", {"action_type": "InstanceStart"})
