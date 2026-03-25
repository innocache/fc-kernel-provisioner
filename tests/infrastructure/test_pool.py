import os

import aiohttp

POOL_SOCKET = os.environ.get("POOL_SOCKET", "/var/run/fc-pool.sock")


class TestPoolMetrics:
    async def test_metrics_endpoint_live(self):
        conn = aiohttp.UnixConnector(path=POOL_SOCKET)
        async with aiohttp.ClientSession(connector=conn) as http:
            resp = await http.get("http://localhost/api/metrics")
            assert resp.status == 200
            body = await resp.text()
            assert "fc_pool_vms_total" in body
            assert "fc_pool_max_vms" in body
            assert "# HELP" in body

    async def test_pool_status(self):
        conn = aiohttp.UnixConnector(path=POOL_SOCKET)
        async with aiohttp.ClientSession(connector=conn) as http:
            resp = await http.get("http://localhost/api/pool/status")
            assert resp.status == 200
            data = await resp.json()
            assert "idle" in data
            assert "assigned" in data
            assert "max" in data
            assert data["max"] > 0
