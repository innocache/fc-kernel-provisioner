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
import json
import os
import uuid

import aiohttp
import pytest

GATEWAY_URL = os.environ.get("KERNEL_GATEWAY_URL", "http://localhost:8888")

pytestmark = pytest.mark.integration


@pytest.fixture
async def kernel_id():
    """Start a kernel and yield its ID, then clean up."""
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{GATEWAY_URL}/api/kernels",
            json={"name": "python3-firecracker"},
        )
        resp.raise_for_status()
        data = await resp.json()
        kid = data["id"]

    yield kid

    async with aiohttp.ClientSession() as session:
        await session.delete(f"{GATEWAY_URL}/api/kernels/{kid}")


async def execute_code(kernel_id: str, code: str, timeout: float = 120) -> dict:
    """Execute code on a kernel via WebSocket and collect output."""
    msg_id = uuid.uuid4().hex
    results = {"stdout": "", "stderr": "", "error": None}

    ws_url = GATEWAY_URL.replace("http://", "ws://")

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"{ws_url}/api/kernels/{kernel_id}/channels"
        ) as ws:
            await ws.send_json({
                "header": {
                    "msg_id": msg_id,
                    "username": "",
                    "session": uuid.uuid4().hex,
                    "msg_type": "execute_request",
                    "version": "5.3",
                },
                "parent_header": {},
                "metadata": {},
                "content": {
                    "code": code,
                    "silent": False,
                    "store_history": True,
                    "user_expressions": {},
                    "allow_stdin": False,
                    "stop_on_error": True,
                },
                "buffers": [],
                "channel": "shell",
            })

            while True:
                raw = await asyncio.wait_for(ws.receive(), timeout=timeout)
                if raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                if raw.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    continue

                msg = json.loads(raw.data)
                parent_id = msg.get("parent_header", {}).get("msg_id")
                if parent_id != msg_id:
                    continue

                msg_type = msg["header"]["msg_type"]
                content = msg.get("content", {})

                if msg_type == "stream":
                    name = content.get("name", "stdout")
                    results[name] += content.get("text", "")
                elif msg_type == "error":
                    results["error"] = {
                        "name": content.get("ename", "Error"),
                        "value": content.get("evalue", ""),
                    }
                elif msg_type == "status":
                    if content.get("execution_state") == "idle":
                        break

    return results


class TestFullPipeline:
    async def test_hello_world(self, kernel_id):
        result = await execute_code(kernel_id, "print('hello')")
        assert result["stdout"].strip() == "hello"
        assert result["error"] is None

    async def test_state_persists_across_cells(self, kernel_id):
        await execute_code(kernel_id, "x = 42")
        result = await execute_code(kernel_id, "print(x)")
        assert result["stdout"].strip() == "42"

    async def test_error_handling(self, kernel_id):
        result = await execute_code(kernel_id, "1/0")
        assert result["error"] is not None
        assert result["error"]["name"] == "ZeroDivisionError"

    async def test_imports_work(self, kernel_id):
        result = await execute_code(kernel_id, "import numpy; print(numpy.__version__)")
        assert result["error"] is None
        assert result["stdout"].strip()

    async def test_multiline_output(self, kernel_id):
        result = await execute_code(kernel_id, "for i in range(3): print(i)")
        assert result["stdout"].strip() == "0\n1\n2"


# ---------------------------------------------------------------------------
# Sandbox Client integration tests
# ---------------------------------------------------------------------------

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
