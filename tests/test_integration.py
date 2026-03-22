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
