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
    from execution_api.server import create_app, SessionManager
    import uvicorn

    mock_pool = AsyncMock()
    mock_pool.acquire.return_value = {
        "vm_id": "vm-fake-001",
        "id": "vm-fake-001",
        "ip": "localhost",
        "kg_port": _KG_PORT,
    }
    mock_pool.destroy.return_value = None
    mock_pool.health_check.return_value = True
    mock_pool.close.return_value = None

    class _FakePoolSessionManager(SessionManager):
        async def create(self, execution_timeout=None):
            async with self._lock:
                if len(self._sessions) >= self._max_sessions:
                    raise RuntimeError("max sessions reached")

            from execution_api._sandbox.session import SandboxSession
            from execution_api.server import _make_artifact_store, SessionState, SessionEntry
            import uuid
            timeout = execution_timeout or self._default_timeout
            session = SandboxSession(
                gateway_url=f"http://localhost:{_KG_PORT}",
                default_timeout=timeout,
                artifact_store=_make_artifact_store(),
            )
            session_id = uuid.uuid4().hex
            now = time.time()
            entry = SessionEntry(
                session=session,
                session_id=session_id,
                created_at=now,
                last_active=now,
                state=SessionState.CREATING,
                vm_id="vm-fake-001",
                vm_ip="localhost",
            )
            async with self._lock:
                self._sessions[session_id] = entry
            try:
                await session.start()
            except Exception:
                entry.state = SessionState.CLOSING
                try:
                    await session.stop()
                except Exception:
                    pass
                entry.state = SessionState.CLOSED
                async with self._lock:
                    self._sessions.pop(session_id, None)
                raise
            entry.state = SessionState.ACTIVE
            return entry

    mgr = _FakePoolSessionManager(
        pool_client=mock_pool,
        default_timeout=30,
        max_sessions=50,
        session_ttl=600,
    )
    app = create_app(session_manager=mgr)

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
