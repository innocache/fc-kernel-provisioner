import os
import subprocess

import aiohttp
import pytest

POOL_SOCKET = os.environ.get("POOL_SOCKET", "/var/run/fc-pool.sock")


class TestSmoke:
    async def test_pool_has_idle_vms(self):
        conn = aiohttp.UnixConnector(path=POOL_SOCKET)
        async with aiohttp.ClientSession(connector=conn) as http:
            resp = await http.get("http://localhost/api/pool/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["idle"] > 0, f"Pool has 0 idle VMs: {data}"

    async def test_acquire_ping_health_release(self):
        conn = aiohttp.UnixConnector(path=POOL_SOCKET)
        async with aiohttp.ClientSession(connector=conn) as http:
            resp = await http.post(
                "http://localhost/api/vms/acquire",
                json={"vcpu": 1, "mem_mib": 512},
            )
            assert resp.status == 200, f"Acquire failed: {await resp.text()}"
            vm = await resp.json()
            vm_id = vm["id"]
            vm_ip = vm["ip"]

            try:
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "2", vm_ip],
                    capture_output=True,
                )
                assert result.returncode == 0, f"Ping {vm_ip} failed"

                resp = await http.get(f"http://localhost/api/vms/{vm_id}/health")
                assert resp.status == 200
                health = await resp.json()
                assert health.get("alive") is True, f"Health check failed: {health}"
            finally:
                await http.post(
                    f"http://localhost/api/vms/{vm_id}/release",
                    json={"destroy": True},
                )

    async def test_pool_replenishes_after_release(self):
        import asyncio

        conn = aiohttp.UnixConnector(path=POOL_SOCKET)
        async with aiohttp.ClientSession(connector=conn) as http:
            resp = await http.get("http://localhost/api/pool/status")
            before = (await resp.json())["idle"]

            resp = await http.post(
                "http://localhost/api/vms/acquire",
                json={"vcpu": 1, "mem_mib": 512},
            )
            vm = await resp.json()
            await http.post(
                f"http://localhost/api/vms/{vm['id']}/release",
                json={"destroy": True},
            )

            await asyncio.sleep(5)

            resp = await http.get("http://localhost/api/pool/status")
            after = (await resp.json())["idle"]
            assert after >= before, f"Pool didn't replenish: before={before} after={after}"
