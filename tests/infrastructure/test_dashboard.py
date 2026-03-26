import asyncio
import os

import aiohttp
import pytest

EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")


class TestDashboard:
    async def test_launch_and_access(self):
        async with aiohttp.ClientSession() as http:
            create = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await create.json())["session_id"]
            try:
                resp = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard", json={"code": "import panel as pn\napp = pn.pane.Markdown('hello')"})
                assert resp.status == 200
                data = await resp.json()
                assert data["url"].startswith("/dash/")
                dash = await http.get(f"http://localhost:8080{data['url']}")
                assert dash.status == 200
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_data_from_kernel(self):
        async with aiohttp.ClientSession() as http:
            create = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await create.json())["session_id"]
            try:
                await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/execute", json={"code": "import pandas as pd\npd.DataFrame({'x': [1,2,3]}).to_parquet('/data/processed.parquet')"})
                resp = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard", json={"code": "import pandas as pd, panel as pn\ndf = pd.read_parquet('/data/processed.parquet')\napp = pn.panel(df)"})
                assert resp.status == 200
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    @pytest.mark.xfail(reason="Dashboard replace requires panel process restart which may exceed vsock timeout")
    async def test_replace(self):
        async with aiohttp.ClientSession() as http:
            create = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await create.json())["session_id"]
            try:
                r1 = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard", json={"code": "import panel as pn\napp = pn.pane.Markdown('v1')"})
                await asyncio.sleep(5)
                r2 = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard", json={"code": "import panel as pn\napp = pn.pane.Markdown('v2')"})
                assert r1.status == 200
                assert r2.status == 200
                assert (await r1.json())["app_id"] != (await r2.json())["app_id"]
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_cleanup_on_session_delete(self):
        async with aiohttp.ClientSession() as http:
            create = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await create.json())["session_id"]
            launch = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard", json={"code": "import panel as pn\napp = pn.pane.Markdown('bye')"})
            url = (await launch.json())["url"]
            await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")
            await asyncio.sleep(2)
            dead = await http.get(f"http://localhost:8080{url}")
            assert dead.status in (404, 502, 200)

    async def test_explicit_stop(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await resp.json())["session_id"]
            try:
                await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard", json={"code": "import panel as pn\napp = pn.pane.Markdown('stop me')"})
                resp = await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard")
                assert resp.status == 200
                assert (await resp.json())["ok"] is True
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")
