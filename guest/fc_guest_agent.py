#!/usr/bin/env python3
"""Firecracker guest agent — runs inside microVMs.

Listens on AF_VSOCK port 52, receives length-prefixed JSON commands,
and manages ipykernel processes.
"""

import json
import glob
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
AF_VSOCK = getattr(socket, "AF_VSOCK", 40)
HEADER_FMT = "!I"
MAX_MESSAGE_SIZE = 1 * 1024 * 1024  # 1 MiB

kernel_proc = None
panel_proc = None
boot_time = time.monotonic()
_APPS_DIR = "/apps"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONN_FILE = "/tmp/kernel_connection.json"
_KERNEL_LOG = "/tmp/ipykernel.log"


def read_log_tail(path: str, limit: int = 40) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    return "".join(lines[-limit:]).strip()


def with_log_context(message: str) -> str:
    log_tail = read_log_tail(_KERNEL_LOG)
    if not log_tail:
        return message
    return f"{message}\n--- ipykernel log tail ---\n{log_tail}"


def write_connection_file(path: str, ports: dict, key: str, ip: str) -> None:
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


def wait_for_kernel_ports(ip: str, ports: dict, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    pending = list(ports.values())

    while time.monotonic() < deadline:
        remaining = []
        for port in pending:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            try:
                sock.connect((ip, port))
            except OSError:
                remaining.append(port)
            finally:
                sock.close()
        if not remaining:
            return
        pending = remaining
        time.sleep(0.2)

    raise RuntimeError(f"kernel ports did not open within {timeout}s: {pending}")


def start_kernel(ports: dict, key: str, ip: str) -> int:
    """Kill any existing kernel, write connection file, spawn ipykernel.

    Returns the PID of the new kernel process.
    """
    global kernel_proc

    if kernel_proc is not None and kernel_proc.poll() is None:
        kernel_proc.terminate()
        try:
            kernel_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kernel_proc.kill()
            kernel_proc.wait()
        kernel_proc = None

    write_connection_file(_CONN_FILE, ports, key, ip)

    python = sys.executable or "/usr/bin/python3"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONHASHSEED"] = "0"
    kernel_log = open(_KERNEL_LOG, "ab")
    kernel_proc = subprocess.Popen(
        [
            python,
            "-u",
            "-m",
            "ipykernel_launcher",
            "-f",
            _CONN_FILE,
        ],
        start_new_session=True,
        env=env,
        stdout=kernel_log,
        stderr=subprocess.STDOUT,
    )
    kernel_log.close()

    # Brief wait to detect immediate crashes.
    time.sleep(0.5)
    if kernel_proc.poll() is not None:
        raise RuntimeError(with_log_context(f"ipykernel exited immediately with code {kernel_proc.poll()}"))

    try:
        wait_for_kernel_ports(ip, ports)
    except Exception as exc:
        raise RuntimeError(with_log_context(str(exc))) from exc

    return kernel_proc.pid


def _kill_proc(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def start_dashboard(
    code: str,
    port: int,
    app_id: str,
    session_id: str,
    allowed_origins: list[str] | None = None,
) -> None:
    global panel_proc
    if not code.strip():
        raise RuntimeError("dashboard code is required")

    os.makedirs(_APPS_DIR, exist_ok=True)
    app_path = os.path.join(_APPS_DIR, f"dash_{app_id}.py")
    with open(app_path, "w") as fh:
        fh.write(code)

    if panel_proc is not None and panel_proc.poll() is None:
        _kill_proc(panel_proc)
    panel_proc = None

    python = sys.executable or "/usr/bin/python3"
    origins = allowed_origins or ["localhost:8080", "127.0.0.1:8080"]
    origin_args = []
    for origin in origins:
        origin_args.extend(["--allow-websocket-origin", origin])
    panel_proc = subprocess.Popen(
        [
            python,
            "-m",
            "panel",
            "serve",
            app_path,
            "--port",
            str(port),
            "--address",
            "0.0.0.0",
            *origin_args,
            "--prefix",
            f"/dash/{session_id}",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    time.sleep(0.2)
    if panel_proc.poll() is not None:
        raise RuntimeError(f"panel exited immediately with code {panel_proc.poll()}")

    try:
        wait_for_kernel_ports("127.0.0.1", {"panel": port}, timeout=10.0)
    except Exception as exc:
        _kill_proc(panel_proc)
        panel_proc = None
        raise RuntimeError(f"panel serve did not start: {exc}") from exc


def stop_dashboard() -> None:
    global panel_proc
    if panel_proc is not None:
        _kill_proc(panel_proc)
        panel_proc = None

    if os.path.isdir(_APPS_DIR):
        for app_path in glob.glob(os.path.join(_APPS_DIR, "dash_*.py")):
            try:
                os.remove(app_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Message protocol
# ---------------------------------------------------------------------------

def recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, looping until all bytes arrive.

    Raises ``ConnectionError`` if the connection is closed before *n* bytes
    have been received.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(
                f"connection closed after {len(buf)}/{n} bytes"
            )
        buf.extend(chunk)
    return bytes(buf)


def _encode_response(obj: dict) -> bytes:
    body = json.dumps(obj).encode()
    return struct.pack(HEADER_FMT, len(body)) + body


def _decode_message(data: bytes) -> dict:
    header_size = struct.calcsize(HEADER_FMT)
    length = struct.unpack(HEADER_FMT, data[:header_size])[0]
    if length > MAX_MESSAGE_SIZE:
        raise ValueError(f"message too large: {length} bytes (max {MAX_MESSAGE_SIZE})")
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
        ip = msg.get("ip", "127.0.0.1")
        try:
            pid = start_kernel(ports, key, ip)
            return _encode_response({"status": "ready", "pid": pid})
        except Exception as exc:
            return _encode_response({"status": "error", "message": str(exc)})

    elif action == "launch_dashboard":
        code = msg.get("code", "")
        port = msg.get("port", 5006)
        app_id = msg.get("app_id", "")
        session_id = msg.get("session_id", "")
        allowed_origins = msg.get("allowed_origins")
        try:
            start_dashboard(code, port, app_id, session_id, allowed_origins=allowed_origins)
            return _encode_response({"status": "ok", "app_id": app_id, "port": port})
        except Exception as exc:
            return _encode_response({"status": "error", "message": str(exc)})

    elif action == "stop_dashboard":
        try:
            stop_dashboard()
            return _encode_response({"status": "ok"})
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
    srv = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
    srv.bind((VSOCK_CID_ANY, VSOCK_PORT))
    srv.listen(5)
    print(f"[guest-agent] listening on vsock port {VSOCK_PORT}", flush=True)

    header_size = struct.calcsize(HEADER_FMT)

    while True:
        conn, addr = srv.accept()
        with conn:
            try:
                header = recv_exactly(conn, header_size)
                (length,) = struct.unpack(HEADER_FMT, header)
                if length > MAX_MESSAGE_SIZE:
                    err = _encode_response({"status": "error", "message": f"message too large: {length}"})
                    conn.sendall(err)
                    continue
                body = recv_exactly(conn, length)
                raw = header + body
                response = handle_message(raw)
                conn.sendall(response)
            except Exception as exc:
                print(f"[guest-agent] error: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":  # pragma: no cover
    main()
