"""End-to-end integration test: code in -> Firecracker VM -> stdout out.

Prerequisites:
  1. Host has KVM enabled
  2. Rootfs built: guest/build_rootfs.sh
  3. Network setup: config/setup_network.sh
  4. Pool manager running: python -m fc_pool_manager.server --config config/fc-pool.yaml
  5. Kernel Gateway running: jupyter kernelgateway --default_kernel_name=python3-firecracker

Run: uv run pytest tests/test_integration.py -v -m integration
Skip: uv run pytest tests/ -v -m "not integration"
"""

import asyncio
import os

import aiohttp
import pytest

GATEWAY_URL = os.environ.get("KERNEL_GATEWAY_URL", "http://localhost:8888")

pytestmark = pytest.mark.integration


def _services_reachable() -> bool:
    import socket
    for host, port in [("localhost", 8888), ("localhost", 8000)]:
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.close()
        except OSError:
            return False
    return True


_SKIP_REASON = "Integration services not running (KG:8888, API:8000)"

if not _services_reachable():
    pytestmark = [pytest.mark.integration, pytest.mark.skip(reason=_SKIP_REASON)]



from sandbox_client import SandboxSession, LocalArtifactStore


class TestSandboxClient:
    async def test_sandbox_hello_world(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute("print('hello')")
        assert result.success is True
        assert result.stdout.strip() == "hello"
        assert result.error is None

    async def test_sandbox_state_persists(self):
        async with SandboxSession(GATEWAY_URL) as session:
            await session.execute("x = 42")
            result = await session.execute("print(x)")
        assert result.stdout.strip() == "42"

    async def test_sandbox_error_handling(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute("1/0")
        assert result.success is False
        assert result.error is not None
        assert result.error.name == "ZeroDivisionError"
        assert result.error.value == "division by zero"
        assert len(result.error.traceback) > 0

    async def test_sandbox_rich_output(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute(
                "import matplotlib.pyplot as plt\n"
                "plt.plot([1, 2, 3])\n"
                "plt.show()"
            )
        assert result.success is True
        png_outputs = [o for o in result.outputs if o.mime_type == "image/png"]
        assert len(png_outputs) >= 1
        assert isinstance(png_outputs[0].data, bytes)
        assert len(png_outputs[0].data) > 100  # Real PNG, not empty

    async def test_sandbox_html_output(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute(
                "import pandas as pd\n"
                "df = pd.DataFrame({'a': [1, 2], 'b': [3, 4]})\n"
                "display(df)"
            )
        assert result.success is True
        html_outputs = [o for o in result.outputs if o.mime_type == "text/html"]
        assert len(html_outputs) >= 1
        assert "<table" in html_outputs[0].data.lower() or "<div" in html_outputs[0].data.lower()

    async def test_sandbox_timeout(self):
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute(
                "import time; time.sleep(60)", timeout=3,
            )
        assert result.success is False
        assert result.error is not None
        assert result.error.name == "TimeoutError"

    async def test_sandbox_artifact_store(self, tmp_path):
        store = LocalArtifactStore(
            base_dir=str(tmp_path / "artifacts"),
            url_prefix="http://localhost:8080/artifacts",
        )
        async with SandboxSession(GATEWAY_URL, artifact_store=store) as session:
            result = await session.execute(
                "import matplotlib.pyplot as plt\n"
                "plt.plot([1, 2, 3])\n"
                "plt.show()"
            )
        png_outputs = [o for o in result.outputs if o.mime_type == "image/png"]
        assert len(png_outputs) >= 1
        assert png_outputs[0].url is not None
        assert "output_" in png_outputs[0].url
        # Verify file was written
        artifact_dir = tmp_path / "artifacts"
        assert artifact_dir.exists()
        files = list(artifact_dir.rglob("*.png"))
        assert len(files) >= 1
        assert files[0].stat().st_size > 100

    async def test_sandbox_explicit_lifecycle(self):
        """start()/stop() without context manager."""
        session = SandboxSession(GATEWAY_URL)
        await session.start()
        try:
            result = await session.execute("print('explicit')")
            assert result.success is True
            assert result.stdout.strip() == "explicit"
        finally:
            await session.stop()

    async def test_sandbox_error_recovery(self):
        """Session stays usable after an execution error."""
        async with SandboxSession(GATEWAY_URL) as session:
            # First: error
            r1 = await session.execute("1/0")
            assert r1.success is False

            # Second: should still work
            r2 = await session.execute("print('recovered')")
            assert r2.success is True
            assert r2.stdout.strip() == "recovered"

    async def test_sandbox_execution_count_increments(self):
        """execution_count increments with each execution."""
        async with SandboxSession(GATEWAY_URL) as session:
            r1 = await session.execute("1 + 1")
            r2 = await session.execute("2 + 2")
            assert r2.execution_count > r1.execution_count

    async def test_sandbox_stderr_captured(self):
        """stderr from print(..., file=sys.stderr) is captured."""
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute(
                "import sys; print('warning', file=sys.stderr)"
            )
        assert result.success is True
        assert "warning" in result.stderr

    async def test_sandbox_stdout_before_error(self):
        """stdout captured before error is preserved."""
        async with SandboxSession(GATEWAY_URL) as session:
            result = await session.execute(
                "print('before')\n"
                "raise RuntimeError('boom')"
            )
        assert result.success is False
        assert "before" in result.stdout
        assert result.error.name == "RuntimeError"


EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")


class TestExecutionAPI:
    async def test_api_hello_world(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                f"{EXECUTION_API_URL}/execute",
                json={"code": "print('hello')"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            assert data["stdout"].strip() == "hello"
            assert data["error"] is None

    async def test_api_session_lifecycle(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/sessions")
            assert resp.status == 200
            sid = (await resp.json())["session_id"]

            resp = await http.post(
                f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                json={"code": "x = 42"},
            )
            assert resp.status == 200
            assert (await resp.json())["success"] is True

            resp = await http.post(
                f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                json={"code": "print(x)"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["stdout"].strip() == "42"

            resp = await http.get(f"{EXECUTION_API_URL}/sessions")
            assert resp.status == 200
            sessions = await resp.json()
            assert any(s["session_id"] == sid for s in sessions)

            resp = await http.delete(
                f"{EXECUTION_API_URL}/sessions/{sid}",
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

    async def test_api_error_result(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                f"{EXECUTION_API_URL}/execute",
                json={"code": "1/0"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is False
            assert data["error"]["name"] == "ZeroDivisionError"
            assert data["error"]["value"] == "division by zero"
            assert len(data["error"]["traceback"]) > 0

    async def test_api_rich_output(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                f"{EXECUTION_API_URL}/execute",
                json={
                    "code": (
                        "import matplotlib.pyplot as plt\n"
                        "plt.plot([1, 2, 3])\n"
                        "plt.show()"
                    ),
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            png_outputs = [
                o for o in data["outputs"]
                if o["mime_type"] == "image/png"
            ]
            assert len(png_outputs) >= 1
            assert png_outputs[0]["data_b64"] is not None
            assert len(png_outputs[0]["data_b64"]) > 100

    async def test_api_session_not_found(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                f"{EXECUTION_API_URL}/sessions/nonexistent/execute",
                json={"code": "x"},
            )
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "session not found"


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


class TestDashboardIntegration:
    async def test_dashboard_launch_and_access(self):
        async with aiohttp.ClientSession() as http:
            create = await http.post(f"{EXECUTION_API_URL}/sessions")
            assert create.status == 200
            sid = (await create.json())["session_id"]
            try:
                resp = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/dashboard",
                    json={"code": "import panel as pn\npn.panel('hello').servable()"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["url"].startswith(f"/dash/{sid}/")
                dash = await http.get(f"http://localhost:8080{data['url']}")
                assert dash.status == 200
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_dashboard_data_from_kernel(self):
        async with aiohttp.ClientSession() as http:
            create = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await create.json())["session_id"]
            try:
                await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                    json={
                        "code": (
                            "import pandas as pd\n"
                            "pd.DataFrame({'x': [1, 2, 3]}).to_parquet('/data/processed.parquet')"
                        ),
                    },
                )
                resp = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/dashboard",
                    json={
                        "code": (
                            "import pandas as pd, panel as pn\n"
                            "df = pd.read_parquet('/data/processed.parquet')\n"
                            "pn.panel(df).servable()"
                        ),
                    },
                )
                assert resp.status == 200
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_dashboard_replace(self):
        async with aiohttp.ClientSession() as http:
            create = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await create.json())["session_id"]
            try:
                r1 = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/dashboard",
                    json={"code": "import panel as pn\npn.panel('v1').servable()"},
                )
                await asyncio.sleep(2)
                r2 = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/dashboard",
                    json={"code": "import panel as pn\npn.panel('v2').servable()"},
                )
                assert r1.status == 200
                assert r2.status == 200
                assert (await r1.json())["app_id"] != (await r2.json())["app_id"]
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_dashboard_cleanup_on_session_delete(self):
        async with aiohttp.ClientSession() as http:
            create = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await create.json())["session_id"]
            launch = await http.post(
                f"{EXECUTION_API_URL}/sessions/{sid}/dashboard",
                json={"code": "import panel as pn\npn.panel('bye').servable()"},
            )
            url = (await launch.json())["url"]
            await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")
            await asyncio.sleep(2)
            dead = await http.get(f"http://localhost:8080{url}")
            assert dead.status in (404, 502, 200)


class TestExecutionAPIExtended:
    async def test_one_shot_error_returns_200(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                f"{EXECUTION_API_URL}/execute",
                json={"code": "1/0"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is False
            assert data["error"]["name"] == "ZeroDivisionError"

    async def test_one_shot_rich_output(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                f"{EXECUTION_API_URL}/execute",
                json={
                    "code": (
                        "import matplotlib.pyplot as plt\n"
                        "plt.plot([1, 2, 3])\n"
                        "plt.show()"
                    ),
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            png = [o for o in data["outputs"] if o["mime_type"] == "image/png"]
            assert len(png) >= 1
            assert len(png[0]["data_b64"]) > 100

    async def test_session_create_with_custom_timeout(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                f"{EXECUTION_API_URL}/sessions",
                json={"execution_timeout": 10},
            )
            assert resp.status == 200
            sid = (await resp.json())["session_id"]
            await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_explicit_dashboard_stop(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(f"{EXECUTION_API_URL}/sessions")
            sid = (await resp.json())["session_id"]
            try:
                await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/dashboard",
                    json={"code": "import panel as pn\npn.panel('stop me').servable()"},
                )
                resp = await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard")
                assert resp.status == 200
                assert (await resp.json())["ok"] is True
            finally:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")

    async def test_large_output(self):
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                f"{EXECUTION_API_URL}/execute",
                json={"code": "print('x' * 100000)"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            assert len(data["stdout"]) >= 100000

    async def test_concurrent_sessions(self):
        async with aiohttp.ClientSession() as http:
            sids = []
            for _ in range(3):
                resp = await http.post(f"{EXECUTION_API_URL}/sessions")
                assert resp.status == 200
                sids.append((await resp.json())["session_id"])

            async def execute_on(sid, value):
                await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                    json={"code": f"x = {value}"},
                )
                resp = await http.post(
                    f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                    json={"code": "print(x)"},
                )
                return (await resp.json())["stdout"].strip()

            results = await asyncio.gather(
                execute_on(sids[0], 10),
                execute_on(sids[1], 20),
                execute_on(sids[2], 30),
            )
            assert results == ["10", "20", "30"]

            for sid in sids:
                await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")


class TestPoolStatusIntegration:
    async def test_pool_status_endpoint(self):
        conn = aiohttp.UnixConnector(path=POOL_SOCKET)
        async with aiohttp.ClientSession(connector=conn) as http:
            resp = await http.get("http://localhost/api/pool/status")
            assert resp.status == 200
            data = await resp.json()
            assert "idle" in data
            assert "assigned" in data
            assert "max" in data
            assert data["max"] > 0

    async def test_session_create_latency(self):
        import time
        async with aiohttp.ClientSession() as http:
            t0 = time.monotonic()
            resp = await http.post(f"{EXECUTION_API_URL}/sessions")
            elapsed = (time.monotonic() - t0) * 1000
            assert resp.status == 200
            sid = (await resp.json())["session_id"]
            await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")
            assert elapsed < 500, f"Session create took {elapsed:.0f}ms, expected <500ms"
