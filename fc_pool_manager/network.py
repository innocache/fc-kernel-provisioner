"""Network management: IP allocation and TAP device lifecycle.

The IPAllocator is behind an interface for future multi-host support.
NetworkManager handles TAP creation/teardown via subprocess calls to `ip`.
"""

import asyncio
import subprocess
from typing import Protocol


class IPAllocatorProtocol(Protocol):
    """Interface for IP allocation — swap for multi-host or external IPAM."""

    def allocate(self) -> str: ...
    def release(self, ip: str) -> None: ...
    @property
    def available(self) -> int: ...


class IPAllocator:
    """Set-based IP allocator for a single /24 subnet."""

    def __init__(self, gateway: str, start: int, end: int):
        self._gateway = gateway
        self._prefix = gateway.rsplit(".", 1)[0]
        self._free: list[int] = list(range(start, end + 1))
        self._allocated: set[int] = set()

    def allocate(self) -> str:
        if not self._free:
            raise RuntimeError("IP address pool exhausted")
        octet = self._free.pop(0)
        self._allocated.add(octet)
        return f"{self._prefix}.{octet}"

    def release(self, ip: str) -> None:
        octet = int(ip.rsplit(".", 1)[1])
        if octet in self._allocated:
            self._allocated.discard(octet)
            self._free.insert(0, octet)

    @property
    def available(self) -> int:
        return len(self._free)


class NetworkManager:
    """Manages TAP devices and IP allocation for Firecracker VMs."""

    def __init__(self, bridge: str, gateway: str, vm_ip_start: int, vm_ip_end: int = 254):
        self.bridge = bridge
        self.ip_allocator = IPAllocator(gateway=gateway, start=vm_ip_start, end=vm_ip_end)

    def tap_name(self, short_id: str) -> str:
        """Generate TAP device name from the short UUID hex (8 chars)."""
        return f"tap-{short_id}"

    def mac_from_ip(self, ip: str) -> str:
        """Generate a deterministic MAC address from the VM IP."""
        last_octet = int(ip.rsplit(".", 1)[1])
        return f"AA:FC:00:00:00:{last_octet:02X}"

    # Keep private aliases for backward compatibility
    def _tap_name(self, short_id: str) -> str:
        return self.tap_name(short_id)

    def _mac_from_ip(self, ip: str) -> str:
        return self.mac_from_ip(ip)

    def allocate_ip(self) -> str:
        return self.ip_allocator.allocate()

    def release_ip(self, ip: str) -> None:
        self.ip_allocator.release(ip)

    async def create_tap(self, short_id: str) -> str:
        """Create a TAP device and attach it to the bridge. Returns TAP name."""
        tap = self._tap_name(short_id)
        await self._run("ip", "tuntap", "add", tap, "mode", "tap")
        await self._run("ip", "link", "set", tap, "master", self.bridge)
        await self._run("ip", "link", "set", tap, "up")
        return tap

    async def delete_tap(self, tap_name: str) -> None:
        """Delete a TAP device (auto-detaches from bridge)."""
        try:
            await self._run("ip", "link", "del", tap_name)
        except subprocess.CalledProcessError:
            pass

    async def _run(self, *cmd: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=stderr)
