"""Network management: IP allocation and TAP device lifecycle.

The IPAllocator is behind an interface for future multi-host support.
NetworkManager handles TAP creation/teardown via subprocess calls to `ip`.
"""

import asyncio
import logging
import subprocess
from typing import Protocol

logger = logging.getLogger(__name__)


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

    def _tap_name(self, short_id: str) -> str:
        """Generate TAP device name from the short UUID hex.

        Linux interface names are limited to 15 chars (IFNAMSIZ - 1).
        """
        return f"tap-{short_id[:11]}"

    def _mac_from_ip(self, ip: str) -> str:
        """Generate a deterministic MAC address from the VM IP."""
        last_octet = int(ip.rsplit(".", 1)[1])
        return f"AA:FC:00:00:00:{last_octet:02X}"

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
        try:
            await self._run("ip", "link", "del", tap_name)
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to delete TAP %s: %s", tap_name, exc)

    async def apply_vm_rules(
        self, tap_name: str, vm_ip: str, rate_limit_mbit: int, allowed_host_ports: tuple[int, ...],
    ) -> None:
        if rate_limit_mbit > 0:
            await self._run(
                "tc", "qdisc", "add", "dev", tap_name, "root",
                "tbf", "rate", f"{rate_limit_mbit}mbit",
                "burst", "32kbit", "latency", "400ms",
            )

        await self._run(
            "iptables", "-I", "INPUT",
            "-i", self.bridge, "-s", vm_ip,
            "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
            "-j", "ACCEPT",
        )
        for port in allowed_host_ports:
            for proto in ("tcp", "udp"):
                await self._run(
                    "iptables", "-I", "INPUT",
                    "-i", self.bridge, "-s", vm_ip,
                    "-p", proto, "--dport", str(port),
                    "-j", "ACCEPT",
                )
        await self._run(
            "iptables", "-A", "INPUT",
            "-i", self.bridge, "-s", vm_ip,
            "-j", "DROP",
        )

    async def remove_vm_rules(
        self, tap_name: str, vm_ip: str, rate_limit_mbit: int, allowed_host_ports: tuple[int, ...],
    ) -> None:
        if rate_limit_mbit > 0:
            try:
                await self._run("tc", "qdisc", "del", "dev", tap_name, "root")
            except subprocess.CalledProcessError:
                pass

        try:
            await self._run(
                "iptables", "-D", "INPUT",
                "-i", self.bridge, "-s", vm_ip,
                "-j", "DROP",
            )
        except subprocess.CalledProcessError:
            pass

        for port in allowed_host_ports:
            for proto in ("tcp", "udp"):
                try:
                    await self._run(
                        "iptables", "-D", "INPUT",
                        "-i", self.bridge, "-s", vm_ip,
                        "-p", proto, "--dport", str(port),
                        "-j", "ACCEPT",
                    )
                except subprocess.CalledProcessError:
                    pass

        try:
            await self._run(
                "iptables", "-D", "INPUT",
                "-i", self.bridge, "-s", vm_ip,
                "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
                "-j", "ACCEPT",
            )
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
