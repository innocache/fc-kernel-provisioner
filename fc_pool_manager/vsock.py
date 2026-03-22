"""Minimal vsock communication for pool manager health checks.

This is a self-contained copy of the vsock protocol helpers that the pool
manager needs for boot verification and health checks. The provisioner
package (fc_provisioner) has its own full-featured vsock_client module.
Both use the same wire protocol: 4-byte big-endian length + JSON payload.
"""

import asyncio
import json
import struct
from typing import Any

GUEST_AGENT_PORT = 52
HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)


async def vsock_request(
    vsock_uds_path: str,
    msg: dict[str, Any],
    timeout: float = 30,
) -> dict[str, Any]:
    """Send a request to the guest agent and return the response."""
    reader, writer = await asyncio.open_unix_connection(vsock_uds_path)
    try:
        # Firecracker vsock handshake
        writer.write(f"CONNECT {GUEST_AGENT_PORT}\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line.startswith(b"OK"):
            raise ConnectionError(f"Vsock handshake failed: {line.decode().strip()}")

        # Send request
        payload = json.dumps(msg).encode()
        writer.write(struct.pack(HEADER_FMT, len(payload)) + payload)
        await writer.drain()

        # Read response
        header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=timeout)
        length = struct.unpack(HEADER_FMT, header)[0]
        resp_data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(resp_data)
    finally:
        writer.close()
        await writer.wait_closed()
