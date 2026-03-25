import os
import socket
import subprocess
import time

import pytest

GATEWAY_URL = os.environ.get("KERNEL_GATEWAY_URL", "http://localhost:8888")
EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")
POOL_SOCKET = os.environ.get("POOL_SOCKET", "/var/run/fc-pool.sock")

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PROCS: list[subprocess.Popen] = []
_LOG_DIR = "/tmp"


def _port_open(host: str, port: int) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def _socket_open(path: str) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        s.close()
        return True
    except OSError:
        return False


def _all_services_ready() -> bool:
    return (
        _socket_open(POOL_SOCKET)
        and _port_open("localhost", 8888)
        and _port_open("localhost", 8000)
        and _port_open("localhost", 8080)
    )


def _pool_has_idle_vms() -> bool:
    try:
        import json
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(POOL_SOCKET)
        s.sendall(b"GET /api/pool/status HTTP/1.1\r\nHost: localhost\r\n\r\n")
        data = s.recv(4096).decode()
        s.close()
        body = data.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in data else ""
        return json.loads(body).get("idle", 0) > 0
    except Exception:
        return False


def _start_services():
    config = os.path.join(_PROJECT_DIR, "config", "fc-pool.yaml")
    caddyfile = os.path.join(_PROJECT_DIR, "config", "Caddyfile")

    services = [
        (
            ["sudo", "uv", "run", "python", "-m", "fc_pool_manager.server",
             "--config", config, "--socket", POOL_SOCKET, "-v"],
            "fc-pool-manager.log",
        ),
        (
            ["sudo", "uv", "run", "jupyter", "kernelgateway",
             "--KernelGatewayApp.default_kernel_name=python3-firecracker",
             "--KernelGatewayApp.port=8888",
             "--KernelGatewayApp.list_kernels=True"],
            "fc-kernel-gateway.log",
        ),
        (
            ["uv", "run", "python", "-m", "execution_api.server"],
            "fc-execution-api.log",
        ),
        (
            ["caddy", "run", "--config", caddyfile],
            "fc-caddy.log",
        ),
    ]

    for cmd, logfile in services:
        log = open(os.path.join(_LOG_DIR, logfile), "w")
        proc = subprocess.Popen(cmd, cwd=_PROJECT_DIR, stdout=log, stderr=subprocess.STDOUT)
        _PROCS.append(proc)

    deadline = time.time() + 120
    while time.time() < deadline:
        if _all_services_ready():
            break
        time.sleep(2)
    else:
        return False

    pool_deadline = time.time() + 60
    while time.time() < pool_deadline:
        if _pool_has_idle_vms():
            return True
        time.sleep(2)
    return False


def _stop_services():
    for proc in _PROCS:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _PROCS.clear()

    subprocess.run(
        ["sudo", "rm", "-rf", "/var/run/fc-pool.sock"],
        capture_output=True,
    )


def pytest_configure(config):
    if _all_services_ready():
        if not _pool_has_idle_vms():
            deadline = time.time() + 60
            while time.time() < deadline:
                if _pool_has_idle_vms():
                    return
                time.sleep(2)
        return

    if os.environ.get("FC_START_SERVICES") == "1":
        if not _start_services():
            _stop_services()
            pytest.exit(
                "Failed to start services within 120s. "
                f"Check logs in {_LOG_DIR}/fc-*.log",
                returncode=1,
            )
    else:
        config._integration_skip = True


def pytest_collection_modifyitems(config, items):
    if getattr(config, "_integration_skip", False):
        skip = pytest.mark.skip(
            reason="Integration services not running. "
            "Set FC_START_SERVICES=1 to auto-start, or start manually."
        )
        for item in items:
            if "integration" in str(item.fspath):
                item.add_marker(skip)


def pytest_unconfigure(config):
    if _PROCS:
        _stop_services()
