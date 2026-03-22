"""Tests for the pool manager HTTP server."""

import pytest
from aiohttp import web
from unittest.mock import AsyncMock, MagicMock
from fc_pool_manager.server import create_app


class TestPoolManagerServer:
    @pytest.fixture
    def mock_manager(self):
        mgr = MagicMock()
        mgr.acquire = AsyncMock(return_value={
            "id": "vm-test1234",
            "ip": "172.16.0.2",
            "vsock_path": "/tmp/v.sock",
        })
        mgr.release = AsyncMock()
        mgr.is_alive = AsyncMock(return_value={
            "alive": True, "uptime": 100, "kernel_alive": True,
        })
        mgr.pool_status = MagicMock(return_value={
            "idle": 3, "assigned": 1, "booting": 0, "max": 30,
        })
        return mgr

    @pytest.fixture
    def client(self, aiohttp_client, mock_manager):
        app = create_app(mock_manager)
        return aiohttp_client(app)

    async def test_acquire(self, client):
        c = await client
        resp = await c.post("/api/vms/acquire", json={"vcpu": 1, "mem_mib": 512})
        assert resp.status == 200
        data = await resp.json()
        assert data["id"] == "vm-test1234"

    async def test_acquire_exhaustion(self, client, mock_manager):
        mock_manager.acquire = AsyncMock(side_effect=RuntimeError("pool_exhausted"))
        c = await client
        resp = await c.post("/api/vms/acquire", json={"vcpu": 1, "mem_mib": 512})
        assert resp.status == 503
        data = await resp.json()
        assert data["error"] == "pool_exhausted"

    async def test_acquire_resource_mismatch(self, client, mock_manager):
        mock_manager.acquire = AsyncMock(
            side_effect=ValueError("do not match pool profile")
        )
        c = await client
        resp = await c.post("/api/vms/acquire", json={"vcpu": 4, "mem_mib": 2048})
        assert resp.status == 400

    async def test_release(self, client):
        c = await client
        resp = await c.delete("/api/vms/vm-test1234", json={"destroy": True})
        assert resp.status == 200

    async def test_health(self, client):
        c = await client
        resp = await c.get("/api/vms/vm-test1234/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["alive"] is True

    async def test_pool_status(self, client):
        c = await client
        resp = await c.get("/api/pool/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["idle"] == 3
