"""Execution API — FastAPI server wrapping SandboxSession."""

import asyncio
import base64
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from sandbox_client import ExecutionResult, LocalArtifactStore, SandboxSession
from .models import (
    CreateSessionRequest,
    CreateSessionResponse,
    DashboardRequest,
    DashboardResponse,
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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_dashboard: str | None = None


def _make_artifact_store() -> LocalArtifactStore | None:
    if not ARTIFACT_BASE_DIR:
        return None
    url_prefix = ARTIFACT_URL_PREFIX or f"http://localhost:{PORT}/artifacts"
    return LocalArtifactStore(base_dir=ARTIFACT_BASE_DIR, url_prefix=url_prefix)


class SessionManager:

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
        self._lock = asyncio.Lock()

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
        async with self._lock:
            if len(self._sessions) >= self._max_sessions:
                raise RuntimeError("max sessions reached")

            timeout = execution_timeout or self._default_timeout
            session = SandboxSession(
                gateway_url=self._gateway_url,
                default_timeout=timeout,
                artifact_store=_make_artifact_store(),
            )
            try:
                await session.start()
            except Exception:
                try:
                    await session.stop()
                except Exception:
                    pass
                raise

            session_id = uuid.uuid4().hex
            now = time.time()
            entry = SessionEntry(
                session=session,
                session_id=session_id,
                created_at=now,
                last_active=now,
                active_dashboard=None,
            )
            self._sessions[session_id] = entry
            return entry

    def get(self, session_id: str) -> SessionEntry | None:
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> bool:
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            return False

        async with entry.lock:
            entry.active_dashboard = None
            try:
                await entry.session.stop()
            except Exception:
                logger.debug("Failed to stop session %s", session_id, exc_info=True)
        return True

    def list_sessions(self) -> list[SessionEntry]:
        return list(self._sessions.values())

    async def cleanup_expired(self) -> None:
        now = time.time()
        expired = [
            sid for sid, entry in self._sessions.items()
            if now - entry.last_active > self._session_ttl
        ]
        for sid in expired:
            await self.delete(sid)

    def start_cleanup_task(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.cleanup_expired()
            except Exception:
                logger.debug("Cleanup error", exc_info=True)

    async def shutdown(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        for sid in list(self._sessions):
            try:
                await self.delete(sid)
            except Exception:
                pass


# ── Result Conversion ────────────────────────────────────────────────────


def _result_to_response(result: ExecutionResult) -> ExecuteResponse:
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


_MAX_CONCURRENT_CLEANUPS = 10
_cleanup_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CLEANUPS)
_cleanup_tasks: set[asyncio.Task] = set()


def create_app(session_manager: SessionManager | None = None) -> FastAPI:
    if session_manager is None:
        session_manager = SessionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        session_manager.start_cleanup_task()
        yield
        if _cleanup_tasks:
            logger.info("Draining %d background cleanup tasks...", len(_cleanup_tasks))
            await asyncio.gather(*_cleanup_tasks, return_exceptions=True)
        await session_manager.shutdown()

    app = FastAPI(title="Execution API", lifespan=lifespan)
    app.state.session_manager = session_manager

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request, exc):
        return JSONResponse(
            status_code=422,
            content={"error": str(exc)},
        )

    # ── Session CRUD ─────────────────────────────────────────────────

    @app.post("/sessions", response_model=CreateSessionResponse)
    async def create_session(
        req: CreateSessionRequest = CreateSessionRequest(),
    ):
        try:
            entry = await session_manager.create(req.execution_timeout)
        except RuntimeError as e:
            detail = "max sessions reached" if "max sessions" in str(e) else "no VMs available"
            raise HTTPException(status_code=503, detail=detail)
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
        async with entry.lock:
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
        started = False
        try:
            await session.start()
            started = True
            result = await session.execute(req.code)
        except RuntimeError:
            if started:
                _schedule_cleanup(session)
            raise HTTPException(status_code=503, detail="no VMs available")
        except Exception:
            if started:
                _schedule_cleanup(session)
            raise
        _schedule_cleanup(session)
        return _result_to_response(result)

    def _schedule_cleanup(session: SandboxSession) -> None:
        task = asyncio.create_task(_safe_stop(session))
        _cleanup_tasks.add(task)
        task.add_done_callback(_cleanup_tasks.discard)

    async def _safe_stop(session: SandboxSession) -> None:
        async with _cleanup_semaphore:
            try:
                await session.stop()
            except Exception:
                logger.debug("Background session cleanup failed", exc_info=True)

    @app.post("/sessions/{session_id}/dashboard", response_model=DashboardResponse)
    async def launch_dashboard(session_id: str, req: DashboardRequest):
        entry = session_manager.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        kernel_id = getattr(entry.session, "_kernel_id", None)
        if kernel_id is None:
            raise HTTPException(status_code=503, detail="kernel not available")
        entry.last_active = time.time()

        async with entry.lock:
            app_id = uuid.uuid4().hex[:12]
            escaped = req.code.replace("\\", "\\\\").replace("'''", "\\'\\'\\'")
            try:
                await entry.session.execute(
                    "import os, tempfile\n"
                    "os.makedirs('/apps', exist_ok=True)\n"
                    f"code = '''{escaped}'''\n"
                    "tmp = tempfile.mktemp(dir='/apps', suffix='.py')\n"
                    "with open(tmp, 'w') as f: f.write(code)\n"
                    f"os.replace(tmp, '/apps/dash_{app_id}.py')\n"
                    "print('dashboard deployed')"
                )
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"dashboard deploy error: {exc}")

            entry.active_dashboard = app_id
            return DashboardResponse(
                url=f"/dash/{kernel_id}/app",
                session_id=session_id,
                app_id=app_id,
            )

    @app.delete("/sessions/{session_id}/dashboard", response_model=DeleteResponse)
    async def stop_dashboard(session_id: str):
        entry = session_manager.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        entry.last_active = time.time()

        async with entry.lock:
            try:
                await entry.session.execute(
                    "import glob, os\n"
                    "for f in glob.glob('/apps/dash_*.py'): os.remove(f)"
                )
            except Exception:
                pass
            entry.active_dashboard = None
            return DeleteResponse()

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
