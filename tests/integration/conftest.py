import os
import socket
import subprocess
import time

import pytest

GATEWAY_URL = os.environ.get("KERNEL_GATEWAY_URL", "http://localhost:8888")
EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")
POOL_SOCKET = os.environ.get("POOL_SOCKET", "/var/run/fc-pool.sock")
CADDY_URL = os.environ.get("CADDY_URL", "http://localhost:8080")


def _port_open(host: str, port: int) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def _services_ready() -> bool:
    return _port_open("localhost", 8888) and _port_open("localhost", 8000)


_PROCS: list[subprocess.Popen] = []


def _start_services():
    project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = os.path.join(project_dir, "config", "fc-pool.yaml")
    caddyfile = os.path.join(project_dir, "config", "Caddyfile")

    services = [
        ["sudo", "uv", "run", "python", "-m", "fc_pool_manager.server", "--config", config, "--socket", POOL_SOCKET, "-v"],
        ["sudo", "uv", "run", "jupyter", "kernelgateway", "--KernelGatewayApp.default_kernel_name=python3-firecracker", "--KernelGatewayApp.port=8888", "--KernelGatewayApp.list_kernels=True"],
        ["uv", "run", "python", "-m", "execution_api.server"],
        ["caddy", "run", "--config", caddyfile],
    ]

    for cmd in services:
        proc = subprocess.Popen(cmd, cwd=project_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _PROCS.append(proc)

    deadline = time.time() + 120
    while time.time() < deadline:
        if _port_open("localhost", 8888) and _port_open("localhost", 8000):
            return True
        time.sleep(2)
    return False


def _stop_services():
    for proc in _PROCS:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    _PROCS.clear()


def pytest_configure(config):
    if _services_ready():
        return

    if os.environ.get("FC_START_SERVICES") == "1":
        if not _start_services():
            pytest.exit("Failed to start services within 120s", returncode=1)
    else:
        config._integration_skip = True


def pytest_collection_modifyitems(config, items):
    if getattr(config, "_integration_skip", False):
        skip = pytest.mark.skip(reason="Integration services not running. Set FC_START_SERVICES=1 to auto-start.")
        for item in items:
            if "integration" in str(item.fspath):
                item.add_marker(skip)


def pytest_unconfigure(config):
    if _PROCS:
        _stop_services()
