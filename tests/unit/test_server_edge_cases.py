"""Edge case tests for the pool manager HTTP server."""

import pytest
from unittest.mock import AsyncMock, MagicMock

pytest.importorskip("pytest_aiohttp")
from aiohttp import web  # noqa: E402
from fc_pool_manager.server import create_app  # noqa: E402


class TestServerEdgeCases:
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

    async def test_acquire_non_pool_exhausted_runtime_error(self, client, mock_manager):
        """RuntimeError that isn't pool_exhausted should return 500."""
        mock_manager.acquire = AsyncMock(side_effect=RuntimeError("boot failed"))
        c = await client
        resp = await c.post("/api/vms/acquire", json={"vcpu": 1, "mem_mib": 512})
        assert resp.status == 500
        data = await resp.json()
        assert "boot failed" in data["error"]

    async def test_acquire_uses_defaults(self, client, mock_manager):
        """Acquire with no explicit vcpu/mem_mib should use defaults."""
        c = await client
        resp = await c.post("/api/vms/acquire", json={})
        assert resp.status == 200
        mock_manager.acquire.assert_awaited_once_with(vcpu=1, mem_mib=512)

    async def test_acquire_pool_exhausted_includes_retry_header(self, client, mock_manager):
        """503 response should include retry_after_ms."""
        mock_manager.acquire = AsyncMock(side_effect=RuntimeError("pool_exhausted"))
        c = await client
        resp = await c.post("/api/vms/acquire", json={})
        assert resp.status == 503
        data = await resp.json()
        assert data["retry_after_ms"] == 5000

    async def test_release_with_destroy_false(self, client, mock_manager):
        """Release with destroy=False should pass through."""
        c = await client
        resp = await c.delete("/api/vms/vm-test", json={"destroy": False})
        assert resp.status == 200
        mock_manager.release.assert_awaited_once_with("vm-test", destroy=False)

    async def test_release_uses_destroy_true_default(self, client, mock_manager):
        """Release without explicit destroy should default to True."""
        c = await client
        resp = await c.delete("/api/vms/vm-test", json={})
        assert resp.status == 200
        mock_manager.release.assert_awaited_once_with("vm-test", destroy=True)

    async def test_health_unknown_vm(self, client, mock_manager):
        """Health check for unknown VM should still return 200 with alive=False."""
        mock_manager.is_alive = AsyncMock(return_value={"alive": False})
        c = await client
        resp = await c.get("/api/vms/vm-nonexistent/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["alive"] is False

    async def test_pool_status_response_structure(self, client):
        """Status response should contain idle, assigned, booting, max."""
        c = await client
        resp = await c.get("/api/pool/status")
        data = await resp.json()
        assert set(data.keys()) == {"idle", "assigned", "booting", "max"}

    async def test_nonexistent_route_returns_404(self, client):
        c = await client
        resp = await c.get("/api/nonexistent")
        assert resp.status == 404
