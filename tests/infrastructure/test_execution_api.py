import asyncio
import os
import time

import aiohttp

EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")


class TestExecutionAPI:
    async def test_api_hello_world(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/execute", json={"code": "print('hello')"})
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            assert data["stdout"].strip() == "hello"

    async def test_api_session_lifecycle(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await resp.json())["session_id"]
            await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/execute", json={"code": "x = 42"})
            resp = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/execute", json={"code": "print(x)"})
            assert (await resp.json())["stdout"].strip() == "42"
            resp = await http.get(f"{EXECUTION_API_URL}/sessions")
            assert any(s["session_id"] == sid for s in await resp.json())
            resp = await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")
            assert (await resp.json())["ok"] is True

    async def test_api_error_result(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/execute", json={"code": "1/0"})
            data = await resp.json()
            assert data["success"] is False
            assert data["error"]["name"] == "ZeroDivisionError"

    async def test_api_rich_output(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/execute", json={"code": "import matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.show()"})
            data = await resp.json()
            png = [o for o in data["outputs"] if o["mime_type"] == "image/png"]
            assert len(png) >= 1

    async def test_api_session_not_found(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/sessions/nonexistent/execute", json={"code": "x"})
            assert resp.status == 404

    async def test_one_shot_error(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/execute", json={"code": "1/0"})
            data = await resp.json()
            assert data["success"] is False
            assert data["error"]["name"] == "ZeroDivisionError"

    async def test_one_shot_rich_output(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/execute", json={"code": "import matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.show()"})
            data = await resp.json()
            png = [o for o in data["outputs"] if o["mime_type"] == "image/png"]
            assert len(png) >= 1
            assert len(png[0]["data_b64"]) > 100

    async def test_session_custom_timeout(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/sessions", json={"execution_timeout": 10})
            assert resp.status == 200
            sid = (await resp.json())["session_id"]
            await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_large_output(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/execute", json={"code": "print('x' * 100000)"})
            data = await resp.json()
            assert len(data["stdout"]) >= 100000

    async def test_concurrent_sessions(self):
        async with aiohttp.ClientSession() as http:
            sids = []
            for _ in range(3):
                resp = await http.post(f"{EXECUTION_API_URL}/sessions")
                sids.append((await resp.json())["session_id"])

            async def execute_on(sid, value):
                await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/execute", json={"code": f"x = {value}"})
                resp = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/execute", json={"code": "print(x)"})
                return (await resp.json())["stdout"].strip()

            results = await asyncio.gather(execute_on(sids[0], 10), execute_on(sids[1], 20), execute_on(sids[2], 30))
            assert results == ["10", "20", "30"]
            for sid in sids:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_session_create_latency(self):
        async with aiohttp.ClientSession() as http:
            t0 = time.monotonic()
            resp = await http.post(f"{EXECUTION_API_URL}/sessions")
            elapsed = (time.monotonic() - t0) * 1000
            sid = (await resp.json())["session_id"]
            await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")
            assert elapsed < 500, f"Session create took {elapsed:.0f}ms, expected <500ms"
