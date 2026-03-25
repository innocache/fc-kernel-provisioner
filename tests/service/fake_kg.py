import asyncio
import io
import json
import traceback
import uuid
from contextlib import redirect_stdout, redirect_stderr

import aiohttp
from aiohttp import web


class FakeKernel:
    def __init__(self):
        self.namespace = {}
        self.execution_count = 0

    def execute(self, code: str, timeout: float = 30) -> dict:
        self.execution_count += 1
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        error = None
        outputs = []

        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exec(code, self.namespace)
        except Exception as e:
            tb_lines = traceback.format_exception(type(e), e, e.__traceback__)
            error = {
                "name": type(e).__name__,
                "value": str(e),
                "traceback": tb_lines,
            }

        return {
            "success": error is None,
            "stdout": stdout_buf.getvalue(),
            "stderr": stderr_buf.getvalue(),
            "error": error,
            "outputs": outputs,
            "execution_count": self.execution_count,
        }


class FakeKernelGateway:
    def __init__(self):
        self._kernels: dict[str, FakeKernel] = {}

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/kernels", self._create_kernel)
        app.router.add_get("/api/kernels", self._list_kernels)
        app.router.add_get("/api/kernels/{kernel_id}", self._get_kernel)
        app.router.add_delete("/api/kernels/{kernel_id}", self._delete_kernel)
        app.router.add_get("/api/kernels/{kernel_id}/channels", self._ws_channels)
        app.router.add_get("/api", self._api_info)
        return app

    async def _api_info(self, request: web.Request) -> web.Response:
        return web.json_response({"version": "fake-kg-1.0"})

    async def _create_kernel(self, request: web.Request) -> web.Response:
        kernel_id = uuid.uuid4().hex
        self._kernels[kernel_id] = FakeKernel()
        return web.json_response({"id": kernel_id, "name": "python3"})

    async def _list_kernels(self, request: web.Request) -> web.Response:
        return web.json_response([
            {"id": kid, "name": "python3"} for kid in self._kernels
        ])

    async def _get_kernel(self, request: web.Request) -> web.Response:
        kernel_id = request.match_info["kernel_id"]
        if kernel_id not in self._kernels:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"id": kernel_id, "name": "python3"})

    async def _delete_kernel(self, request: web.Request) -> web.Response:
        kernel_id = request.match_info["kernel_id"]
        self._kernels.pop(kernel_id, None)
        return web.Response(status=204)

    async def _ws_channels(self, request: web.Request) -> web.WebSocketResponse:
        kernel_id = request.match_info["kernel_id"]
        kernel = self._kernels.get(kernel_id)
        if kernel is None:
            return web.Response(status=404)

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                header = data.get("header", {})
                content = data.get("content", {})
                msg_type = header.get("msg_type", "")
                msg_id = header.get("msg_id", "")
                session = header.get("session", "")

                if msg_type == "execute_request":
                    code = content.get("code", "")
                    result = kernel.execute(code)

                    parent_header = {"msg_id": msg_id, "session": session}

                    if result["stdout"]:
                        await ws.send_json({
                            "header": {"msg_type": "stream", "msg_id": uuid.uuid4().hex},
                            "parent_header": parent_header,
                            "content": {"name": "stdout", "text": result["stdout"]},
                        })

                    if result["stderr"]:
                        await ws.send_json({
                            "header": {"msg_type": "stream", "msg_id": uuid.uuid4().hex},
                            "parent_header": parent_header,
                            "content": {"name": "stderr", "text": result["stderr"]},
                        })

                    if result["error"]:
                        await ws.send_json({
                            "header": {"msg_type": "error", "msg_id": uuid.uuid4().hex},
                            "parent_header": parent_header,
                            "content": {
                                "ename": result["error"]["name"],
                                "evalue": result["error"]["value"],
                                "traceback": result["error"]["traceback"],
                            },
                        })

                    await ws.send_json({
                        "header": {"msg_type": "execute_reply", "msg_id": uuid.uuid4().hex},
                        "parent_header": parent_header,
                        "content": {
                            "status": "ok" if result["success"] else "error",
                            "execution_count": result["execution_count"],
                        },
                    })

                    await ws.send_json({
                        "header": {"msg_type": "status", "msg_id": uuid.uuid4().hex},
                        "parent_header": parent_header,
                        "content": {"execution_state": "idle"},
                    })

            elif msg.type == aiohttp.WSMsgType.ERROR:
                break

        return ws
