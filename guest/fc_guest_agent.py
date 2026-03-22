#!/usr/bin/env python3
"""Firecracker guest agent — runs inside microVMs.

Listens on AF_VSOCK port 52, receives length-prefixed JSON commands,
and manages ipykernel processes.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Module-level globals
# ---------------------------------------------------------------------------

VSOCK_PORT = 52
VSOCK_CID_ANY = 0xFFFFFFFF
HEADER_FMT = "!I"

kernel_proc = None
boot_time = time.monotonic()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONN_FILE = "/tmp/kernel_connection.json"


def write_connection_file(path: str, ports: dict, key: str) -> None:
    """Write a Jupyter kernel connection file to *path*."""
    data = {
        "ip": "0.0.0.0",
        "transport": "tcp",
        "signature_scheme": "hmac-sha256",
        "key": key,
        "kernel_name": "python3",
    }
    data.update(ports)
    with open(path, "w") as fh:
        json.dump(data, fh)


def start_kernel(ports: dict, key: str) -> int:
    """Kill any existing kernel, write connection file, spawn ipykernel.

    Returns the PID of the new kernel process.
    """
    global kernel_proc

    if kernel_proc is not None and kernel_proc.poll() is None:
        kernel_proc.terminate()
        kernel_proc = None

    write_connection_file(_CONN_FILE, ports, key)

    kernel_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ipykernel_launcher",
            "-f",
            _CONN_FILE,
        ],
        start_new_session=True,
    )

    # Brief wait to detect immediate crashes.
    time.sleep(0.5)
    if kernel_proc.poll() is not None:
        raise RuntimeError(f"ipykernel exited immediately with code {kernel_proc.poll()}")

    return kernel_proc.pid


# ---------------------------------------------------------------------------
# Message protocol
# ---------------------------------------------------------------------------

def _encode_response(obj: dict) -> bytes:
    body = json.dumps(obj).encode()
    return struct.pack(HEADER_FMT, len(body)) + body


def _decode_message(data: bytes) -> dict:
    header_size = struct.calcsize(HEADER_FMT)
    length = struct.unpack(HEADER_FMT, data[:header_size])[0]
    return json.loads(data[header_size: header_size + length])


def handle_message(data: bytes) -> bytes:
    """Dispatch an incoming length-prefixed JSON message and return a response."""
    global kernel_proc

    try:
        msg = _decode_message(data)
    except Exception as exc:
        return _encode_response({"status": "error", "message": f"bad message: {exc}"})

    action = msg.get("action", "")

    if action in ("start_kernel", "restart_kernel"):
        ports = msg.get("ports", {})
        key = msg.get("key", "")
        try:
            pid = start_kernel(ports, key)
            return _encode_response({"status": "ready", "pid": pid})
        except Exception as exc:
            return _encode_response({"status": "error", "message": str(exc)})

    elif action == "ping":
        uptime = time.monotonic() - boot_time
        alive = kernel_proc is not None and kernel_proc.poll() is None
        return _encode_response({"status": "alive", "uptime": uptime, "kernel_alive": alive})

    elif action == "signal":
        if kernel_proc is None or kernel_proc.poll() is not None:
            return _encode_response({"status": "error", "message": "no kernel running"})
        signum = msg.get("signum", 15)
        try:
            pgid = os.getpgid(kernel_proc.pid)
            os.killpg(pgid, signum)
            return _encode_response({"status": "ok"})
        except Exception as exc:
            return _encode_response({"status": "error", "message": str(exc)})

    else:
        return _encode_response({"status": "error", "message": f"unknown action: {action!r}"})


# ---------------------------------------------------------------------------
# Main — AF_VSOCK listener loop (not exercised by unit tests)
# ---------------------------------------------------------------------------

def main() -> None:  # pragma: no cover
    """Listen on AF_VSOCK and handle incoming messages."""
    srv = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    srv.bind((VSOCK_CID_ANY, VSOCK_PORT))
    srv.listen(5)
    print(f"[guest-agent] listening on vsock port {VSOCK_PORT}", flush=True)

    header_size = struct.calcsize(HEADER_FMT)

    while True:
        conn, addr = srv.accept()
        with conn:
            try:
                header = conn.recv(header_size)
                if len(header) < header_size:
                    continue
                (length,) = struct.unpack(HEADER_FMT, header)
                body = conn.recv(length)
                raw = header + body
                response = handle_message(raw)
                conn.sendall(response)
            except Exception as exc:
                print(f"[guest-agent] error: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":  # pragma: no cover
    main()
