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

    async def test_replace(self):
        """Dispatcher dynamically loads the newest dash_*.py on each request,
        so replacing a dashboard only requires writing a new file — no Panel
        restart needed."""
        async with aiohttp.ClientSession() as http:
            create = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await create.json())["session_id"]
            try:
                r1 = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/dashboard",
                    json={"code": "import panel as pn\napp = pn.pane.Markdown('v1')"},
                )
                assert r1.status == 200
                d1 = await r1.json()

                dash_url = f"http://localhost:8080{d1['url']}"
                page1 = await http.get(dash_url)
                assert page1.status == 200

                r2 = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/dashboard",
                    json={"code": "import panel as pn\napp = pn.pane.Markdown('v2')"},
                )
                assert r2.status == 200
                d2 = await r2.json()
                assert d1["app_id"] != d2["app_id"]
                assert d1["url"] == d2["url"]

                page2 = await http.get(dash_url)
                assert page2.status == 200

                files = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                    json={"code": "import os; print(sorted(os.listdir('/apps/')))"},
                )
                fdata = await files.json()
                assert f"dash_{d1['app_id']}.py" in fdata["stdout"]
                assert f"dash_{d2['app_id']}.py" in fdata["stdout"]
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
