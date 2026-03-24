# Chatbot Integration Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A REST Execution API (`execution_api`) that wraps `SandboxSession` with server-managed sessions, TTL cleanup, and a one-shot convenience endpoint. Includes tool schemas for Claude and OpenAI, plus working Python example scripts.

**Architecture:** Four-file package. `models.py` defines Pydantic request/response models. `server.py` contains `SessionManager`, `create_app()` factory, all FastAPI endpoints, and a `_result_to_response()` helper that converts `sandbox_client.ExecutionResult` to JSON-safe API responses (base64-encoding binary outputs, inlining text). `tool_schemas/` holds JSON tool definitions. Separate `examples/` directory has complete runnable scripts using `SandboxSession` directly (no REST API needed for Python chatbots).

**Tech Stack:** Python 3.11+, FastAPI >=0.115, uvicorn >=0.34, Pydantic v2, sandbox_client (existing), httpx (dev, for testing)

**Spec:** `docs/superpowers/specs/2026-03-23-chatbot-integration-design.md`

**Status:** All tasks complete. Full suite: 476 unit + 27 integration tests passing (current repo total).

| Chunk | Status |
|-------|--------|
| 1: Pydantic Models + Dependencies (Task 1) | DONE |
| 2: Server Implementation + Tests (Tasks 2-4) | DONE |
| 3: Tool Schemas + Packaging + Examples (Tasks 5-6) | DONE |
| 4: Integration Tests (Task 7) | DONE |

---

## File Map

| File | Responsibility | Dependencies |
|------|---------------|--------------|
| `execution_api/__init__.py` | Re-exports public API | All modules |
| `execution_api/models.py` | Pydantic request/response models | pydantic |
| `execution_api/server.py` | SessionManager, create_app(), endpoints, `_result_to_response()` | `models.py`, `sandbox_client`, fastapi |
| `execution_api/tool_schemas/claude.json` | Claude `tool_use` schema | None |
| `execution_api/tool_schemas/openai.json` | OpenAI `function_calling` schema | None |
| `examples/oneshot_example.py` | Single-turn Claude + SandboxSession example | `sandbox_client`, `anthropic` (user-installed) |
| `examples/conversation_example.py` | Multi-turn persistent session example | `sandbox_client`, `anthropic` (user-installed) |
| `tests/test_execution_api.py` | Unit tests for models, session manager, endpoints | `execution_api`, `sandbox_client`, httpx |
| `tests/test_integration.py` | API integration tests (modify existing) | `execution_api`, aiohttp |
| `pyproject.toml` | Add fastapi, uvicorn deps + execution_api package (modify) | |

---

## Chunk 1: Pydantic Models + Dependencies

### Task 1: Dependencies, request/response models, and validation tests

**Files:**
- Modify: `pyproject.toml`
- Create: `execution_api/__init__.py`
- Create: `execution_api/models.py`
- Create: `tests/test_execution_api.py`

- [ ] **Step 1: Add dependencies to pyproject.toml**

Add `fastapi` and `uvicorn` to `[project.dependencies]`:
```toml
dependencies = [
    "jupyter_client>=7.0",
    "aiohttp>=3.9",
    "pyyaml>=6.0",
    "fastapi>=0.115",
    "uvicorn>=0.34",
]
```

Add `httpx` to `[dependency-groups] dev`:
```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-aiohttp>=1.0",
    "aioresponses>=0.7",
    "jupyter_kernel_gateway>=3.0",
    "httpx>=0.27",
]
```

Run: `uv sync --group dev`

- [ ] **Step 2: Write failing tests for Pydantic models**

```python
# tests/test_execution_api.py
"""Tests for the Execution API."""

import base64
from datetime import datetime, timezone

from execution_api.models import (
    CreateSessionRequest,
    CreateSessionResponse,
    DeleteResponse,
    ErrorDetail,
    ErrorResponse,
    ExecuteRequest,
    ExecuteResponse,
    OneShotRequest,
    OutputItem,
    SessionInfo,
)


class TestModels:
    def test_create_session_request_defaults(self):
        req = CreateSessionRequest()
        assert req.execution_timeout is None

    def test_create_session_request_with_timeout(self):
        req = CreateSessionRequest(execution_timeout=60)
        assert req.execution_timeout == 60

    def test_create_session_response(self):
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        resp = CreateSessionResponse(session_id="abc123", created_at=now)
        data = resp.model_dump(mode="json")
        assert data["session_id"] == "abc123"
        assert "2026-03-23" in data["created_at"]

    def test_execute_request(self):
        req = ExecuteRequest(code="print('hi')")
        assert req.code == "print('hi')"

    def test_one_shot_request_defaults(self):
        req = OneShotRequest(code="x = 1")
        assert req.code == "x = 1"
        assert req.timeout is None

    def test_one_shot_request_with_timeout(self):
        req = OneShotRequest(code="x", timeout=10)
        assert req.timeout == 10

    def test_error_detail(self):
        err = ErrorDetail(
            name="ValueError", value="bad", traceback=["line 1", "line 2"],
        )
        data = err.model_dump()
        assert data["name"] == "ValueError"
        assert len(data["traceback"]) == 2

    def test_output_item_text(self):
        item = OutputItem(mime_type="text/html", data="<b>x</b>")
        data = item.model_dump()
        assert data["data"] == "<b>x</b>"
        assert data["data_b64"] is None
        assert data["url"] is None

    def test_output_item_binary(self):
        b64 = base64.b64encode(b"\x89PNG").decode()
        item = OutputItem(mime_type="image/png", data_b64=b64)
        data = item.model_dump()
        assert data["data_b64"] == b64
        assert data["data"] is None

    def test_output_item_with_url(self):
        item = OutputItem(
            mime_type="image/png", data_b64="abc",
            url="http://cdn/img.png",
        )
        assert item.url == "http://cdn/img.png"

    def test_execute_response_success(self):
        resp = ExecuteResponse(
            success=True, stdout="hello\n", stderr="", error=None,
            outputs=[], execution_count=1,
        )
        data = resp.model_dump(mode="json")
        assert data["success"] is True
        assert data["stdout"] == "hello\n"
        assert data["error"] is None
        assert data["outputs"] == []

    def test_execute_response_with_error(self):
        resp = ExecuteResponse(
            success=False, stdout="partial\n", stderr="",
            execution_count=3,
            error=ErrorDetail(
                name="ZeroDivisionError", value="division by zero",
                traceback=["Traceback ..."],
            ),
            outputs=[],
        )
        data = resp.model_dump(mode="json")
        assert data["success"] is False
        assert data["error"]["name"] == "ZeroDivisionError"

    def test_execute_response_with_outputs(self):
        resp = ExecuteResponse(
            success=True, stdout="", stderr="", error=None,
            execution_count=1,
            outputs=[
                OutputItem(mime_type="image/png", data_b64="abc"),
                OutputItem(mime_type="text/html", data="<table></table>"),
            ],
        )
        data = resp.model_dump(mode="json")
        assert len(data["outputs"]) == 2
        assert data["outputs"][0]["mime_type"] == "image/png"
        assert data["outputs"][1]["data"] == "<table></table>"

    def test_session_info(self):
        now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        info = SessionInfo(
            session_id="s1", created_at=now, last_active=now,
        )
        data = info.model_dump(mode="json")
        assert data["session_id"] == "s1"

    def test_delete_response(self):
        resp = DeleteResponse()
        assert resp.ok is True

    def test_error_response(self):
        resp = ErrorResponse(error="session not found")
        assert resp.error == "session not found"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_execution_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution_api'`

- [ ] **Step 4: Create package and implement models**

`execution_api/__init__.py`:
```python
"""Execution API — REST server for sandboxed Python code execution."""
```

`execution_api/models.py`:
```python
"""Pydantic request/response models for the Execution API."""

from datetime import datetime

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    execution_timeout: int | None = None


class CreateSessionResponse(BaseModel):
    session_id: str
    created_at: datetime


class ExecuteRequest(BaseModel):
    code: str


class OneShotRequest(BaseModel):
    code: str
    timeout: int | None = None


class ErrorDetail(BaseModel):
    name: str
    value: str
    traceback: list[str]


class OutputItem(BaseModel):
    mime_type: str
    data: str | None = None
    data_b64: str | None = None
    url: str | None = None


class ExecuteResponse(BaseModel):
    success: bool
    stdout: str
    stderr: str
    error: ErrorDetail | None = None
    outputs: list[OutputItem]
    execution_count: int


class SessionInfo(BaseModel):
    session_id: str
    created_at: datetime
    last_active: datetime


class DeleteResponse(BaseModel):
    ok: bool = True


class ErrorResponse(BaseModel):
    error: str
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_execution_api.py -v`
Expected: PASS (17 tests)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml execution_api/__init__.py execution_api/models.py tests/test_execution_api.py
git commit -m "feat(api): add Pydantic models and dependencies for Execution API"
```

---

## Chunk 2: Server Implementation + Tests

### Task 2: Full server implementation and basic session manager tests

**Files:**
- Create: `execution_api/server.py`
- Modify: `tests/test_execution_api.py`

- [ ] **Step 1: Write failing tests for SessionManager basics**

Add to `tests/test_execution_api.py`:

```python
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution_api.server import SessionManager, SessionEntry


class TestSessionManager:
    @patch("execution_api.server.SandboxSession")
    async def test_create_session(self, MockSession):
        mock = AsyncMock()
        MockSession.return_value = mock
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        entry = await mgr.create()
        assert entry.session_id is not None
        assert len(entry.session_id) == 32  # UUID4 hex
        assert entry.created_at > 0
        assert entry.last_active == entry.created_at
        mock.start.assert_awaited_once()

    @patch("execution_api.server.SandboxSession")
    async def test_create_with_custom_timeout(self, MockSession):
        MockSession.return_value = AsyncMock()
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        await mgr.create(execution_timeout=60)
        call_kwargs = MockSession.call_args[1]
        assert call_kwargs["default_timeout"] == 60

    @patch("execution_api.server.SandboxSession")
    async def test_create_uses_default_timeout(self, MockSession):
        MockSession.return_value = AsyncMock()
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        await mgr.create()
        call_kwargs = MockSession.call_args[1]
        assert call_kwargs["default_timeout"] == 30

    @patch("execution_api.server.SandboxSession")
    async def test_get_session(self, MockSession):
        MockSession.return_value = AsyncMock()
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        entry = await mgr.create()
        found = mgr.get(entry.session_id)
        assert found is not None
        assert found.session_id == entry.session_id

    async def test_get_nonexistent_returns_none(self):
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        assert mgr.get("nonexistent") is None

    @patch("execution_api.server.SandboxSession")
    async def test_delete_session(self, MockSession):
        mock = AsyncMock()
        MockSession.return_value = mock
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        entry = await mgr.create()
        deleted = await mgr.delete(entry.session_id)
        assert deleted is True
        assert entry.session_id not in mgr.sessions
        mock.stop.assert_awaited_once()

    async def test_delete_nonexistent_returns_false(self):
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        deleted = await mgr.delete("nonexistent")
        assert deleted is False

    @patch("execution_api.server.SandboxSession")
    async def test_list_sessions(self, MockSession):
        MockSession.return_value = AsyncMock()
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        await mgr.create()
        await mgr.create()
        sessions = mgr.list_sessions()
        assert len(sessions) == 2

    @patch("execution_api.server.SandboxSession")
    async def test_list_empty(self, MockSession):
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        assert mgr.list_sessions() == []

    @patch("execution_api.server.SandboxSession")
    async def test_delete_suppresses_stop_errors(self, MockSession):
        mock = AsyncMock()
        mock.stop = AsyncMock(side_effect=ConnectionError("gone"))
        MockSession.return_value = mock
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        entry = await mgr.create()
        deleted = await mgr.delete(entry.session_id)
        assert deleted is True  # Should not raise

    @patch("execution_api.server.SandboxSession")
    async def test_properties(self, MockSession):
        mgr = SessionManager(
            gateway_url="http://gw:8888", default_timeout=45,
            max_sessions=10, session_ttl=300,
        )
        assert mgr.gateway_url == "http://gw:8888"
        assert mgr.default_timeout == 45
        assert mgr.is_full is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_execution_api.py::TestSessionManager -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution_api.server'`

- [ ] **Step 3: Implement full server.py**

```python
# execution_api/server.py
"""Execution API — FastAPI server wrapping SandboxSession."""

import asyncio
import base64
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from sandbox_client import ExecutionResult, LocalArtifactStore, SandboxSession

from .models import (
    CreateSessionRequest,
    CreateSessionResponse,
    DeleteResponse,
    ErrorDetail,
    ExecuteRequest,
    ExecuteResponse,
    OneShotRequest,
    OutputItem,
    SessionInfo,
)

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8888")
SESSION_TTL = int(os.environ.get("SESSION_TTL", "600"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "20"))
DEFAULT_TIMEOUT = int(os.environ.get("DEFAULT_TIMEOUT", "30"))
ARTIFACT_BASE_DIR = os.environ.get("ARTIFACT_BASE_DIR")
ARTIFACT_URL_PREFIX = os.environ.get("ARTIFACT_URL_PREFIX")
SERVE_ARTIFACTS = os.environ.get("SERVE_ARTIFACTS", "true").lower() == "true"
PORT = int(os.environ.get("PORT", "8000"))


# ── Session Manager ──────────────────────────────────────────────────────


@dataclass
class SessionEntry:
    session: SandboxSession
    session_id: str
    created_at: float
    last_active: float


def _make_artifact_store() -> LocalArtifactStore | None:
    """Create an artifact store from env config, or None if disabled."""
    if not ARTIFACT_BASE_DIR:
        return None
    url_prefix = ARTIFACT_URL_PREFIX or f"http://localhost:{PORT}/artifacts"
    return LocalArtifactStore(base_dir=ARTIFACT_BASE_DIR, url_prefix=url_prefix)


class SessionManager:
    """Manages sandbox session lifecycle, TTL cleanup, and concurrency limits."""

    def __init__(
        self,
        gateway_url: str = GATEWAY_URL,
        default_timeout: int = DEFAULT_TIMEOUT,
        max_sessions: int = MAX_SESSIONS,
        session_ttl: int = SESSION_TTL,
    ):
        self._gateway_url = gateway_url
        self._default_timeout = default_timeout
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl
        self._sessions: dict[str, SessionEntry] = {}
        self._cleanup_task: asyncio.Task | None = None

    @property
    def gateway_url(self) -> str:
        return self._gateway_url

    @property
    def default_timeout(self) -> int:
        return self._default_timeout

    @property
    def is_full(self) -> bool:
        return len(self._sessions) >= self._max_sessions

    @property
    def sessions(self) -> dict[str, SessionEntry]:
        return self._sessions

    async def create(self, execution_timeout: int | None = None) -> SessionEntry:
        """Create a new sandbox session. Raises RuntimeError if no VMs."""
        timeout = execution_timeout or self._default_timeout
        session = SandboxSession(
            gateway_url=self._gateway_url,
            default_timeout=timeout,
            artifact_store=_make_artifact_store(),
        )
        await session.start()

        session_id = uuid.uuid4().hex
        now = time.time()
        entry = SessionEntry(
            session=session,
            session_id=session_id,
            created_at=now,
            last_active=now,
        )
        self._sessions[session_id] = entry
        return entry

    def get(self, session_id: str) -> SessionEntry | None:
        """Return session entry or None if not found."""
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> bool:
        """Delete a session. Returns False if not found."""
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            return False
        try:
            await entry.session.stop()
        except Exception:
            logger.debug("Failed to stop session %s", session_id, exc_info=True)
        return True

    def list_sessions(self) -> list[SessionEntry]:
        """Return all active sessions."""
        return list(self._sessions.values())

    async def cleanup_expired(self) -> None:
        """Remove sessions idle longer than TTL."""
        now = time.time()
        expired = [
            sid for sid, entry in self._sessions.items()
            if now - entry.last_active > self._session_ttl
        ]
        for sid in expired:
            await self.delete(sid)

    def start_cleanup_task(self) -> None:
        """Start the background TTL cleanup loop."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        """Run cleanup every 60 seconds."""
        while True:
            await asyncio.sleep(60)
            try:
                await self.cleanup_expired()
            except Exception:
                logger.debug("Cleanup error", exc_info=True)

    async def shutdown(self) -> None:
        """Cancel cleanup task and destroy all sessions."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        for sid in list(self._sessions):
            entry = self._sessions.pop(sid, None)
            if entry:
                try:
                    await entry.session.stop()
                except Exception:
                    pass


# ── Result Conversion ────────────────────────────────────────────────────


def _result_to_response(result: ExecutionResult) -> ExecuteResponse:
    """Convert sandbox_client ExecutionResult to API response model.

    Binary outputs (bytes) are base64-encoded into `data_b64`.
    Text outputs (str) are inlined into `data`.
    """
    error = None
    if result.error:
        error = ErrorDetail(
            name=result.error.name,
            value=result.error.value,
            traceback=result.error.traceback,
        )

    outputs = []
    for output in result.outputs:
        if isinstance(output.data, bytes):
            outputs.append(OutputItem(
                mime_type=output.mime_type,
                data_b64=base64.b64encode(output.data).decode(),
                url=output.url,
            ))
        else:
            outputs.append(OutputItem(
                mime_type=output.mime_type,
                data=output.data,
                url=output.url,
            ))

    return ExecuteResponse(
        success=result.success,
        stdout=result.stdout,
        stderr=result.stderr,
        error=error,
        outputs=outputs,
        execution_count=result.execution_count,
    )


# ── FastAPI App ──────────────────────────────────────────────────────────


def create_app(session_manager: SessionManager | None = None) -> FastAPI:
    """Create the FastAPI application.

    Pass a SessionManager for testing. Default creates one from env config.
    """
    if session_manager is None:
        session_manager = SessionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        session_manager.start_cleanup_task()
        yield
        await session_manager.shutdown()

    app = FastAPI(title="Execution API", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail},
        )

    # ── Session CRUD ─────────────────────────────────────────────────

    @app.post("/sessions", response_model=CreateSessionResponse)
    async def create_session(
        req: CreateSessionRequest = CreateSessionRequest(),
    ):
        if session_manager.is_full:
            raise HTTPException(status_code=503, detail="max sessions reached")
        try:
            entry = await session_manager.create(req.execution_timeout)
        except RuntimeError:
            raise HTTPException(status_code=503, detail="no VMs available")
        return CreateSessionResponse(
            session_id=entry.session_id,
            created_at=datetime.fromtimestamp(entry.created_at, tz=timezone.utc),
        )

    @app.get("/sessions", response_model=list[SessionInfo])
    async def list_sessions():
        return [
            SessionInfo(
                session_id=e.session_id,
                created_at=datetime.fromtimestamp(e.created_at, tz=timezone.utc),
                last_active=datetime.fromtimestamp(e.last_active, tz=timezone.utc),
            )
            for e in session_manager.list_sessions()
        ]

    @app.delete("/sessions/{session_id}", response_model=DeleteResponse)
    async def delete_session(session_id: str):
        deleted = await session_manager.delete(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="session not found")
        return DeleteResponse()

    # ── Execution ────────────────────────────────────────────────────

    @app.post(
        "/sessions/{session_id}/execute", response_model=ExecuteResponse,
    )
    async def execute_in_session(session_id: str, req: ExecuteRequest):
        entry = session_manager.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        entry.last_active = time.time()
        result = await entry.session.execute(req.code)
        return _result_to_response(result)

    @app.post("/execute", response_model=ExecuteResponse)
    async def one_shot_execute(req: OneShotRequest):
        timeout = req.timeout or session_manager.default_timeout
        session = SandboxSession(
            gateway_url=session_manager.gateway_url,
            default_timeout=timeout,
            artifact_store=_make_artifact_store(),
        )
        try:
            await session.start()
        except RuntimeError:
            raise HTTPException(status_code=503, detail="no VMs available")
        try:
            result = await session.execute(req.code)
            return _result_to_response(result)
        finally:
            try:
                await session.stop()
            except Exception:
                pass

    # ── Artifact serving ─────────────────────────────────────────────

    if ARTIFACT_BASE_DIR and SERVE_ARTIFACTS:
        os.makedirs(ARTIFACT_BASE_DIR, exist_ok=True)
        app.mount(
            "/artifacts",
            StaticFiles(directory=ARTIFACT_BASE_DIR),
            name="artifacts",
        )

    return app


if __name__ == "__main__":
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_execution_api.py -v`
Expected: PASS (29 tests — 17 model + 12 session manager)

- [ ] **Step 5: Commit**

```bash
git add execution_api/server.py tests/test_execution_api.py
git commit -m "feat(api): add SessionManager and FastAPI server with all endpoints"
```

---

### Task 3: Session manager edge case tests

**Files:**
- Modify: `tests/test_execution_api.py`
- (No changes to `execution_api/server.py` — already implemented above)

- [ ] **Step 1: Write tests for TTL cleanup, max sessions, and shutdown**

Add to `tests/test_execution_api.py`:

```python
class TestSessionManagerEdgeCases:
    @patch("execution_api.server.SandboxSession")
    async def test_max_sessions_reached(self, MockSession):
        MockSession.return_value = AsyncMock()
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=2, session_ttl=600,
        )
        await mgr.create()
        await mgr.create()
        assert mgr.is_full is True

    @patch("execution_api.server.SandboxSession")
    async def test_cleanup_expired_sessions(self, MockSession):
        MockSession.return_value = AsyncMock()
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        entry = await mgr.create()
        entry.last_active = time.time() - 9999  # Force expired
        await mgr.cleanup_expired()
        assert len(mgr.sessions) == 0

    @patch("execution_api.server.SandboxSession")
    async def test_cleanup_preserves_active_sessions(self, MockSession):
        MockSession.return_value = AsyncMock()
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        active = await mgr.create()
        expired = await mgr.create()
        expired.last_active = time.time() - 9999
        await mgr.cleanup_expired()
        assert len(mgr.sessions) == 1
        assert active.session_id in mgr.sessions

    @patch("execution_api.server.SandboxSession")
    async def test_shutdown_destroys_all(self, MockSession):
        mock = AsyncMock()
        MockSession.return_value = mock
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        await mgr.create()
        await mgr.create()
        await mgr.shutdown()
        assert len(mgr.sessions) == 0

    @patch("execution_api.server.SandboxSession")
    async def test_shutdown_suppresses_stop_errors(self, MockSession):
        mock = AsyncMock()
        mock.stop = AsyncMock(side_effect=ConnectionError("gone"))
        MockSession.return_value = mock
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        await mgr.create()
        await mgr.shutdown()  # Should not raise
        assert len(mgr.sessions) == 0

    @patch("execution_api.server.SandboxSession")
    async def test_create_propagates_start_error(self, MockSession):
        mock = AsyncMock()
        mock.start = AsyncMock(side_effect=RuntimeError("No VMs available"))
        MockSession.return_value = mock
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        with pytest.raises(RuntimeError, match="No VMs available"):
            await mgr.create()
        assert len(mgr.sessions) == 0  # Not added on failure

    @patch("execution_api.server.SandboxSession")
    async def test_is_full_after_delete(self, MockSession):
        MockSession.return_value = AsyncMock()
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=2, session_ttl=600,
        )
        e1 = await mgr.create()
        await mgr.create()
        assert mgr.is_full is True
        await mgr.delete(e1.session_id)
        assert mgr.is_full is False
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_execution_api.py -v`
Expected: PASS (37 tests — 17 model + 12 session manager + 8 edge cases)

- [ ] **Step 3: Commit**

```bash
git add tests/test_execution_api.py
git commit -m "test(api): add session manager edge case tests"
```

---

### Task 4: Result conversion and endpoint tests

**Files:**
- Modify: `tests/test_execution_api.py`

- [ ] **Step 1: Write tests for result conversion**

Add to `tests/test_execution_api.py`:

```python
from execution_api.server import _result_to_response, create_app
from sandbox_client import DisplayOutput, ExecutionError, ExecutionResult

import httpx


class TestResultConversion:
    def test_success_result(self):
        result = ExecutionResult(
            success=True, stdout="hello\n", stderr="", error=None,
            outputs=[], execution_count=1,
        )
        resp = _result_to_response(result)
        assert resp.success is True
        assert resp.stdout == "hello\n"
        assert resp.error is None
        assert resp.outputs == []
        assert resp.execution_count == 1

    def test_error_result(self):
        result = ExecutionResult(
            success=False, stdout="partial\n", stderr="warn\n",
            execution_count=3,
            error=ExecutionError(
                name="ZeroDivisionError", value="division by zero",
                traceback=["Traceback ...", "ZeroDivisionError: division by zero"],
            ),
            outputs=[],
        )
        resp = _result_to_response(result)
        assert resp.success is False
        assert resp.stdout == "partial\n"
        assert resp.stderr == "warn\n"
        assert resp.error.name == "ZeroDivisionError"
        assert len(resp.error.traceback) == 2

    def test_binary_output_base64_encoded(self):
        result = ExecutionResult(
            success=True, stdout="", stderr="", error=None,
            execution_count=1,
            outputs=[DisplayOutput(mime_type="image/png", data=b"\x89PNG")],
        )
        resp = _result_to_response(result)
        assert len(resp.outputs) == 1
        assert resp.outputs[0].data_b64 == base64.b64encode(b"\x89PNG").decode()
        assert resp.outputs[0].data is None

    def test_text_output_inlined(self):
        result = ExecutionResult(
            success=True, stdout="", stderr="", error=None,
            execution_count=1,
            outputs=[DisplayOutput(mime_type="text/html", data="<b>x</b>")],
        )
        resp = _result_to_response(result)
        assert resp.outputs[0].data == "<b>x</b>"
        assert resp.outputs[0].data_b64 is None

    def test_output_with_url_preserved(self):
        result = ExecutionResult(
            success=True, stdout="", stderr="", error=None,
            execution_count=1,
            outputs=[
                DisplayOutput(
                    mime_type="image/png", data=b"img",
                    url="http://cdn/session-1/output_0.png",
                ),
            ],
        )
        resp = _result_to_response(result)
        assert resp.outputs[0].url == "http://cdn/session-1/output_0.png"

    def test_multiple_outputs(self):
        result = ExecutionResult(
            success=True, stdout="", stderr="", error=None,
            execution_count=1,
            outputs=[
                DisplayOutput(mime_type="image/png", data=b"img"),
                DisplayOutput(mime_type="text/html", data="<p>chart</p>"),
            ],
        )
        resp = _result_to_response(result)
        assert len(resp.outputs) == 2
        assert resp.outputs[0].mime_type == "image/png"
        assert resp.outputs[1].mime_type == "text/html"
```

- [ ] **Step 2: Write endpoint tests**

Add to `tests/test_execution_api.py`:

```python
@pytest.fixture
def mock_sandbox_session():
    """A mocked SandboxSession for endpoint testing."""
    session = AsyncMock()
    session.start = AsyncMock()
    session.stop = AsyncMock()
    session.execute = AsyncMock(return_value=ExecutionResult(
        success=True, stdout="hello\n", stderr="", error=None,
        outputs=[], execution_count=1,
    ))
    return session


@pytest.fixture
async def client(mock_sandbox_session):
    """httpx AsyncClient backed by the FastAPI app with mocked sessions."""
    with patch("execution_api.server.SandboxSession") as MockSession:
        MockSession.return_value = mock_sandbox_session
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        app = create_app(session_manager=mgr)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
        ) as c:
            yield c, mock_sandbox_session


class TestEndpoints:
    async def test_create_session(self, client):
        c, mock = client
        resp = await c.post("/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert "created_at" in data
        mock.start.assert_awaited_once()

    async def test_create_session_with_timeout(self, client):
        c, mock = client
        resp = await c.post(
            "/sessions", json={"execution_timeout": 60},
        )
        assert resp.status_code == 200
        assert "session_id" in resp.json()

    async def test_list_sessions(self, client):
        c, mock = client
        await c.post("/sessions")
        resp = await c.get("/sessions")
        assert resp.status_code == 200
        sessions = resp.json()
        assert len(sessions) == 1
        assert "session_id" in sessions[0]
        assert "created_at" in sessions[0]
        assert "last_active" in sessions[0]

    async def test_list_sessions_empty(self, client):
        c, mock = client
        resp = await c.get("/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_delete_session(self, client):
        c, mock = client
        create_resp = await c.post("/sessions")
        sid = create_resp.json()["session_id"]
        resp = await c.delete(f"/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock.stop.assert_awaited_once()

    async def test_delete_nonexistent_404(self, client):
        c, mock = client
        resp = await c.delete("/sessions/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["error"] == "session not found"

    async def test_execute_in_session(self, client):
        c, mock = client
        create_resp = await c.post("/sessions")
        sid = create_resp.json()["session_id"]
        resp = await c.post(
            f"/sessions/{sid}/execute", json={"code": "print('hi')"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["stdout"] == "hello\n"
        assert data["execution_count"] == 1
        mock.execute.assert_awaited_once_with("print('hi')")

    async def test_execute_nonexistent_session_404(self, client):
        c, mock = client
        resp = await c.post(
            "/sessions/nonexistent/execute", json={"code": "x"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"] == "session not found"

    async def test_one_shot_execute(self, client):
        c, mock = client
        resp = await c.post("/execute", json={"code": "print('hi')"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        # One-shot: session created + started, executed, then stopped
        mock.start.assert_awaited()
        mock.execute.assert_awaited()
        mock.stop.assert_awaited()

    async def test_one_shot_with_timeout(self, client):
        c, mock = client
        resp = await c.post(
            "/execute", json={"code": "x", "timeout": 10},
        )
        assert resp.status_code == 200

    async def test_execute_error_returns_200(self, client):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=False, stdout="", stderr="", execution_count=3,
            error=ExecutionError(
                name="ZeroDivisionError", value="division by zero",
                traceback=["ZeroDivisionError: division by zero"],
            ),
            outputs=[],
        ))
        create_resp = await c.post("/sessions")
        sid = create_resp.json()["session_id"]
        resp = await c.post(
            f"/sessions/{sid}/execute", json={"code": "1/0"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["name"] == "ZeroDivisionError"

    async def test_max_sessions_503(self, client):
        c, mock = client
        # client fixture has max_sessions=20, create 20 sessions
        for _ in range(20):
            resp = await c.post("/sessions")
            assert resp.status_code == 200
        # 21st should fail
        resp = await c.post("/sessions")
        assert resp.status_code == 503
        assert resp.json()["error"] == "max sessions reached"

    async def test_no_vms_503(self, client):
        c, mock = client
        mock.start = AsyncMock(
            side_effect=RuntimeError("No VMs available"),
        )
        resp = await c.post("/sessions")
        assert resp.status_code == 503
        assert resp.json()["error"] == "no VMs available"

    async def test_one_shot_no_vms_503(self, client):
        c, mock = client
        mock.start = AsyncMock(
            side_effect=RuntimeError("No VMs available"),
        )
        resp = await c.post(
            "/execute", json={"code": "print('hi')"},
        )
        assert resp.status_code == 503
        assert resp.json()["error"] == "no VMs available"

    async def test_execute_with_rich_output(self, client):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=True, stdout="", stderr="", error=None,
            execution_count=1,
            outputs=[
                DisplayOutput(mime_type="image/png", data=b"\x89PNG"),
                DisplayOutput(mime_type="text/html", data="<table>x</table>"),
            ],
        ))
        create_resp = await c.post("/sessions")
        sid = create_resp.json()["session_id"]
        resp = await c.post(
            f"/sessions/{sid}/execute",
            json={"code": "plt.show()"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["outputs"]) == 2
        assert data["outputs"][0]["mime_type"] == "image/png"
        assert data["outputs"][0]["data_b64"] is not None
        assert data["outputs"][0]["data"] is None
        assert data["outputs"][1]["mime_type"] == "text/html"
        assert data["outputs"][1]["data"] == "<table>x</table>"
        assert data["outputs"][1]["data_b64"] is None
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `uv run pytest tests/test_execution_api.py -v`
Expected: PASS (62 tests — 17 model + 12 session + 8 edge + 6 conversion + 19 endpoint)

- [ ] **Step 4: Commit**

```bash
git add tests/test_execution_api.py
git commit -m "test(api): add result conversion and endpoint tests"
```

---

## Chunk 3: Tool Schemas + Packaging + Examples

### Task 5: Tool schema JSON files

**Files:**
- Create: `execution_api/tool_schemas/claude.json`
- Create: `execution_api/tool_schemas/openai.json`

- [ ] **Step 1: Create Claude tool schema**

```json
[
  {
    "name": "execute_python_code",
    "description": "Execute Python code in an isolated Firecracker microVM sandbox. Use this when the user asks you to run code, analyze data, create visualizations, or perform calculations. The sandbox has numpy, pandas, matplotlib, scipy, plotly, and seaborn pre-installed. State persists across calls within the same conversation.",
    "input_schema": {
      "type": "object",
      "properties": {
        "code": {
          "type": "string",
          "description": "Python code to execute"
        }
      },
      "required": ["code"]
    }
  }
]
```

- [ ] **Step 2: Create OpenAI tool schema**

```json
[
  {
    "type": "function",
    "function": {
      "name": "execute_python_code",
      "description": "Execute Python code in an isolated Firecracker microVM sandbox. Use this when the user asks you to run code, analyze data, create visualizations, or perform calculations. The sandbox has numpy, pandas, matplotlib, scipy, plotly, and seaborn pre-installed. State persists across calls within the same conversation.",
      "parameters": {
        "type": "object",
        "properties": {
          "code": {
            "type": "string",
            "description": "Python code to execute"
          }
        },
        "required": ["code"]
      }
    }
  }
]
```

- [ ] **Step 3: Commit**

```bash
git add execution_api/tool_schemas/
git commit -m "feat(api): add Claude and OpenAI tool schemas"
```

---

### Task 6: Package wiring and example scripts

**Files:**
- Modify: `execution_api/__init__.py`
- Modify: `pyproject.toml`
- Create: `examples/oneshot_example.py`
- Create: `examples/conversation_example.py`

- [ ] **Step 1: Update package init with re-exports**

```python
# execution_api/__init__.py
"""Execution API — REST server for sandboxed Python code execution."""

from .models import (
    CreateSessionRequest,
    CreateSessionResponse,
    DeleteResponse,
    ErrorDetail,
    ErrorResponse,
    ExecuteRequest,
    ExecuteResponse,
    OneShotRequest,
    OutputItem,
    SessionInfo,
)
from .server import SessionEntry, SessionManager, create_app

__all__ = [
    "CreateSessionRequest",
    "CreateSessionResponse",
    "DeleteResponse",
    "ErrorDetail",
    "ErrorResponse",
    "ExecuteRequest",
    "ExecuteResponse",
    "OneShotRequest",
    "OutputItem",
    "SessionEntry",
    "SessionInfo",
    "SessionManager",
    "create_app",
]
```

- [ ] **Step 2: Add execution_api to pyproject.toml wheel packages**

In `pyproject.toml`, change:
```toml
packages = ["fc_provisioner", "fc_pool_manager", "sandbox_client"]
```
to:
```toml
packages = ["fc_provisioner", "fc_pool_manager", "sandbox_client", "execution_api"]
```

- [ ] **Step 3: Create one-shot example**

```python
# examples/oneshot_example.py
"""One-shot example: user asks a question, Claude writes code, sandbox runs it."""

import asyncio

import anthropic

from sandbox_client import SandboxSession

TOOL_DEFINITION = {
    "name": "execute_python_code",
    "description": (
        "Execute Python code in an isolated sandbox. The sandbox has numpy, "
        "pandas, matplotlib, scipy, plotly, and seaborn pre-installed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute",
            },
        },
        "required": ["code"],
    },
}


def format_result(result):
    """Convert ExecutionResult to a string for the LLM."""
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]: {result.stderr}")
    if result.error:
        parts.append(f"[error]: {result.error.name}: {result.error.value}")
    for i, output in enumerate(result.outputs):
        if output.url:
            parts.append(f"[output {i}]: {output.mime_type} at {output.url}")
        elif isinstance(output.data, str):
            parts.append(f"[output {i}]: {output.mime_type}\n{output.data}")
        else:
            parts.append(
                f"[output {i}]: {output.mime_type} ({len(output.data)} bytes)",
            )
    return "\n".join(parts) or "(no output)"


async def main():
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        tools=[TOOL_DEFINITION],
        messages=[
            {"role": "user", "content": "What's the 100th Fibonacci number?"},
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "execute_python_code":
            async with SandboxSession("http://localhost:8888") as session:
                result = await session.execute(block.input["code"])

            tool_result = format_result(result)

            final = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                tools=[TOOL_DEFINITION],
                messages=[
                    {
                        "role": "user",
                        "content": "What's the 100th Fibonacci number?",
                    },
                    {"role": "assistant", "content": response.content},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": tool_result,
                            },
                        ],
                    },
                ],
            )
            print(final.content[0].text)


asyncio.run(main())
```

- [ ] **Step 4: Create conversation example**

```python
# examples/conversation_example.py
"""Multi-turn conversation: session persists, variables carry over."""

import asyncio

import anthropic

from sandbox_client import SandboxSession

TOOL_DEFINITION = {
    "name": "execute_python_code",
    "description": (
        "Execute Python code in an isolated sandbox. The sandbox has numpy, "
        "pandas, matplotlib, scipy, plotly, and seaborn pre-installed. "
        "State persists across calls within the same conversation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute",
            },
        },
        "required": ["code"],
    },
}


def format_result(result):
    """Convert ExecutionResult to a string for the LLM."""
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]: {result.stderr}")
    if result.error:
        parts.append(f"[error]: {result.error.name}: {result.error.value}")
    for i, output in enumerate(result.outputs):
        if output.url:
            parts.append(f"[output {i}]: {output.mime_type} at {output.url}")
        elif isinstance(output.data, str):
            parts.append(f"[output {i}]: {output.mime_type}\n{output.data}")
        else:
            parts.append(
                f"[output {i}]: {output.mime_type} ({len(output.data)} bytes)",
            )
    return "\n".join(parts) or "(no output)"


async def main():
    client = anthropic.Anthropic()
    messages = []

    session = SandboxSession("http://localhost:8888")
    await session.start()

    try:
        while True:
            user_input = input("> ")
            if user_input.lower() in ("exit", "quit"):
                break

            messages.append({"role": "user", "content": user_input})

            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                tools=[TOOL_DEFINITION],
                messages=messages,
            )

            # Handle tool use loop (LLM may call tool multiple times)
            while response.stop_reason == "tool_use":
                messages.append(
                    {"role": "assistant", "content": response.content},
                )
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        result = await session.execute(block.input["code"])
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": format_result(result),
                            },
                        )

                messages.append({"role": "user", "content": tool_results})
                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=4096,
                    tools=[TOOL_DEFINITION],
                    messages=messages,
                )

            # Print final text response
            messages.append(
                {"role": "assistant", "content": response.content},
            )
            for block in response.content:
                if hasattr(block, "text"):
                    print(block.text)
    finally:
        await session.stop()


asyncio.run(main())
```

- [ ] **Step 5: Verify imports and run all unit tests**

Run: `uv run python -c "from execution_api import create_app, SessionManager, ExecuteResponse; print('OK')"`
Expected: `OK`

Run: `uv run pytest tests/test_execution_api.py -v`
Expected: All tests pass

Run: `uv run python -c "import ast; ast.parse(open('examples/oneshot_example.py').read()); ast.parse(open('examples/conversation_example.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Step 6: Commit**

```bash
git add execution_api/__init__.py pyproject.toml examples/
git commit -m "feat(api): add tool schemas, package exports, and example scripts"
```

---

## Chunk 4: Integration Tests

### Task 7: API integration tests

**Files:**
- Modify: `tests/test_integration.py`

These tests require a running Execution API server + Kernel Gateway + pool manager (run via `scripts/remote-test.sh`).

- [ ] **Step 1: Add Execution API integration tests**

Add to the bottom of `tests/test_integration.py`:

```python
EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")


class TestExecutionAPI:
    async def test_api_hello_world(self):
        """POST /execute with print('hello') returns stdout."""
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
        """Create → execute twice (state persists) → delete."""
        async with aiohttp.ClientSession() as http:
            # Create
            resp = await http.post(f"{EXECUTION_API_URL}/sessions")
            assert resp.status == 200
            sid = (await resp.json())["session_id"]

            # Execute 1: set variable
            resp = await http.post(
                f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                json={"code": "x = 42"},
            )
            assert resp.status == 200
            assert (await resp.json())["success"] is True

            # Execute 2: read variable (state persists)
            resp = await http.post(
                f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                json={"code": "print(x)"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["stdout"].strip() == "42"

            # List (should include our session)
            resp = await http.get(f"{EXECUTION_API_URL}/sessions")
            assert resp.status == 200
            sessions = await resp.json()
            assert any(s["session_id"] == sid for s in sessions)

            # Delete
            resp = await http.delete(
                f"{EXECUTION_API_URL}/sessions/{sid}",
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

    async def test_api_error_result(self):
        """Execute 1/0 → 200 with success=false."""
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
        """Execute matplotlib plot → response contains base64 PNG."""
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
        """Execute on nonexistent session → 404."""
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                f"{EXECUTION_API_URL}/sessions/nonexistent/execute",
                json={"code": "x"},
            )
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "session not found"
```

- [ ] **Step 2: Verify unit tests still pass**

Run: `uv run pytest tests/ -v -m "not integration"`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "test(api): add Execution API integration tests"
```

- [ ] **Step 4: Run full test suite on remote host**

Run: `./scripts/remote-test.sh xuwang@192.168.1.53 --skip-setup`
Expected: All unit tests pass, integration tests pass (including new Execution API tests; `remote-test.sh` now starts the API server automatically)
