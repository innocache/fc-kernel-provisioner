import asyncio
import base64
import logging
import mimetypes
from dataclasses import dataclass, field
from typing import AsyncGenerator

import httpx

from .config import (
    CADDY_BASE_URL, DASHBOARD_MARKER_PREFIX, DOWNLOAD_MAX_BYTES,
    MAX_INLINE_DASHBOARD_BYTES, RECENT_KEEP_FULL, RECOVERY_CACHE_MAX,
    SYSTEM_PROMPT, TOOLS, TRUNCATED_OUTPUT_CHARS, UPLOAD_MAX_BYTES,
    sanitize_filename,
)
from .llm_provider import LLMProvider

logger = logging.getLogger(__name__)

_WARMUP_CODE = "import numpy, pandas\n%matplotlib inline\nimport os; os.makedirs('/data', exist_ok=True)"


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
class DashboardHTML:
    html: bytes
    filename: str


@dataclass
class FileDownload:
    filename: str
    data: bytes
    mime_type: str


AgentEvent = TextDelta | ToolStart | ToolResult | ImageOutput | DashboardLink | DashboardHTML | FileDownload


@dataclass
class _UploadRecord:
    filename: str
    size: int
    data: bytes | None  # None if too large to cache


class DataAnalystAgent:
    def __init__(self, api_url: str, provider: LLMProvider):
        self.api_url = api_url
        self.provider = provider
        self.session_id: str | None = None
        self.messages: list[dict] = []
        self._client: httpx.AsyncClient | None = None
        self._uploaded_files: list[_UploadRecord] = []
        self._upload_cache_used: int = 0
        self._session_context: str | None = None

    # ── Session lifecycle ────────────────────────────────────────────

    async def _ensure_session(self, retries: int = 3) -> None:
        if self.session_id is not None:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.api_url, timeout=120.0)
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                resp = await self._client.post("/sessions")
                resp.raise_for_status()
                sid = resp.json()["session_id"]
                self.session_id = sid
                await self._execute(_WARMUP_CODE)
                return
            except (httpx.HTTPStatusError, httpx.ConnectError) as exc:
                last_exc = exc
                self.session_id = None
                logger.warning("Session creation attempt %d/%d failed: %s", attempt + 1, retries, exc)
                await asyncio.sleep(2 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    async def start_session(self) -> str:
        await self._ensure_session()
        assert self.session_id is not None
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

    # ── File operations ──────────────────────────────────────────────

    async def upload_file(self, filename: str, content: bytes) -> str:
        if len(content) > UPLOAD_MAX_BYTES:
            raise ValueError(
                f"File too large ({len(content)} bytes). Max {UPLOAD_MAX_BYTES // (1024 * 1024)}MB."
            )
        await self._ensure_session()
        safe_name = sanitize_filename(filename)
        client = self._require_client()
        session_id = self._require_session_id()
        resp = await client.post(
            f"/sessions/{session_id}/files",
            files={"file": (safe_name, content)},
        )
        resp.raise_for_status()
        data = resp.json()
        self._cache_upload(safe_name, content)
        return f"Saved {data['path']} ({data['size']} bytes)"

    def _cache_upload(self, filename: str, content: bytes) -> None:
        cached_data: bytes | None = None
        if self._upload_cache_used + len(content) <= RECOVERY_CACHE_MAX:
            cached_data = content
            self._upload_cache_used += len(content)
        self._uploaded_files.append(_UploadRecord(
            filename=filename, size=len(content), data=cached_data,
        ))

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

    # ── Chat loop ────────────────────────────────────────────────────

    _MAX_DASHBOARD_ATTEMPTS = 3

    async def chat(self, user_message: str) -> AsyncGenerator[AgentEvent, None]:
        await self._ensure_session()
        self.messages.append({"role": "user", "content": user_message})
        self.messages = self._compact_messages(self.messages)

        system = self._build_system_prompt()
        response = await self.provider.chat(
            messages=self.messages, system=system, tools=TOOLS,
        )

        dashboard_attempts = 0
        while response.stop_reason == "tool_use":
            self.messages.append({"role": "assistant", "content": response.raw_content})
            tool_results = []

            for tc in response.tool_calls:
                yield ToolStart(tool_name=tc.name, code=str(tc.input.get("code", tc.input)))

                if tc.name == "execute_python_code":
                    result = await self._execute_with_recovery(tc.input["code"])
                    dashboard_path = self._extract_dashboard_path(result)
                    output = self._format_result(result, strip_dashboard_marker=True)
                    yield ToolResult(tool_name=tc.name, output=output, success=result.get("success", False))
                    for img in self._extract_images(result):
                        yield img
                    if dashboard_path:
                        dashboard_event = await self._download_dashboard_html(dashboard_path)
                        if dashboard_event:
                            yield dashboard_event
                            output += "\n[dashboard rendered inline]"
                    tool_results.append(self.provider.format_tool_result(tc.id, output))

                elif tc.name == "launch_dashboard":
                    dashboard_attempts += 1
                    if dashboard_attempts > self._MAX_DASHBOARD_ATTEMPTS:
                        msg = (f"Dashboard failed after {self._MAX_DASHBOARD_ATTEMPTS} attempts. "
                               "Explain the issue to the user instead of retrying.")
                        tool_results.append(self.provider.format_tool_result(tc.id, msg))
                        yield ToolResult(tool_name=tc.name, output=msg, success=False)
                    else:
                        dash = await self._launch_dashboard(tc.input["code"])
                        tool_results.append(self.provider.format_tool_result(tc.id, dash["text"]))
                        if dash.get("link"):
                            yield dash["link"]
                            dashboard_attempts = 0

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
                messages=self.messages, system=system, tools=TOOLS,
            )

        self.messages.append({"role": "assistant", "content": response.raw_content})
        if response.text:
            yield TextDelta(text=response.text)

    # ── Dynamic system prompt ────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        parts = [SYSTEM_PROMPT]
        if self._uploaded_files:
            file_list = "\n".join(f"  - /data/{r.filename}" for r in self._uploaded_files)
            parts.append(f"\nUPLOADED FILES:\n{file_list}")
        if self._session_context:
            parts.append(f"\nSESSION STATE:\n{self._session_context}")
        return "\n".join(parts)

    # ── Execution helpers ────────────────────────────────────────────

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Session not started")
        return self._client

    def _require_session_id(self) -> str:
        if self.session_id is None:
            raise RuntimeError("Session not started")
        return self.session_id

    async def _execute(self, code: str) -> dict:
        client = self._require_client()
        session_id = self._require_session_id()
        resp = await client.post(
            f"/sessions/{session_id}/execute", json={"code": code},
        )
        if resp.status_code in (404, 503):
            raise httpx.ConnectError(f"session unavailable ({resp.status_code})")
        resp.raise_for_status()
        return resp.json()

    async def _execute_with_recovery(self, code: str) -> dict:
        try:
            return await self._execute(code)
        except (httpx.ConnectError, httpx.ReadError):
            logger.warning("Session lost, recreating...")
            await self._recover_session()
            return {
                "success": False, "stdout": "", "stderr": "",
                "error": {"name": "SessionRestarted",
                          "value": "Sandbox restarted. Previous variables lost.",
                          "traceback": []},
                "outputs": [], "execution_count": 0,
            }

    async def _recover_session(self) -> None:
        if self._client:
            await self._client.aclose()
        self._client = None
        self.session_id = None

        await self._ensure_session()

        re_uploaded = []
        too_large = []
        for rec in self._uploaded_files:
            if rec.data is not None:
                try:
                    client = self._require_client()
                    session_id = self._require_session_id()
                    await client.post(
                        f"/sessions/{session_id}/files",
                        files={"file": (rec.filename, rec.data)},
                    )
                    re_uploaded.append(rec.filename)
                except Exception:
                    logger.warning("Failed to re-upload %s during recovery", rec.filename)
            else:
                too_large.append(rec.filename)

        context_parts = []
        if re_uploaded:
            context_parts.append(f"Files re-uploaded: {', '.join(re_uploaded)}")
        if too_large:
            context_parts.append(f"Files too large to cache (user must re-upload): {', '.join(too_large)}")
        context_parts.append("All variables and computation state from previous session are lost.")
        context_parts.append("Reload data from /data/ paths as needed.")
        self._session_context = "\n".join(context_parts)

    async def _launch_dashboard(self, code: str) -> dict:
        client = self._require_client()
        session_id = self._require_session_id()
        try:
            resp = await client.post(
                f"/sessions/{session_id}/dashboard", json={"code": code},
            )
        except (httpx.ConnectError, httpx.ReadError):
            resp = None
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            url = data.get("url", "")
            full_url = f"{CADDY_BASE_URL}{url}"
            return {"text": f"Dashboard at {full_url}",
                    "link": DashboardLink(url=url, full_url=full_url)}
        if resp is not None and resp.status_code == 422:
            detail = resp.json().get("detail", resp.text)
            return {"text": f"Dashboard code error — fix and retry:\n{detail}"}
        if resp is not None and resp.status_code in (404, 503):
            logger.warning("Dashboard failed (%s), recovering session...", resp.status_code)
            await self._recover_session()
            return {"text": "Dashboard failed: sandbox was restarted. Please try again."}
        detail = resp.text if resp is not None else "connection lost"
        return {"text": f"Dashboard failed: {detail}"}

    # ── Result formatting ────────────────────────────────────────────

    @staticmethod
    def _format_result(data: dict, strip_dashboard_marker: bool = False) -> str:
        parts = []
        if data.get("stdout"):
            stdout = data["stdout"]
            if strip_dashboard_marker:
                stdout = "\n".join(
                    line for line in stdout.splitlines()
                    if not line.startswith(DASHBOARD_MARKER_PREFIX)
                )
            parts.append(stdout)
        if data.get("stderr"):
            parts.append(f"[stderr]: {data['stderr']}")
        if data.get("error"):
            err = data["error"]
            parts.append(f"[error]: {err['name']}: {err['value']}")
            if err.get("traceback"):
                parts.append("".join(err["traceback"]))
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
    def _extract_dashboard_path(data: dict) -> str | None:
        stdout = data.get("stdout", "")
        for line in reversed(stdout.splitlines()):
            if line.startswith(DASHBOARD_MARKER_PREFIX) and line.endswith(".html"):
                return line[len("DASHBOARD:"):]
        return None

    async def _download_dashboard_html(self, path: str) -> DashboardHTML | None:
        filename = path.rsplit("/", 1)[-1]
        try:
            client = self._require_client()
            session_id = self._require_session_id()
            resp = await client.get(f"/sessions/{session_id}/files/{filename}")
            if resp.status_code != 200:
                logger.warning("Dashboard HTML download failed: %s", resp.status_code)
                return None
            html_bytes = resp.content
            if len(html_bytes) > MAX_INLINE_DASHBOARD_BYTES:
                logger.warning("Dashboard HTML too large for inline rendering (%d bytes)", len(html_bytes))
                return None
            return DashboardHTML(html=html_bytes, filename=filename)
        except Exception as exc:
            logger.warning("Dashboard HTML download error: %s", exc)
            return None

    # ── Context compaction ───────────────────────────────────────────

    @staticmethod
    def _compact_messages(messages: list[dict]) -> list[dict]:
        tool_indices = [
            i for i, m in enumerate(messages)
            if m["role"] == "user" and isinstance(m.get("content"), list)
        ]
        if len(tool_indices) <= RECENT_KEEP_FULL:
            return messages

        recent_cutoff = tool_indices[-RECENT_KEEP_FULL]
        very_old_cutoff = tool_indices[0] if len(tool_indices) > RECENT_KEEP_FULL * 2 else -1

        compacted = []
        for i, m in enumerate(messages):
            if i >= recent_cutoff or m["role"] != "user" or not isinstance(m.get("content"), list):
                compacted.append(m)
                continue

            content = []
            for block in m["content"]:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    content.append(block)
                    continue

                text = block.get("content", "")

                has_error = "[error]:" in text
                if has_error:
                    pass  # never truncate errors
                elif i <= very_old_cutoff:
                    text = "[truncated tool output]"
                elif len(text) > TRUNCATED_OUTPUT_CHARS:
                    head = text[:200]
                    tail = text[-100:]
                    text = f"{head}\n... [truncated] ...\n{tail}"

                content.append({**block, "content": text})
            compacted.append({**m, "content": content})
        return compacted
