"""SandboxSession — execute code in Firecracker microVMs via Kernel Gateway."""

import asyncio
import json
import logging
import uuid
from typing import Any

import aiohttp

from .artifact_store import ArtifactStore
from .output import DisplayOutput, ExecutionError, ExecutionResult, OutputParser

logger = logging.getLogger(__name__)

# Mime type → file extension for auto-generated artifact filenames.
_MIME_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "text/html": ".html",
    "application/json": ".json",
    "text/plain": ".txt",
}


class SandboxSession:
    """Execute Python code inside a Firecracker microVM sandbox.

    Usage::

        async with SandboxSession("http://localhost:8888") as session:
            result = await session.execute("print('hello')")
            print(result.stdout)  # "hello\\n"
    """

    def __init__(
        self,
        gateway_url: str = "http://localhost:8888",
        kernel_name: str = "python3-firecracker",
        default_timeout: float = 30.0,
        artifact_store: ArtifactStore | None = None,
    ):
        self._gateway_url = gateway_url.rstrip("/")
        self._kernel_name = kernel_name
        self._default_timeout = default_timeout
        self._artifact_store = artifact_store

        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._kernel_id: str | None = None
        self._ws_ctx: Any = None  # context manager for ws_connect
        self._started = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Create a kernel and open a WebSocket connection."""
        self._http = aiohttp.ClientSession()

        resp = await self._http.post(
            f"{self._gateway_url}/api/kernels",
            json={"name": self._kernel_name},
        )
        if resp.status == 503:
            raise RuntimeError("No VMs available")
        resp.raise_for_status()
        data = await resp.json()
        self._kernel_id = data["id"]

        ws_url = self._gateway_url.replace("http://", "ws://").replace("https://", "wss://")
        self._ws_ctx = self._http.ws_connect(
            f"{ws_url}/api/kernels/{self._kernel_id}/channels",
        )
        self._ws = await self._ws_ctx.__aenter__()
        self._started = True

    async def stop(self) -> None:
        """Delete the kernel and close connections."""
        if not self._started:
            return

        self._started = False

        if self._ws_ctx is not None:
            try:
                await self._ws_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._ws_ctx = None
            self._ws = None

        if self._http is not None and self._kernel_id is not None:
            try:
                await self._http.delete(
                    f"{self._gateway_url}/api/kernels/{self._kernel_id}",
                )
            except Exception:
                logger.debug("Failed to delete kernel %s", self._kernel_id, exc_info=True)

        if self._http is not None:
            await self._http.close()
            self._http = None

        self._kernel_id = None

    async def __aenter__(self) -> "SandboxSession":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        try:
            await self.stop()
        except Exception:
            logger.debug("Error during session cleanup", exc_info=True)
        return False  # Do not suppress exceptions from the body.

    # ── Execution ────────────────────────────────────────────────────────

    async def execute(self, code: str, timeout: float | None = None) -> ExecutionResult:
        """Execute code and return the result.

        Raises RuntimeError if the session has not been started.
        """
        if not self._started or self._ws is None or self._http is None:
            raise RuntimeError("Session not started")

        timeout = timeout if timeout is not None else self._default_timeout
        msg_id = uuid.uuid4().hex

        # Build and send execute_request.
        await self._ws.send_json({
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

        # Collect response messages.
        messages: list[dict] = []
        try:
            messages = await asyncio.wait_for(
                self._collect_messages(msg_id), timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Interrupt the kernel and return a timeout error.
            try:
                await self._http.post(
                    f"{self._gateway_url}/api/kernels/{self._kernel_id}/interrupt",
                )
            except Exception:
                pass
            result = OutputParser.parse(messages)
            return ExecutionResult(
                success=False,
                stdout=result.stdout,
                stderr=result.stderr,
                error=ExecutionError(
                    name="TimeoutError",
                    value=f"Execution timed out after {timeout}s",
                    traceback=[],
                ),
                outputs=result.outputs,
                execution_count=result.execution_count,
            )

        result = OutputParser.parse(messages)

        # Save artifacts if store is configured.
        if self._artifact_store is not None and self._kernel_id is not None:
            result = await self._save_artifacts(result)

        return result

    async def _collect_messages(self, msg_id: str) -> list[dict]:
        """Read WebSocket messages until status: idle for our msg_id."""
        assert self._ws is not None
        messages: list[dict] = []

        while True:
            raw = await self._ws.receive()
            if raw.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                raise ConnectionError("WebSocket closed unexpectedly")

            if raw.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                continue

            msg = json.loads(raw.data)
            parent_id = msg.get("parent_header", {}).get("msg_id")
            if parent_id != msg_id:
                continue

            msg_type = msg.get("header", {}).get("msg_type", "")
            content = msg.get("content", {})

            if msg_type == "status" and content.get("execution_state") == "idle":
                break

            messages.append(msg)

        return messages

    async def _save_artifacts(self, result: ExecutionResult) -> ExecutionResult:
        """Save display outputs to the artifact store and attach URLs."""
        assert self._artifact_store is not None
        assert self._kernel_id is not None

        new_outputs: list[DisplayOutput] = []
        for i, output in enumerate(result.outputs):
            ext = _MIME_EXTENSIONS.get(output.mime_type, ".bin")
            filename = f"output_{i}{ext}"

            data = output.data
            if isinstance(data, str):
                data = data.encode("utf-8")

            url = await self._artifact_store.save(
                self._kernel_id, filename, data, output.mime_type,
            )
            new_outputs.append(DisplayOutput(
                mime_type=output.mime_type,
                data=output.data,
                url=url,
            ))

        return ExecutionResult(
            success=result.success,
            stdout=result.stdout,
            stderr=result.stderr,
            error=result.error,
            outputs=new_outputs,
            execution_count=result.execution_count,
        )
