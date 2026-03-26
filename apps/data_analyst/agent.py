import base64
import logging
import mimetypes
from dataclasses import dataclass
from typing import AsyncGenerator

import httpx

from .config import (
    CADDY_BASE_URL, DOWNLOAD_MAX_BYTES, RECENT_KEEP_FULL, SYSTEM_PROMPT,
    TOOLS, TRUNCATED_OUTPUT_CHARS, UPLOAD_MAX_BYTES,
    sanitize_filename,
)
from .llm_provider import LLMProvider

logger = logging.getLogger(__name__)


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolStart:
    tool_name: str
    code: str


@dataclass
class ToolResult:
    tool_name: str
    output: str
    success: bool


@dataclass
class ImageOutput:
    data: bytes
    mime_type: str


@dataclass
class DashboardLink:
    url: str
    full_url: str


@dataclass
class FileDownload:
    filename: str
    data: bytes
    mime_type: str


AgentEvent = TextDelta | ToolStart | ToolResult | ImageOutput | DashboardLink | FileDownload


class DataAnalystAgent:
    def __init__(self, api_url: str, provider: LLMProvider):
        self.api_url = api_url
        self.provider = provider
        self.session_id: str | None = None
        self.messages: list[dict] = []
        self._client: httpx.AsyncClient | None = None

    async def start_session(self) -> str:
        self._client = httpx.AsyncClient(base_url=self.api_url, timeout=120.0)
        resp = await self._client.post("/sessions")
        resp.raise_for_status()
        self.session_id = resp.json()["session_id"]
        await self._execute("import numpy, pandas\nimport matplotlib; matplotlib.use('Agg')")
        assert self.session_id is not None
        return self.session_id

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Session not started")
        return self._client

    def _require_session_id(self) -> str:
        if self.session_id is None:
            raise RuntimeError("Session not started")
        return self.session_id

    async def end_session(self) -> None:
        if self._client and self.session_id:
            try:
                await self._client.delete(f"/sessions/{self.session_id}")
            except Exception:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None
        self.session_id = None

    async def upload_file(self, filename: str, content: bytes) -> str:
        if len(content) > UPLOAD_MAX_BYTES:
            raise ValueError(
                f"File too large ({len(content)} bytes). Max {UPLOAD_MAX_BYTES // (1024 * 1024)}MB."
            )
        safe_name = sanitize_filename(filename)
        client = self._require_client()
        session_id = self._require_session_id()
        resp = await client.post(
            f"/sessions/{session_id}/files",
            files={"file": (safe_name, content)},
        )
        resp.raise_for_status()
        data = resp.json()
        return f"Saved {data['path']} ({data['size']} bytes)"

    async def download_file(self, path: str) -> FileDownload:
        if not path.startswith("/data/"):
            raise ValueError(f"Downloads restricted to /data/. Got: {path}")
        size_result = await self._execute(f"import os; print(os.path.getsize('{path}'))")
        if not size_result.get("success"):
            error = size_result.get("error", {}).get("value", "unknown error")
            raise FileNotFoundError(f"Cannot read {path}: {error}")
        size = int(size_result["stdout"].strip())
        if size > DOWNLOAD_MAX_BYTES:
            raise ValueError(
                f"File too large ({size} bytes). Max {DOWNLOAD_MAX_BYTES // (1024 * 1024)}MB."
            )
        read_result = await self._execute(
            "import base64\n"
            f"with open('{path}', 'rb') as f:\n"
            "    print(base64.b64encode(f.read()).decode())"
        )
        data = base64.b64decode(read_result["stdout"].strip())
        filename = path.rsplit("/", 1)[-1]
        mime, _ = mimetypes.guess_type(filename)
        return FileDownload(filename=filename, data=data, mime_type=mime or "application/octet-stream")

    async def chat(self, user_message: str) -> AsyncGenerator[AgentEvent, None]:
        self.messages.append({"role": "user", "content": user_message})
        self.messages = self._compact_messages(self.messages)

        response = await self.provider.chat(
            messages=self.messages, system=SYSTEM_PROMPT, tools=TOOLS,
        )

        while response.stop_reason == "tool_use":
            self.messages.append({"role": "assistant", "content": response.raw_content})
            tool_results = []

            for tc in response.tool_calls:
                yield ToolStart(tool_name=tc.name, code=str(tc.input.get("code", tc.input)))

                if tc.name == "execute_python_code":
                    result = await self._execute_with_recovery(tc.input["code"])
                    output = self._format_result(result)
                    yield ToolResult(tool_name=tc.name, output=output, success=result.get("success", False))
                    for img in self._extract_images(result):
                        yield img
                    tool_results.append(self.provider.format_tool_result(tc.id, output))

                elif tc.name == "launch_dashboard":
                    dash = await self._launch_dashboard(tc.input["code"])
                    tool_results.append(self.provider.format_tool_result(tc.id, dash["text"]))
                    if dash.get("link"):
                        yield dash["link"]

                elif tc.name == "download_file":
                    try:
                        fd = await self.download_file(tc.input["path"])
                        yield fd
                        tool_results.append(self.provider.format_tool_result(
                            tc.id, f"File {fd.filename} sent to user for download"))
                    except (FileNotFoundError, ValueError) as e:
                        tool_results.append(self.provider.format_tool_result(tc.id, str(e)))
                        yield ToolResult(tool_name=tc.name, output=str(e), success=False)

            self.messages.append({"role": "user", "content": tool_results})
            self.messages = self._compact_messages(self.messages)
            response = await self.provider.chat(
                messages=self.messages, system=SYSTEM_PROMPT, tools=TOOLS,
            )

        self.messages.append({"role": "assistant", "content": response.raw_content})
        if response.text:
            yield TextDelta(text=response.text)

    async def _execute(self, code: str) -> dict:
        client = self._require_client()
        session_id = self._require_session_id()
        resp = await client.post(
            f"/sessions/{session_id}/execute", json={"code": code},
        )
        return resp.json()

    async def _execute_with_recovery(self, code: str) -> dict:
        try:
            return await self._execute(code)
        except (httpx.ConnectError, httpx.ReadError):
            logger.warning("Session lost, recreating...")
            if self._client:
                await self._client.aclose()
            await self.start_session()
            return {
                "success": False, "stdout": "", "stderr": "",
                "error": {"name": "SessionRestarted",
                          "value": "Sandbox restarted. Previous variables lost.",
                          "traceback": []},
                "outputs": [], "execution_count": 0,
            }

    async def _launch_dashboard(self, code: str) -> dict:
        client = self._require_client()
        session_id = self._require_session_id()
        resp = await client.post(
            f"/sessions/{session_id}/dashboard", json={"code": code},
        )
        if resp.status_code == 200:
            data = resp.json()
            url = data.get("url", "")
            full_url = f"{CADDY_BASE_URL}{url}"
            return {"text": f"Dashboard at {full_url}",
                    "link": DashboardLink(url=url, full_url=full_url)}
        return {"text": f"Dashboard failed: {resp.text}"}

    @staticmethod
    def _format_result(data: dict) -> str:
        parts = []
        if data.get("stdout"):
            parts.append(data["stdout"])
        if data.get("stderr"):
            parts.append(f"[stderr]: {data['stderr']}")
        if data.get("error"):
            err = data["error"]
            parts.append(f"[error]: {err['name']}: {err['value']}")
        for i, out in enumerate(data.get("outputs", [])):
            mime = out.get("mime_type", "")
            if out.get("data_b64"):
                parts.append(f"[output {i}]: {mime} (image)")
            elif out.get("data"):
                parts.append(f"[output {i}]: {mime}\n{out['data'][:2000]}")
        return "\n".join(parts) or "(no output)"

    @staticmethod
    def _extract_images(data: dict) -> list[ImageOutput]:
        images = []
        for out in data.get("outputs", []):
            if out.get("data_b64") and "image" in out.get("mime_type", ""):
                images.append(ImageOutput(
                    data=base64.b64decode(out["data_b64"]),
                    mime_type=out["mime_type"],
                ))
        return images

    @staticmethod
    def _compact_messages(messages: list[dict]) -> list[dict]:
        tool_indices = [
            i for i, m in enumerate(messages)
            if m["role"] == "user" and isinstance(m.get("content"), list)
        ]
        if len(tool_indices) <= RECENT_KEEP_FULL:
            return messages
        old_indices = set(tool_indices[:-RECENT_KEEP_FULL])
        compacted = []
        for i, m in enumerate(messages):
            if i in old_indices:
                content = []
                for block in m["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        text = block.get("content", "")
                        if len(text) > TRUNCATED_OUTPUT_CHARS:
                            text = text[:TRUNCATED_OUTPUT_CHARS] + "... [truncated]"
                        content.append({**block, "content": text})
                    else:
                        content.append(block)
                compacted.append({**m, "content": content})
            else:
                compacted.append(m)
        return compacted
