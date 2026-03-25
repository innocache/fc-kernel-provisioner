import asyncio
import time

import httpx
import pytest


class TestExecutionAPI:
    async def test_session_create(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            resp = await c.post("/sessions")
            assert resp.status_code == 200
            data = resp.json()
            assert "session_id" in data
            await c.delete(f"/sessions/{data['session_id']}")

    async def test_session_lifecycle(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            sid = (await c.post("/sessions")).json()["session_id"]
            resp = await c.post(f"/sessions/{sid}/execute", json={"code": "x = 42"})
            assert resp.json()["success"] is True
            resp = await c.post(f"/sessions/{sid}/execute", json={"code": "print(x)"})
            assert resp.json()["stdout"].strip() == "42"
            resp = await c.get("/sessions")
            assert any(s["session_id"] == sid for s in resp.json())
            resp = await c.delete(f"/sessions/{sid}")
            assert resp.json()["ok"] is True

    async def test_execute_error(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            sid = (await c.post("/sessions")).json()["session_id"]
            resp = await c.post(f"/sessions/{sid}/execute", json={"code": "1/0"})
            data = resp.json()
            assert data["success"] is False
            assert data["error"]["name"] == "ZeroDivisionError"
            await c.delete(f"/sessions/{sid}")

    async def test_session_not_found(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            resp = await c.post("/sessions/nonexistent/execute", json={"code": "x"})
            assert resp.status_code == 404

    async def test_one_shot_execute(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            resp = await c.post("/execute", json={"code": "print('hi')"})
            assert resp.status_code == 200
            assert resp.json()["stdout"].strip() == "hi"

    async def test_one_shot_error(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            resp = await c.post("/execute", json={"code": "1/0"})
            data = resp.json()
            assert data["success"] is False
            assert data["error"]["name"] == "ZeroDivisionError"

    async def test_list_sessions_empty(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            resp = await c.get("/sessions")
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)

    async def test_delete_nonexistent(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            resp = await c.delete("/sessions/nonexistent")
            assert resp.status_code == 404

    async def test_large_output(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            sid = (await c.post("/sessions")).json()["session_id"]
            resp = await c.post(f"/sessions/{sid}/execute", json={"code": "print('x' * 10000)"})
            assert len(resp.json()["stdout"]) >= 10000
            await c.delete(f"/sessions/{sid}")

    async def test_concurrent_sessions(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            sids = [(await c.post("/sessions")).json()["session_id"] for _ in range(3)]

            async def run(sid, val):
                await c.post(f"/sessions/{sid}/execute", json={"code": f"x = {val}"})
                resp = await c.post(f"/sessions/{sid}/execute", json={"code": "print(x)"})
                return resp.json()["stdout"].strip()

            results = await asyncio.gather(run(sids[0], 10), run(sids[1], 20), run(sids[2], 30))
            assert results == ["10", "20", "30"]
            for sid in sids:
                await c.delete(f"/sessions/{sid}")
