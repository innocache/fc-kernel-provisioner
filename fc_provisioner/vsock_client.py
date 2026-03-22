"""Host-side vsock communication with the guest agent.

Firecracker maps guest AF_VSOCK to a host-side Unix domain socket.
The host connects to that UDS and sends "CONNECT <port>\n" to reach
the guest agent listening on that port.

IMPORTANT: The guest agent is single-threaded and responds on the same
connection. vsock_request() sends AND receives on a single connection.
"""

import asyncio
import json
import logging
import struct
from typing import Any

logger = logging.getLogger(__name__)

GUEST_AGENT_PORT = 52
_SENSITIVE_KEYS = frozenset({"key", "token", "password", "secret"})


def _safe_log_msg(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *msg* with sensitive fields redacted for logging."""
    return {k: "<redacted>" if k in _SENSITIVE_KEYS else v for k, v in msg.items()}


HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

logger = logging.getLogger(__name__)


def _encode_message(msg: dict[str, Any]) -> bytes:
    """Encode a dict as length-prefixed JSON."""
    payload = json.dumps(msg).encode()
    return struct.pack(HEADER_FMT, len(payload)) + payload


def _decode_message(data: bytes) -> dict[str, Any]:
    """Decode length-prefixed JSON from raw bytes."""
    length = struct.unpack(HEADER_FMT, data[:HEADER_SIZE])[0]
    payload = data[HEADER_SIZE : HEADER_SIZE + length]
    return json.loads(payload)


async def _handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    port: int = GUEST_AGENT_PORT,
) -> None:
    """Perform the Firecracker vsock handshake."""
    writer.write(f"CONNECT {port}\n".encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    if not line.startswith(b"OK"):
        raise ConnectionError(f"Vsock handshake failed: {line.decode().strip()}")


async def vsock_request(
    vsock_uds_path: str,
    msg: dict[str, Any],
    timeout: float = 30,
) -> dict[str, Any]:
    """Send a request and return the response on a single connection."""
    reader, writer = await asyncio.open_unix_connection(vsock_uds_path)
    try:
        await _handshake(reader, writer)

        writer.write(_encode_message(msg))
        await writer.drain()

        header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=timeout)
        length = struct.unpack(HEADER_FMT, header)[0]
        payload = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(payload)
    finally:
        writer.close()
        await writer.wait_closed()


async def vsock_send_only(
    vsock_uds_path: str,
    msg: dict[str, Any],
) -> None:
    """Send a fire-and-forget message (e.g., signal)."""
    reader, writer = await asyncio.open_unix_connection(vsock_uds_path)
    try:
        await _handshake(reader, writer)
        writer.write(_encode_message(msg))
        await writer.drain()
    except Exception:
        logger.warning("Failed to send vsock message to %s: %s", vsock_uds_path, _safe_log_msg(msg), exc_info=True)
    finally:
        writer.close()
        await writer.wait_closed()
