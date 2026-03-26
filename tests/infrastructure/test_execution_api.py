import asyncio
import os
import time

import aiohttp
import pytest

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
            assert elapsed < 2000, f"Session create took {elapsed:.0f}ms, expected <2000ms"

    async def test_upload_and_read_csv(self):
        """Upload CSV via file endpoint, read it with pandas, verify content."""
        async with aiohttp.ClientSession() as http:
            sid = (await (await http.post(f"{EXECUTION_API_URL}/sessions")).json())["session_id"]
            try:
                csv_content = b"name,score\nalice,95\nbob,87\ncharlie,92\n"
                form = aiohttp.FormData()
                form.add_field("file", csv_content, filename="scores.csv")
                resp = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/files", data=form)
                assert resp.status == 200
                data = await resp.json()
                assert data["path"] == "/data/scores.csv"
                assert data["size"] == len(csv_content)

                resp = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                    json={"code": "import pandas as pd\ndf = pd.read_csv('/data/scores.csv')\nprint(df['score'].mean())"},
                )
                result = await resp.json()
                assert float(result["stdout"].strip()) == pytest.approx(91.333, rel=0.01)
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_list_files(self):
        async with aiohttp.ClientSession() as http:
            sid = (await (await http.post(f"{EXECUTION_API_URL}/sessions")).json())["session_id"]
            try:
                for name, content in [("a.csv", b"x\n1\n"), ("b.csv", b"y\n2\n")]:
                    form = aiohttp.FormData()
                    form.add_field("file", content, filename=name)
                    await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/files", data=form)

                resp = await http.get(f"{EXECUTION_API_URL}/sessions/{sid}/files")
                assert resp.status == 200
                files = (await resp.json())["files"]
                names = {f["filename"] for f in files}
                assert names == {"a.csv", "b.csv"}
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_delete_file(self):
        async with aiohttp.ClientSession() as http:
            sid = (await (await http.post(f"{EXECUTION_API_URL}/sessions")).json())["session_id"]
            try:
                form = aiohttp.FormData()
                form.add_field("file", b"temp data", filename="temp.txt")
                await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/files", data=form)

                resp = await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}/files/temp.txt")
                assert resp.status == 200

                resp = await http.get(f"{EXECUTION_API_URL}/sessions/{sid}/files")
                names = {f["filename"] for f in (await resp.json())["files"]}
                assert "temp.txt" not in names
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_upload_multiple_then_analyze(self):
        """Upload two files, then run code that joins them."""
        async with aiohttp.ClientSession() as http:
            sid = (await (await http.post(f"{EXECUTION_API_URL}/sessions")).json())["session_id"]
            try:
                for name, content in [
                    ("users.csv", b"id,name\n1,alice\n2,bob\n"),
                    ("orders.csv", b"user_id,amount\n1,100\n2,200\n1,50\n"),
                ]:
                    form = aiohttp.FormData()
                    form.add_field("file", content, filename=name)
                    await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/files", data=form)

                resp = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                    json={"code": (
                        "import pandas as pd\n"
                        "users = pd.read_csv('/data/users.csv')\n"
                        "orders = pd.read_csv('/data/orders.csv')\n"
                        "merged = users.merge(orders, left_on='id', right_on='user_id')\n"
                        "print(merged.groupby('name')['amount'].sum().to_dict())"
                    )},
                )
                result = await resp.json()
                assert result["success"] is True
                assert "alice" in result["stdout"]
                assert "bob" in result["stdout"]
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_upload_binary_file(self):
        """Upload a binary file via the file endpoint, verify content roundtrip."""
        async with aiohttp.ClientSession() as http:
            sid = (await (await http.post(f"{EXECUTION_API_URL}/sessions")).json())["session_id"]
            try:
                binary_data = bytes(range(256)) * 100
                form = aiohttp.FormData()
                form.add_field("file", binary_data, filename="data.bin")
                resp = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/files", data=form)
                assert resp.status == 200

                read_resp = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                    json={"code": "with open('/data/data.bin', 'rb') as f: print(len(f.read()))"},
                )
                assert (await read_resp.json())["stdout"].strip() == str(len(binary_data))
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")
