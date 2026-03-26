import asyncio
import time

import httpx
import pytest


class TestExecutionAPI:
    async def _ensure_data_dir_writable(self, client: httpx.AsyncClient, sid: str):
        resp = await client.post(
            f"/sessions/{sid}/execute",
            json={
                "code": (
                    "import os\n"
                    "try:\n"
                    "    os.makedirs('/data', exist_ok=True)\n"
                    "    with open('/data/.write_test', 'wb') as f:\n"
                    "        f.write(b'1')\n"
                    "    os.remove('/data/.write_test')\n"
                    "    print('ok')\n"
                    "except Exception as e:\n"
                    "    print(type(e).__name__)"
                )
            },
        )
        if resp.json()["stdout"].strip() != "ok":
            await client.delete(f"/sessions/{sid}")
            pytest.skip("fake KG cannot write to /data")

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

    async def test_upload_file(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            sid = (await c.post("/sessions")).json()["session_id"]
            await self._ensure_data_dir_writable(c, sid)
            content = b"name,value\nalice,100\nbob,200\n"
            resp = await c.post(
                f"/sessions/{sid}/files",
                files={"file": ("test.csv", content)},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["filename"] == "test.csv"
            assert data["path"] == "/data/test.csv"
            assert data["size"] == len(content)
            await c.delete(f"/sessions/{sid}")

    async def test_upload_and_read_file(self, execution_api):
        """Upload a CSV then read it with pandas to verify content integrity."""
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            sid = (await c.post("/sessions")).json()["session_id"]
            await self._ensure_data_dir_writable(c, sid)
            pandas_check = await c.post(
                f"/sessions/{sid}/execute",
                json={"code": "import pandas as pd\nprint('ok')"},
            )
            if pandas_check.json()["success"] is False:
                await c.delete(f"/sessions/{sid}")
                pytest.skip("pandas not available in fake KG")
            csv_data = b"x,y\n1,10\n2,20\n3,30\n"
            await c.post(f"/sessions/{sid}/files", files={"file": ("data.csv", csv_data)})
            resp = await c.post(
                f"/sessions/{sid}/execute",
                json={"code": "import pandas as pd\ndf = pd.read_csv('/data/data.csv')\nprint(df['y'].sum())"},
            )
            assert resp.json()["stdout"].strip() == "60"
            await c.delete(f"/sessions/{sid}")

    async def test_list_files(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            sid = (await c.post("/sessions")).json()["session_id"]
            await self._ensure_data_dir_writable(c, sid)
            await c.post(f"/sessions/{sid}/files", files={"file": ("a.txt", b"hello")})
            await c.post(f"/sessions/{sid}/files", files={"file": ("b.txt", b"world")})
            resp = await c.get(f"/sessions/{sid}/files")
            assert resp.status_code == 200
            files = resp.json()["files"]
            names = {f["filename"] for f in files}
            assert "a.txt" in names
            assert "b.txt" in names
            await c.delete(f"/sessions/{sid}")

    async def test_delete_file(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            sid = (await c.post("/sessions")).json()["session_id"]
            await self._ensure_data_dir_writable(c, sid)
            await c.post(f"/sessions/{sid}/files", files={"file": ("temp.txt", b"data")})
            resp = await c.delete(f"/sessions/{sid}/files/temp.txt")
            assert resp.status_code == 200
            files_resp = await c.get(f"/sessions/{sid}/files")
            names = {f["filename"] for f in files_resp.json()["files"]}
            assert "temp.txt" not in names
            await c.delete(f"/sessions/{sid}")

    async def test_upload_file_session_not_found(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            resp = await c.post(
                "/sessions/nonexistent/files",
                files={"file": ("x.txt", b"data")},
            )
            assert resp.status_code == 404

    async def test_upload_unsafe_filename(self, execution_api):
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            sid = (await c.post("/sessions")).json()["session_id"]
            resp = await c.post(
                f"/sessions/{sid}/files",
                files={"file": ("../etc/passwd", b"data")},
            )
            assert resp.status_code == 422
            await c.delete(f"/sessions/{sid}")

    async def test_upload_binary_file(self, execution_api):
        """Upload a binary file (like a parquet) and verify roundtrip."""
        async with httpx.AsyncClient(base_url=execution_api, timeout=30) as c:
            sid = (await c.post("/sessions")).json()["session_id"]
            await self._ensure_data_dir_writable(c, sid)
            binary_data = bytes(range(256)) * 100
            upload_resp = await c.post(f"/sessions/{sid}/files", files={"file": ("data.bin", binary_data)})
            assert upload_resp.status_code == 200
            resp = await c.post(
                f"/sessions/{sid}/execute",
                json={"code": "with open('/data/data.bin', 'rb') as f: print(len(f.read()))"},
            )
            assert resp.json()["stdout"].strip() == str(len(binary_data))
            await c.delete(f"/sessions/{sid}")
