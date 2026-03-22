#!/usr/bin/env python3
"""Firecracker guest agent — runs as PID 1 inside each microVM.

Listens on AF_VSOCK port 52 for commands from the host.
Protocol: length-prefixed JSON (4-byte big-endian length header + JSON payload).

Commands:
  start_kernel  — start ipykernel with given ZMQ ports and HMAC key
  restart_kernel — kill existing kernel, start a new one
  signal        — forward a signal to the kernel process group
  ping          — health check, returns uptime and kernel status
"""

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time

VSOCK_PORT = 52
VSOCK_CID_ANY = 0xFFFFFFFF  # VMADDR_CID_ANY
HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

kernel_proc = None
boot_time = time.monotonic()


def write_connection_file(path: str, ports: dict, key: str):
    """Write a Jupyter kernel connection file."""
    conn = {
        "ip": "0.0.0.0",
        "transport": "tcp",
        "key": key,
        "signature_scheme": "hmac-sha256",
        "kernel_name": "python3",
        **ports,
    }
    with open(path, "w") as f:
        json.dump(conn, f)


def start_kernel(ports: dict, key: str) -> int:
    """Start ipykernel as a child process. Returns PID."""
    global kernel_proc

    # Kill existing kernel if any
    if kernel_proc and kernel_proc.poll() is None:
        kernel_proc.terminate()
        try:
            kernel_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kernel_proc.kill()
            kernel_proc.wait()

    conn_file = "/tmp/kernel.json"
    write_connection_file(conn_file, ports, key)

    kernel_proc = subprocess.Popen(
        [sys.executable, "-m", "ipykernel_launcher", "-f", conn_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )

    # Wait briefly to check it didn't crash immediately
    time.sleep(0.5)
    if kernel_proc.poll() is not None:
        stderr = kernel_proc.stderr.read().decode()
        raise RuntimeError(f"Kernel exited immediately: {stderr[:500]}")

    return kernel_proc.pid


def handle_message(data: bytes) -> bytes:
    """Process a host command and return a response."""
    global kernel_proc
    msg = json.loads(data)
    action = msg.get("action")

    if action in ("start_kernel", "restart_kernel"):
        try:
            pid = start_kernel(msg["ports"], msg.get("key", ""))
            resp = {"status": "ready", "pid": pid}
        except Exception as e:
            resp = {"status": "error", "error": str(e)}

    elif action == "signal":
        signum = msg.get("signum", signal.SIGINT)
        if kernel_proc and kernel_proc.poll() is None:
            os.killpg(os.getpgid(kernel_proc.pid), signum)
            resp = {"status": "ok"}
        else:
            resp = {"status": "error", "error": "no kernel running"}

    elif action == "ping":
        meminfo = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemFree:"):
                        meminfo["mem_free_mib"] = int(line.split()[1]) // 1024
                        break
        except Exception:
            pass
        resp = {
            "status": "alive",
            "uptime": int(time.monotonic() - boot_time),
            "kernel_alive": kernel_proc is not None and kernel_proc.poll() is None,
            **meminfo,
        }

    else:
        resp = {"status": "error", "error": f"unknown action: {action}"}

    return json.dumps(resp).encode()


def main():
    """Main loop: listen on vsock, handle commands."""
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((VSOCK_CID_ANY, VSOCK_PORT))
    sock.listen(5)

    print(f"[guest-agent] Listening on vsock port {VSOCK_PORT}", flush=True)

    while True:
        conn, addr = sock.accept()
        try:
            header = conn.recv(HEADER_SIZE)
            if len(header) < HEADER_SIZE:
                continue
            length = struct.unpack(HEADER_FMT, header)[0]
            data = b""
            while len(data) < length:
                chunk = conn.recv(length - len(data))
                if not chunk:
                    break
                data += chunk

            response = handle_message(data)
            conn.sendall(struct.pack(HEADER_FMT, len(response)))
            conn.sendall(response)
        except Exception as e:
            print(f"[guest-agent] Error: {e}", flush=True)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
