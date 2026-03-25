import asyncio
import os
import socket
import threading
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from .fake_kg import FakeKernelGateway


def _free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


_KG_PORT = _free_port()
_API_PORT = _free_port()


def _start_fake_kg():
    kg = FakeKernelGateway()
    app = kg.create_app()

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "localhost", _KG_PORT)
        loop.run_until_complete(site.start())
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_port(_KG_PORT)


def _start_execution_api():
    os.environ["GATEWAY_URL"] = f"http://localhost:{_KG_PORT}"
    os.environ["POOL_SOCKET"] = "/dev/null"

    from execution_api.server import create_app, SessionManager
    import uvicorn

    mgr = SessionManager(
        gateway_url=f"http://localhost:{_KG_PORT}",
        default_timeout=30,
        max_sessions=50,
        session_ttl=600,
    )
    mock_pool = AsyncMock()
    mock_pool.launch_dashboard = AsyncMock(return_value={"status": "ok"})
    mock_pool.stop_dashboard = AsyncMock(return_value={"status": "ok"})
    app = create_app(session_manager=mgr, pool_client=mock_pool)

    config = uvicorn.Config(app, host="localhost", port=_API_PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_port(_API_PORT)


def _wait_port(port, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("localhost", port), timeout=0.5)
            s.close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Port {port} not ready in {timeout}s")


def pytest_configure(config):
    _start_fake_kg()
    _start_execution_api()


@pytest.fixture
def fake_kg():
    return f"http://localhost:{_KG_PORT}"


@pytest.fixture
def execution_api():
    return f"http://localhost:{_API_PORT}"
