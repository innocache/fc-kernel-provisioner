# Sandbox Client Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python client library (`sandbox_client`) that wraps the Kernel Gateway REST + WebSocket API, letting a chatbot backend execute Python code in Firecracker microVMs and get structured results (stdout, stderr, errors, images, HTML).

**Architecture:** Four-file package. `output.py` defines data classes and a stateless `OutputParser` that converts Jupyter protocol messages into `ExecutionResult`. `artifact_store.py` defines the `ArtifactStore` protocol and `LocalArtifactStore`. `session.py` provides `SandboxSession` which manages kernel lifecycle via REST and code execution via WebSocket, using the parser and optional store. `__init__.py` re-exports the public API.

**Tech Stack:** Python 3.11+, aiohttp (existing dependency), asyncio, Jupyter kernel protocol v5.3

**Spec:** `docs/superpowers/specs/2026-03-22-sandbox-client-design.md`

**Status:** All tasks complete. 70 unit tests + 12 integration tests passing.

| Chunk | Status |
|-------|--------|
| 1: Data Classes + OutputParser (Tasks 1-3) | DONE |
| 2: ArtifactStore (Tasks 4-5) | DONE |
| 3-4: SandboxSession + Integration (Tasks 6-8) | DONE |
| Post-plan: Edge case coverage tests | DONE — 34 additional tests added |

---

## File Map

| File | Responsibility | Dependencies |
|------|---------------|--------------|
| `sandbox_client/output.py` | `ExecutionResult`, `DisplayOutput`, `ExecutionError` dataclasses + `OutputParser.parse()` | None (stdlib only) |
| `sandbox_client/artifact_store.py` | `ArtifactStore` Protocol + `LocalArtifactStore` | None (stdlib only) |
| `sandbox_client/session.py` | `SandboxSession` — lifecycle + execute | `output.py`, `artifact_store.py`, `aiohttp` |
| `sandbox_client/__init__.py` | Re-exports public API | All three modules |
| `tests/test_output_parser.py` | Unit tests for OutputParser | `sandbox_client.output` |
| `tests/test_artifact_store.py` | Unit tests for LocalArtifactStore | `sandbox_client.artifact_store` |
| `tests/test_session.py` | Unit tests for SandboxSession (mocked WebSocket) | `sandbox_client.session` |
| `tests/test_integration.py` | Integration tests using SandboxSession | `sandbox_client` (modify existing) |
| `pyproject.toml` | Add `sandbox_client` to wheel packages | (modify existing) |

---

## Chunk 1: Data Classes + OutputParser

### Task 1: Data classes and basic OutputParser

**Files:**
- Create: `sandbox_client/__init__.py` (empty initially)
- Create: `sandbox_client/output.py`
- Create: `tests/test_output_parser.py`

- [ ] **Step 1: Write failing tests for data classes and stream parsing**

```python
# tests/test_output_parser.py
"""Tests for the sandbox client output parser."""

import base64
from sandbox_client.output import (
    DisplayOutput,
    ExecutionError,
    ExecutionResult,
    OutputParser,
)


class TestExecutionResultDataclass:
    def test_success_result(self):
        r = ExecutionResult(
            success=True, stdout="hello\n", stderr="", error=None,
            outputs=[], execution_count=1,
        )
        assert r.success is True
        assert r.stdout == "hello\n"
        assert r.execution_count == 1

    def test_error_result(self):
        err = ExecutionError(name="ValueError", value="bad", traceback=["line 1"])
        r = ExecutionResult(
            success=False, stdout="", stderr="", error=err,
            outputs=[], execution_count=1,
        )
        assert r.success is False
        assert r.error.name == "ValueError"


class TestOutputParserStreams:
    def test_stdout(self):
        messages = [
            {"header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "hello\n"}},
        ]
        result = OutputParser.parse(messages)
        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.success is True

    def test_stderr(self):
        messages = [
            {"header": {"msg_type": "stream"}, "content": {"name": "stderr", "text": "warn\n"}},
        ]
        result = OutputParser.parse(messages)
        assert result.stderr == "warn\n"
        assert result.stdout == ""

    def test_multiple_stdout_chunks(self):
        messages = [
            {"header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "a"}},
            {"header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "b"}},
        ]
        result = OutputParser.parse(messages)
        assert result.stdout == "ab"

    def test_empty_messages(self):
        result = OutputParser.parse([])
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.success is True
        assert result.outputs == []
        assert result.execution_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_output_parser.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sandbox_client'`

- [ ] **Step 3: Create empty package init and implement data classes + stream parsing**

`sandbox_client/__init__.py`:
```python
"""Sandbox client — execute code in Firecracker microVMs."""
```

`sandbox_client/output.py`:
```python
"""Output parser — converts Jupyter protocol messages into ExecutionResult."""

import base64
from dataclasses import dataclass, field


@dataclass
class ExecutionError:
    """Error information from a failed execution."""
    name: str
    value: str
    traceback: list[str]


@dataclass
class DisplayOutput:
    """A single rich output (image, HTML, etc.)."""
    mime_type: str
    data: bytes | str
    url: str | None = None


@dataclass
class ExecutionResult:
    """Structured result of a code execution."""
    success: bool
    stdout: str
    stderr: str
    error: ExecutionError | None
    outputs: list[DisplayOutput]
    execution_count: int


# Mime types treated as binary (base64-decoded to bytes).
# All others are kept as str.
_BINARY_MIME_TYPES = frozenset({"image/png"})

# Priority order for selecting display outputs from a mime bundle.
# Lower index = higher priority.  text/plain is only kept when it is
# the sole representation (handled in _extract_display_outputs).
_MIME_PRIORITY = [
    "image/png",
    "image/svg+xml",
    "text/html",
    "application/json",
    "text/plain",
]


def _extract_display_outputs(data: dict) -> list[DisplayOutput]:
    """Extract DisplayOutput entries from a Jupyter mime bundle dict.

    Creates one DisplayOutput per non-text/plain mime type, in priority
    order.  Falls back to text/plain only when it is the sole
    representation.  Binary types are base64-decoded to bytes; text
    types are kept as str.  application/json is serialised to a JSON
    string.
    """
    outputs: list[DisplayOutput] = []
    for mime in _MIME_PRIORITY:
        if mime not in data:
            continue
        if mime == "text/plain" and outputs:
            # Skip text/plain fallback when richer types exist.
            continue
        raw = data[mime]
        if mime in _BINARY_MIME_TYPES:
            decoded = base64.b64decode(raw)
            outputs.append(DisplayOutput(mime_type=mime, data=decoded))
        elif mime == "application/json":
            import json as _json
            outputs.append(DisplayOutput(mime_type=mime, data=_json.dumps(raw)))
        else:
            outputs.append(DisplayOutput(mime_type=mime, data=raw))
    return outputs


class OutputParser:
    """Stateless parser for Jupyter kernel protocol messages."""

    @staticmethod
    def parse(messages: list[dict]) -> ExecutionResult:
        """Parse a list of Jupyter messages into an ExecutionResult."""
        stdout = ""
        stderr = ""
        error: ExecutionError | None = None
        outputs: list[DisplayOutput] = []
        execution_count = 0
        success = True

        for msg in messages:
            msg_type = msg.get("header", {}).get("msg_type", "")
            content = msg.get("content", {})

            if msg_type == "stream":
                name = content.get("name", "stdout")
                text = content.get("text", "")
                if name == "stderr":
                    stderr += text
                else:
                    stdout += text

            elif msg_type == "error":
                error = ExecutionError(
                    name=content.get("ename", "Error"),
                    value=content.get("evalue", ""),
                    traceback=content.get("traceback", []),
                )
                success = False

            elif msg_type in ("execute_result", "display_data"):
                bundle = content.get("data", {})
                outputs.extend(_extract_display_outputs(bundle))

            elif msg_type == "execute_reply":
                execution_count = content.get("execution_count", 0) or 0
                # Fallback error detection.
                if content.get("status") == "error" and error is None:
                    error = ExecutionError(
                        name=content.get("ename", "Error"),
                        value=content.get("evalue", ""),
                        traceback=content.get("traceback", []),
                    )
                    success = False

        return ExecutionResult(
            success=success,
            stdout=stdout,
            stderr=stderr,
            error=error,
            outputs=outputs,
            execution_count=execution_count,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_parser.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add sandbox_client/__init__.py sandbox_client/output.py tests/test_output_parser.py
git commit -m "feat(sandbox): add data classes and OutputParser with stream parsing"
```

---

### Task 2: Error parsing and execute_reply handling

**Files:**
- Modify: `tests/test_output_parser.py`
- (No changes to `sandbox_client/output.py` — already implemented above)

- [ ] **Step 1: Write tests for error and execute_reply parsing**

Add to `tests/test_output_parser.py`:

```python
class TestOutputParserErrors:
    def test_error_message(self):
        messages = [
            {"header": {"msg_type": "error"}, "content": {
                "ename": "ZeroDivisionError",
                "evalue": "division by zero",
                "traceback": ["Traceback ...", "  File ...", "ZeroDivisionError: division by zero"],
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.success is False
        assert result.error is not None
        assert result.error.name == "ZeroDivisionError"
        assert result.error.value == "division by zero"
        assert len(result.error.traceback) == 3

    def test_stdout_before_error(self):
        """stdout captured before error should be preserved."""
        messages = [
            {"header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "before\n"}},
            {"header": {"msg_type": "error"}, "content": {
                "ename": "RuntimeError", "evalue": "oops", "traceback": [],
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.stdout == "before\n"
        assert result.success is False

    def test_execute_reply_extracts_count(self):
        messages = [
            {"header": {"msg_type": "execute_reply"}, "content": {
                "status": "ok", "execution_count": 5,
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.execution_count == 5
        assert result.success is True

    def test_execute_reply_fallback_error(self):
        """execute_reply with status=error and no prior error message."""
        messages = [
            {"header": {"msg_type": "execute_reply"}, "content": {
                "status": "error",
                "ename": "NameError",
                "evalue": "name 'x' is not defined",
                "traceback": ["NameError: name 'x' is not defined"],
                "execution_count": 3,
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.success is False
        assert result.error.name == "NameError"
        assert result.execution_count == 3

    def test_execute_reply_does_not_override_error_message(self):
        """If both error and execute_reply with error, the error message wins."""
        messages = [
            {"header": {"msg_type": "error"}, "content": {
                "ename": "TypeError", "evalue": "from error msg", "traceback": [],
            }},
            {"header": {"msg_type": "execute_reply"}, "content": {
                "status": "error",
                "ename": "TypeError", "evalue": "from reply", "traceback": [],
                "execution_count": 1,
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.error.value == "from error msg"

    def test_unknown_message_types_ignored(self):
        messages = [
            {"header": {"msg_type": "status"}, "content": {"execution_state": "busy"}},
            {"header": {"msg_type": "execute_input"}, "content": {"code": "x = 1"}},
            {"header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "ok\n"}},
        ]
        result = OutputParser.parse(messages)
        assert result.stdout == "ok\n"
        assert result.success is True
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_parser.py -v`
Expected: PASS (12 tests)

- [ ] **Step 3: Commit**

```bash
git add tests/test_output_parser.py
git commit -m "test(sandbox): add error and execute_reply parser tests"
```

---

### Task 3: Mime bundle / display output parsing

**Files:**
- Modify: `tests/test_output_parser.py`

- [ ] **Step 1: Write tests for mime bundle handling**

Add to `tests/test_output_parser.py`:

```python
class TestOutputParserDisplayOutputs:
    def test_display_data_html(self):
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"text/html": "<b>bold</b>", "text/plain": "bold"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "text/html"
        assert result.outputs[0].data == "<b>bold</b>"
        assert isinstance(result.outputs[0].data, str)

    def test_display_data_png(self):
        png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"image/png": png_b64, "text/plain": "<Figure>"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "image/png"
        assert isinstance(result.outputs[0].data, bytes)
        assert result.outputs[0].data == b"\x89PNG\r\n\x1a\n"

    def test_display_data_svg_is_text(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>'
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"image/svg+xml": svg, "text/plain": "<SVG>"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "image/svg+xml"
        assert isinstance(result.outputs[0].data, str)

    def test_execute_result_text_only(self):
        """text/plain kept when it's the sole representation."""
        messages = [
            {"header": {"msg_type": "execute_result"}, "content": {
                "data": {"text/plain": "42"},
                "execution_count": 1,
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "text/plain"
        assert result.outputs[0].data == "42"

    def test_text_plain_skipped_when_richer_exists(self):
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"text/html": "<table></table>", "text/plain": "DataFrame"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "text/html"

    def test_multiple_display_outputs(self):
        png_b64 = base64.b64encode(b"png1").decode()
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"image/png": png_b64},
            }},
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"text/html": "<p>chart 2</p>"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 2
        assert result.outputs[0].mime_type == "image/png"
        assert result.outputs[1].mime_type == "text/html"

    def test_display_output_url_defaults_none(self):
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"text/html": "<b>x</b>"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.outputs[0].url is None

    def test_json_output(self):
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"application/json": {"key": "value"}, "text/plain": "{'key': 'value'}"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "application/json"
        assert isinstance(result.outputs[0].data, str)
        import json
        assert json.loads(result.outputs[0].data) == {"key": "value"}

    def test_bundle_with_multiple_non_text_types(self):
        """A single bundle with image/png + text/html produces two outputs."""
        png_b64 = base64.b64encode(b"img").decode()
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {
                    "image/png": png_b64,
                    "text/html": "<b>chart</b>",
                    "text/plain": "fallback",
                },
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 2
        assert result.outputs[0].mime_type == "image/png"
        assert result.outputs[1].mime_type == "text/html"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_output_parser.py -v`
Expected: PASS (21 tests)

- [ ] **Step 3: Commit**

```bash
git add tests/test_output_parser.py
git commit -m "test(sandbox): add display output and mime bundle parser tests"
```

---

## Chunk 2: ArtifactStore

### Task 4: ArtifactStore protocol and LocalArtifactStore

**Files:**
- Create: `sandbox_client/artifact_store.py`
- Create: `tests/test_artifact_store.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_artifact_store.py
"""Tests for the artifact store."""

import os

import pytest

from sandbox_client.artifact_store import ArtifactStore, LocalArtifactStore


class TestLocalArtifactStore:
    @pytest.fixture
    def store(self, tmp_path):
        return LocalArtifactStore(
            base_dir=str(tmp_path / "artifacts"),
            url_prefix="http://localhost:8080/artifacts",
        )

    async def test_save_creates_file(self, store, tmp_path):
        url = await store.save("session-1", "output_0.png", b"fake-png", "image/png")
        path = tmp_path / "artifacts" / "session-1" / "output_0.png"
        assert path.exists()
        assert path.read_bytes() == b"fake-png"

    async def test_save_returns_url(self, store):
        url = await store.save("session-1", "output_0.png", b"data", "image/png")
        assert url == "http://localhost:8080/artifacts/session-1/output_0.png"

    async def test_save_creates_directories(self, store, tmp_path):
        """Directories are created automatically."""
        await store.save("new-session", "chart.html", b"<html>", "text/html")
        path = tmp_path / "artifacts" / "new-session" / "chart.html"
        assert path.exists()

    async def test_save_multiple_files_same_session(self, store, tmp_path):
        await store.save("s1", "output_0.png", b"img1", "image/png")
        await store.save("s1", "output_1.html", b"<p>hi</p>", "text/html")
        assert (tmp_path / "artifacts" / "s1" / "output_0.png").exists()
        assert (tmp_path / "artifacts" / "s1" / "output_1.html").exists()

    async def test_save_overwrites_existing(self, store, tmp_path):
        await store.save("s1", "output_0.png", b"v1", "image/png")
        await store.save("s1", "output_0.png", b"v2", "image/png")
        path = tmp_path / "artifacts" / "s1" / "output_0.png"
        assert path.read_bytes() == b"v2"

    async def test_url_prefix_no_trailing_slash(self):
        store = LocalArtifactStore(base_dir="/tmp/art", url_prefix="http://host/art/")
        url = await store.save("s", "f.png", b"x", "image/png")
        assert url == "http://host/art/s/f.png"

    def test_implements_protocol(self):
        """LocalArtifactStore satisfies the ArtifactStore protocol."""
        store = LocalArtifactStore(base_dir="/tmp", url_prefix="http://x")
        assert isinstance(store, ArtifactStore)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_artifact_store.py -v`
Expected: FAIL — `ModuleNotFoundError` or `ImportError`

- [ ] **Step 3: Implement ArtifactStore**

```python
# sandbox_client/artifact_store.py
"""Artifact storage — save execution outputs to files and return URLs."""

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class ArtifactStore(Protocol):
    """Protocol for saving execution artifacts and returning URLs."""

    async def save(
        self, session_id: str, filename: str, data: bytes, content_type: str,
    ) -> str:
        """Save artifact data and return its URL.

        The caller (SandboxSession) is responsible for encoding str data to
        bytes (UTF-8) before calling save().  Data is always bytes here.
        """
        ...


class LocalArtifactStore:
    """Saves artifacts to the local filesystem."""

    def __init__(self, base_dir: str, url_prefix: str):
        self._base_dir = base_dir
        self._url_prefix = url_prefix.rstrip("/")

    async def save(
        self, session_id: str, filename: str, data: bytes, content_type: str,
    ) -> str:
        """Write data to {base_dir}/{session_id}/{filename} and return URL."""
        dir_path = os.path.join(self._base_dir, session_id)
        os.makedirs(dir_path, exist_ok=True)

        file_path = os.path.join(dir_path, filename)
        with open(file_path, "wb") as f:
            f.write(data)

        return f"{self._url_prefix}/{session_id}/{filename}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_artifact_store.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add sandbox_client/artifact_store.py tests/test_artifact_store.py
git commit -m "feat(sandbox): add ArtifactStore protocol and LocalArtifactStore"
```

---

## Chunk 3: SandboxSession

### Task 5: SandboxSession lifecycle (start/stop)

**Files:**
- Create: `sandbox_client/session.py`
- Create: `tests/test_session.py`

- [ ] **Step 1: Write failing tests for start/stop**

```python
# tests/test_session.py
"""Tests for SandboxSession (mocked HTTP/WebSocket)."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sandbox_client.session import SandboxSession


def _make_mock_session(kernel_id="test-kernel-id"):
    """Create a mock aiohttp.ClientSession with REST responses."""
    mock_http = AsyncMock()

    # POST /api/kernels → kernel_id
    post_resp = AsyncMock()
    post_resp.status = 200
    post_resp.raise_for_status = MagicMock()
    post_resp.json = AsyncMock(return_value={"id": kernel_id})
    mock_http.post = AsyncMock(return_value=post_resp)

    # DELETE /api/kernels/{id}
    delete_resp = AsyncMock()
    delete_resp.status = 204
    delete_resp.raise_for_status = MagicMock()
    mock_http.delete = AsyncMock(return_value=delete_resp)

    # POST /api/kernels/{id}/interrupt
    interrupt_resp = AsyncMock()
    interrupt_resp.status = 204
    interrupt_resp.raise_for_status = MagicMock()

    # ws_connect
    mock_ws = AsyncMock()
    mock_ws.close = AsyncMock()
    mock_ws.closed = False

    ws_ctx = AsyncMock()
    ws_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
    ws_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_http.ws_connect = MagicMock(return_value=ws_ctx)

    # close
    mock_http.close = AsyncMock()

    return mock_http, mock_ws


class TestSandboxSessionLifecycle:
    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_start_creates_kernel(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        mock_http.post.assert_called_once_with(
            "http://gw:8888/api/kernels",
            json={"name": "python3-firecracker"},
        )

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_start_opens_websocket(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        mock_http.ws_connect.assert_called_once_with(
            "ws://gw:8888/api/kernels/test-kernel-id/channels",
        )

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_stop_deletes_kernel(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()
        await session.stop()

        mock_http.delete.assert_called_once_with(
            "http://gw:8888/api/kernels/test-kernel-id",
        )
        mock_http.close.assert_called_once()

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_context_manager(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        # Need ws.receive to return idle status for any execute calls
        async with SandboxSession("http://gw:8888") as session:
            assert session._kernel_id == "test-kernel-id"

        mock_http.delete.assert_called_once()

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_context_manager_suppresses_stop_errors(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        mock_http.delete = AsyncMock(side_effect=ConnectionError("gone"))
        MockClientSession.return_value = mock_http

        # Should not raise even though stop() fails
        async with SandboxSession("http://gw:8888") as session:
            pass

    async def test_execute_before_start_raises(self):
        session = SandboxSession("http://gw:8888")
        with pytest.raises(RuntimeError, match="Session not started"):
            await session.execute("print('hi')")

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_stop_is_idempotent(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()
        await session.stop()
        await session.stop()  # Should not raise

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_start_503_raises(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        post_resp = AsyncMock()
        post_resp.status = 503
        mock_http.post = AsyncMock(return_value=post_resp)
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        with pytest.raises(RuntimeError, match="No VMs available"):
            await session.start()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sandbox_client.session'`

- [ ] **Step 3: Implement SandboxSession lifecycle**

```python
# sandbox_client/session.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_session.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add sandbox_client/session.py tests/test_session.py
git commit -m "feat(sandbox): add SandboxSession with lifecycle and execute"
```

---

### Task 6: SandboxSession execute and timeout tests

**Files:**
- Modify: `tests/test_session.py`

- [ ] **Step 1: Add execute and timeout tests**

Add to `tests/test_session.py`:

```python
import aiohttp


def _queue_ws_messages(mock_ws, messages, idle_msg_id=None):
    """Configure mock_ws.receive to yield messages then status:idle."""
    raw_msgs = []
    for msg in messages:
        raw = MagicMock()
        raw.type = aiohttp.WSMsgType.TEXT
        raw.data = json.dumps(msg)
        raw_msgs.append(raw)

    # Add idle status message.
    if idle_msg_id:
        idle = MagicMock()
        idle.type = aiohttp.WSMsgType.TEXT
        idle.data = json.dumps({
            "header": {"msg_type": "status"},
            "parent_header": {"msg_id": idle_msg_id},
            "content": {"execution_state": "idle"},
        })
        raw_msgs.append(idle)

    mock_ws.receive = AsyncMock(side_effect=raw_msgs)


class TestSandboxSessionExecute:
    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_execute_returns_stdout(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        # We need to capture the msg_id that execute() generates.
        # Patch uuid to return a known value.
        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="abc123")
            _queue_ws_messages(mock_ws, [
                {
                    "header": {"msg_type": "stream"},
                    "parent_header": {"msg_id": "abc123"},
                    "content": {"name": "stdout", "text": "hello\n"},
                },
            ], idle_msg_id="abc123")

            result = await session.execute("print('hello')")

        assert result.success is True
        assert result.stdout == "hello\n"

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_execute_sends_correct_message(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="msg123")
            _queue_ws_messages(mock_ws, [], idle_msg_id="msg123")
            await session.execute("x = 1")

        sent = mock_ws.send_json.call_args[0][0]
        assert sent["header"]["msg_type"] == "execute_request"
        assert sent["content"]["code"] == "x = 1"
        assert sent["channel"] == "shell"

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_execute_timeout_interrupts_kernel(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        # Make ws.receive hang forever to trigger timeout.
        async def hang_forever():
            await asyncio.sleep(999)

        mock_ws.receive = AsyncMock(side_effect=hang_forever)

        # POST for interrupt
        interrupt_resp = AsyncMock()
        interrupt_resp.status = 204
        # mock_http.post is already set for kernel creation; override for interrupt
        original_post = mock_http.post
        call_count = 0

        async def smart_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "interrupt" in url:
                return interrupt_resp
            return await original_post(url, **kwargs)

        mock_http.post = AsyncMock(side_effect=smart_post)

        session = SandboxSession("http://gw:8888", default_timeout=30)
        await session.start()

        result = await session.execute("import time; time.sleep(999)", timeout=0.1)

        assert result.success is False
        assert result.error is not None
        assert result.error.name == "TimeoutError"

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_execute_websocket_close_raises(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        close_msg = MagicMock()
        close_msg.type = aiohttp.WSMsgType.CLOSE
        mock_ws.receive = AsyncMock(return_value=close_msg)

        session = SandboxSession("http://gw:8888")
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="x")
            with pytest.raises(ConnectionError):
                await session.execute("print('hi')")


class TestSandboxSessionArtifacts:
    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_execute_with_artifact_store(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        mock_store = AsyncMock()
        mock_store.save = AsyncMock(return_value="http://cdn/session-1/output_0.png")

        session = SandboxSession("http://gw:8888", artifact_store=mock_store)
        await session.start()

        import base64
        png_b64 = base64.b64encode(b"fake-png").decode()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="aid1")
            _queue_ws_messages(mock_ws, [
                {
                    "header": {"msg_type": "display_data"},
                    "parent_header": {"msg_id": "aid1"},
                    "content": {"data": {"image/png": png_b64}},
                },
            ], idle_msg_id="aid1")

            result = await session.execute("plt.show()")

        assert len(result.outputs) == 1
        assert result.outputs[0].url == "http://cdn/session-1/output_0.png"
        assert result.outputs[0].data == b"fake-png"
        mock_store.save.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_session.py -v`
Expected: PASS (13 tests)

- [ ] **Step 3: Commit**

```bash
git add tests/test_session.py
git commit -m "test(sandbox): add execute, timeout, and artifact store session tests"
```

---

## Chunk 4: Package Wiring + Integration Tests

### Task 7: Package init and pyproject.toml

**Files:**
- Modify: `sandbox_client/__init__.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Update package init with re-exports**

```python
# sandbox_client/__init__.py
"""Sandbox client — execute code in Firecracker microVMs."""

from .artifact_store import ArtifactStore, LocalArtifactStore
from .output import DisplayOutput, ExecutionError, ExecutionResult
from .session import SandboxSession

__all__ = [
    "ArtifactStore",
    "DisplayOutput",
    "ExecutionError",
    "ExecutionResult",
    "LocalArtifactStore",
    "SandboxSession",
]
```

- [ ] **Step 2: Add sandbox_client to pyproject.toml wheel packages**

In `pyproject.toml`, change:
```toml
packages = ["fc_provisioner", "fc_pool_manager"]
```
to:
```toml
packages = ["fc_provisioner", "fc_pool_manager", "sandbox_client"]
```

- [ ] **Step 3: Verify imports work**

Run: `uv run python -c "from sandbox_client import SandboxSession, ExecutionResult, LocalArtifactStore; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Run all unit tests**

Run: `uv run pytest tests/ -v -m "not integration"`
Expected: All tests pass (existing 211 + new ~28 = ~239 tests)

- [ ] **Step 5: Commit**

```bash
git add sandbox_client/__init__.py pyproject.toml
git commit -m "feat(sandbox): wire up package exports and add to build config"
```

---

### Task 8: Integration tests

**Files:**
- Modify: `tests/test_integration.py`

These tests require a running Kernel Gateway + pool manager (run via `scripts/remote-test.sh`).

- [ ] **Step 1: Add sandbox client integration tests**

Add to the bottom of `tests/test_integration.py`:

```python
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
        import os
        artifact_dir = tmp_path / "artifacts"
        assert artifact_dir.exists()
        files = list(artifact_dir.rglob("*.png"))
        assert len(files) >= 1
        assert files[0].stat().st_size > 100
```

- [ ] **Step 2: Verify unit tests still pass**

Run: `uv run pytest tests/ -v -m "not integration"`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "test(sandbox): add sandbox client integration tests"
```

- [ ] **Step 4: Run full test suite on remote host**

Run: `./scripts/remote-test.sh xuwang@192.168.1.53 --skip-setup`
Expected: All unit tests pass, smoke test passes, integration tests pass (including new sandbox client tests)
