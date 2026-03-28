"""Execution API — FastAPI server wrapping SandboxSession."""

# pyright: reportMissingImports=false

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ._sandbox import ExecutionResult, LocalArtifactStore, SandboxSession
from .models import (
    CreateSessionRequest,
    CreateSessionResponse,
    DashboardRequest,
    DashboardResponse,
    DeleteResponse,
    ErrorDetail,
    ExecuteRequest,
    ExecuteResponse,
    FileListResponse,
    FileUploadResponse,
    OneShotRequest,
    OutputItem,
    SessionInfo,
)

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8888")
USE_PER_VM_KG = os.environ.get("USE_PER_VM_KG", "false").lower() == "true"
POOL_MANAGER_URL = os.environ.get("POOL_MANAGER_URL", "http+unix:///var/run/fc-pool.sock")
SESSION_TTL = int(os.environ.get("SESSION_TTL", "600"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "20"))
DEFAULT_TIMEOUT = int(os.environ.get("DEFAULT_TIMEOUT", "30"))
ARTIFACT_BASE_DIR = os.environ.get("ARTIFACT_BASE_DIR")
ARTIFACT_URL_PREFIX = os.environ.get("ARTIFACT_URL_PREFIX")
SERVE_ARTIFACTS = os.environ.get("SERVE_ARTIFACTS", "true").lower() == "true"
PORT = int(os.environ.get("PORT", "8000"))
UPLOAD_MAX_BYTES = 50 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 2 * 1024 * 1024

_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")



# ── Session Manager ──────────────────────────────────────────────────────


class SessionState(Enum):
    CREATING = "creating"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass
class SessionEntry:
    session: SandboxSession
    session_id: str
    created_at: float
    last_active: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_dashboard: str | None = None
    state: SessionState = SessionState.ACTIVE
    vm_id: str | None = None
    vm_ip: str | None = None


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
        pool_client: "PoolClient | None" = None,
    ):
        self._gateway_url = gateway_url
        self._default_timeout = default_timeout
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl
        self._sessions: dict[str, SessionEntry] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._pool_client = pool_client

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
        if self._pool_client is not None:
            return await self._create_per_vm(execution_timeout)
        return await self._create_central_kg(execution_timeout)

    async def _create_central_kg(self, execution_timeout: int | None = None) -> SessionEntry:
        async with self._lock:
            if len(self._sessions) >= self._max_sessions:
                raise RuntimeError("max sessions reached")

            timeout = execution_timeout or self._default_timeout
            session = SandboxSession(
                gateway_url=self._gateway_url,
                default_timeout=timeout,
                artifact_store=_make_artifact_store(),
            )

            session_id = uuid.uuid4().hex
            now = time.time()
            entry = SessionEntry(
                session=session,
                session_id=session_id,
                created_at=now,
                last_active=now,
                state=SessionState.CREATING,
            )
            self._sessions[session_id] = entry

            try:
                await session.start()
            except Exception:
                entry.state = SessionState.CLOSING
                try:
                    await session.stop()
                except Exception:
                    logger.debug("Failed to stop session during create cleanup", exc_info=True)
                entry.state = SessionState.CLOSED
                self._sessions.pop(session_id, None)
                raise

            entry.state = SessionState.ACTIVE
            return entry

    async def _create_per_vm(self, execution_timeout: int | None = None) -> SessionEntry:
        assert self._pool_client is not None
        async with self._lock:
            if len(self._sessions) >= self._max_sessions:
                raise RuntimeError("max sessions reached")

        vm = await self._pool_client.acquire()
        vm_id = vm["vm_id"]
        vm_ip = vm["ip"]

        timeout = execution_timeout or self._default_timeout
        session = SandboxSession(
            gateway_url=f"http://{vm_ip}:8888",
            default_timeout=timeout,
            artifact_store=_make_artifact_store(),
            discover_kernel=True,
        )

        session_id = uuid.uuid4().hex
        now = time.time()
        entry = SessionEntry(
            session=session,
            session_id=session_id,
            created_at=now,
            last_active=now,
            state=SessionState.CREATING,
            vm_id=vm_id,
            vm_ip=vm_ip,
        )

        async with self._lock:
            self._sessions[session_id] = entry

        try:
            await session.start()
        except Exception:
            entry.state = SessionState.CLOSING
            try:
                await session.stop()
            except Exception:
                logger.debug("Failed to stop session during create cleanup", exc_info=True)
            try:
                await self._pool_client.destroy(vm_id)
            except Exception:
                logger.debug("Failed to destroy VM %s during create cleanup", vm_id, exc_info=True)
            entry.state = SessionState.CLOSED
            async with self._lock:
                self._sessions.pop(session_id, None)
            raise

        entry.state = SessionState.ACTIVE
        return entry

    def get(self, session_id: str) -> SessionEntry | None:
        entry = self._sessions.get(session_id)
        if entry is None:
            return None
        if entry.state in (SessionState.CLOSING, SessionState.CLOSED):
            return None
        return entry

    async def delete(self, session_id: str) -> bool:
        return await self.destroy(session_id)

    async def destroy(self, session_id: str) -> bool:
        async with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return False
            if entry.state in (SessionState.CLOSING, SessionState.CLOSED):
                return True
            entry.state = SessionState.CLOSING

        async with entry.lock:
            entry.active_dashboard = None
            try:
                await entry.session.stop()
            except Exception:
                logger.debug("Failed to stop session %s", session_id, exc_info=True)
            if entry.vm_id and self._pool_client:
                try:
                    await self._pool_client.destroy(entry.vm_id)
                except Exception:
                    logger.debug("Failed to destroy VM %s", entry.vm_id, exc_info=True)

        async with self._lock:
            current = self._sessions.get(session_id)
            if current is entry:
                entry.state = SessionState.CLOSED
                self._sessions.pop(session_id, None)
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
            await self.destroy(sid)

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
                await self.destroy(sid)
            except Exception:
                pass
        if self._pool_client:
            await self._pool_client.close()


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


def _validate_safe_filename(filename: str) -> str:
    if not filename or not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=422, detail="unsafe filename")
    return filename


async def _upload_file_to_vm(
    session: SandboxSession,
    content: bytes,
    dest_path: str,
    chunk_size: int | None = None,
) -> None:
    if chunk_size is None:
        chunk_size = UPLOAD_CHUNK_SIZE
    tmp_path = dest_path + ".tmp"

    try:
        if len(content) == 0:
            code = (
                "import os\n"
                "os.makedirs('/data', exist_ok=True)\n"
                f"open('{dest_path}', 'wb').close()\n"
            )
            result = await session.execute(code)
            if not result.success:
                error_msg = result.error.value if result.error else "unknown"
                raise RuntimeError(f"File write failed: {error_msg}")
            return

        for offset in range(0, len(content), chunk_size):
            chunk = content[offset:offset + chunk_size]
            b64 = base64.b64encode(chunk).decode()
            mode = "wb" if offset == 0 else "ab"
            code = (
                "import base64, os\n"
                "os.makedirs('/data', exist_ok=True)\n"
                f"with open('{tmp_path}', '{mode}') as _f:\n"
                f"    _f.write(base64.b64decode('{b64}'))\n"
            )
            result = await session.execute(code)
            if not result.success:
                error_msg = result.error.value if result.error else "unknown"
                raise RuntimeError(f"File write failed: {error_msg}")

        rename_code = f"import os; os.replace('{tmp_path}', '{dest_path}')\n"
        result = await session.execute(rename_code)
        if not result.success:
            error_msg = result.error.value if result.error else "unknown"
            raise RuntimeError(f"File rename failed: {error_msg}")

    except Exception:
        try:
            await session.execute(
                f"import os\ntry: os.remove('{tmp_path}')\nexcept FileNotFoundError: pass\n",
            )
        except Exception:
            pass
        raise


# ── FastAPI App ──────────────────────────────────────────────────────────


_MAX_CONCURRENT_CLEANUPS = 10
_cleanup_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CLEANUPS)
_cleanup_tasks: set[asyncio.Task] = set()


def create_app(session_manager: SessionManager | None = None) -> FastAPI:
    if session_manager is None:
        pool_client = None
        if USE_PER_VM_KG:
            from execution_api.pool_client import PoolClient
            pool_client = PoolClient(POOL_MANAGER_URL)
            logger.info("Per-VM KG mode enabled (pool: %s)", POOL_MANAGER_URL)
        session_manager = SessionManager(pool_client=pool_client)

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
        except Exception as e:
            logger.error("Session creation failed: %s", e)
            raise HTTPException(status_code=503, detail="session creation failed")
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
        deleted = await session_manager.destroy(session_id)
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
        if entry.state is not SessionState.ACTIVE:
            raise HTTPException(status_code=409, detail="session not active")
        try:
            async with entry.lock:
                if entry.state is not SessionState.ACTIVE:
                    raise HTTPException(status_code=409, detail="session not active")
                entry.last_active = time.time()
                result = await entry.session.execute(req.code)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Execute failed for session %s: %s", session_id, exc)
            raise HTTPException(status_code=503, detail=f"sandbox unavailable: {exc}")
        return _result_to_response(result)

    @app.post("/sessions/{session_id}/files", response_model=FileUploadResponse)
    async def upload_file(
        session_id: str,
        file: UploadFile = File(...),
    ):
        entry = session_manager.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")

        filename = _validate_safe_filename(file.filename or "")
        dest_path = f"/data/{filename}"

        content = await file.read()
        if len(content) > UPLOAD_MAX_BYTES:
            raise HTTPException(status_code=413, detail="file too large")

        async with entry.lock:
            entry.last_active = time.time()
            try:
                await _upload_file_to_vm(entry.session, content, dest_path)
            except RuntimeError as exc:
                raise HTTPException(status_code=500, detail=str(exc))
        return FileUploadResponse(path=dest_path, filename=filename, size=len(content))

    @app.get("/sessions/{session_id}/files", response_model=FileListResponse)
    async def list_files(session_id: str):
        entry = session_manager.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")

        async with entry.lock:
            entry.last_active = time.time()
            result = await entry.session.execute(
                "import os, json\n"
                "files = []\n"
                "if os.path.isdir('/data'):\n"
                "    files = [{\"filename\": f, \"path\": f\"/data/{f}\", \"size\": os.path.getsize(f\"/data/{f}\")} for f in os.listdir('/data/') if os.path.isfile(f\"/data/{f}\")]\n"
                "print(json.dumps(files))"
            )
        if not result.success:
            error_msg = result.error.value if result.error else "unknown"
            raise HTTPException(status_code=500, detail=f"file list failed: {error_msg}")
        try:
            files = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="invalid file list output")
        return FileListResponse(files=files)

    @app.get("/sessions/{session_id}/files/{filename}")
    async def download_file(session_id: str, filename: str):
        entry = session_manager.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        safe_filename = _validate_safe_filename(filename)

        async with entry.lock:
            entry.last_active = time.time()
            result = await entry.session.execute(
                "import os, json\n"
                f"path = '/data/{safe_filename}'\n"
                "if not os.path.isfile(path):\n"
                "    print(json.dumps({'error': 'not_found'}))\n"
                "else:\n"
                "    size = os.path.getsize(path)\n"
                "    print(json.dumps({'size': size}))\n"
            )
        if not result.success:
            error_msg = result.error.value if result.error else "unknown"
            raise HTTPException(status_code=500, detail=f"file read failed: {error_msg}")
        try:
            output = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="invalid file check output")
        if output.get("error") == "not_found":
            raise HTTPException(status_code=404, detail="file not found")
        file_size = output.get("size", 0)
        if file_size > UPLOAD_MAX_BYTES:
            raise HTTPException(status_code=413, detail="file too large")

        async with entry.lock:
            chunks = []
            chunk_size = UPLOAD_CHUNK_SIZE
            for offset in range(0, file_size, chunk_size):
                read_result = await entry.session.execute(
                    "import base64\n"
                    f"with open('/data/{safe_filename}', 'rb') as _f:\n"
                    f"    _f.seek({offset})\n"
                    f"    print(base64.b64encode(_f.read({chunk_size})).decode())\n"
                )
                if not read_result.success:
                    error_msg = read_result.error.value if read_result.error else "unknown"
                    raise HTTPException(status_code=500, detail=f"file read failed: {error_msg}")
                chunks.append(base64.b64decode(read_result.stdout.strip()))

        file_bytes = b"".join(chunks)
        content_type, _ = mimetypes.guess_type(safe_filename)
        return Response(
            content=file_bytes,
            media_type=content_type or "application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
        )

    @app.delete("/sessions/{session_id}/files/{filename}", response_model=DeleteResponse)
    async def delete_file(session_id: str, filename: str):
        entry = session_manager.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        safe_filename = _validate_safe_filename(filename)

        async with entry.lock:
            entry.last_active = time.time()
            result = await entry.session.execute(
                "import os, json\n"
                f"path = '/data/{safe_filename}'\n"
                "if not os.path.isfile(path):\n"
                "    print(json.dumps({'error': 'not_found'}))\n"
                "else:\n"
                "    os.remove(path)\n"
                "    print(json.dumps({'ok': True}))\n"
            )
        if not result.success:
            error_msg = result.error.value if result.error else "unknown"
            raise HTTPException(status_code=500, detail=f"file delete failed: {error_msg}")
        try:
            output = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="invalid file delete output")
        if output.get("error") == "not_found":
            raise HTTPException(status_code=404, detail="file not found")
        return DeleteResponse()

    @app.post("/execute", response_model=ExecuteResponse)
    async def one_shot_execute(
        request: Request,
        code: str | None = Form(default=None),
        timeout: int | None = Form(default=None),
        file: UploadFile | None = File(default=None),
        files: list[UploadFile] | None = File(default=None),
    ):
        req_code = code
        req_timeout = timeout
        content_type = request.headers.get("content-type", "")

        if "multipart" in content_type:
            pass
        else:
            try:
                payload = await request.json()
            except Exception:
                raise HTTPException(status_code=422, detail="invalid JSON body")
            try:
                req = OneShotRequest.model_validate(payload)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            req_code = req.code
            req_timeout = req.timeout
            files = []

        if "multipart" in content_type:
            if not req_code or (isinstance(req_code, str) and not req_code.strip()):
                raise HTTPException(status_code=422, detail="'code' field is required")

        if not req_code:
            raise HTTPException(status_code=422, detail="code is required")

        timeout = req_timeout or session_manager.default_timeout
        session = SandboxSession(
            gateway_url=session_manager.gateway_url,
            default_timeout=timeout,
            artifact_store=_make_artifact_store(),
        )
        try:
            await session.start()
        except RuntimeError:
            raise HTTPException(status_code=503, detail="no VMs available")
        except Exception as e:
            logger.error("One-shot session creation failed: %s", e)
            raise HTTPException(status_code=503, detail="session creation failed")

        try:
            upload_files: list[UploadFile] = []
            if file is not None:
                upload_files.append(file)
            if files:
                upload_files.extend(files)

            if upload_files:
                for uploaded_file in upload_files:
                    filename = _validate_safe_filename(uploaded_file.filename or "")
                    content = await uploaded_file.read()
                    if len(content) > UPLOAD_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="file too large")
                    await _upload_file_to_vm(session, content, f"/data/{filename}")
            result = await session.execute(req_code)
        except Exception:
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
        from execution_api.dashboard_sanitizer import sanitize_dashboard_code
        entry = session_manager.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        kernel_id = getattr(entry.session, "_kernel_id", None)
        if kernel_id is None:
            raise HTTPException(status_code=503, detail="kernel not available")
        entry.last_active = time.time()

        sanitized_code = sanitize_dashboard_code(req.code)

        async with entry.lock:
            app_id = uuid.uuid4().hex[:12]
            escaped = sanitized_code.replace("\\", "\\\\").replace("'''", "\\'\\'\\'")

            try:
                preflight = await entry.session.execute(
                    "import os, tempfile, traceback\n"
                    "os.makedirs('/apps', exist_ok=True)\n"
                    f"code = '''{escaped}'''\n"
                    "tmp = tempfile.mktemp(dir='/apps', suffix='_preflight.py')\n"
                    "with open(tmp, 'w') as f: f.write(code)\n"
                    "try:\n"
                    "    exec(compile(open(tmp).read(), tmp, 'exec'))\n"
                    "    print('PREFLIGHT_OK')\n"
                    "except Exception:\n"
                    "    print('PREFLIGHT_FAIL')\n"
                    "    traceback.print_exc()\n"
                    "finally:\n"
                    "    os.remove(tmp)\n"
                )
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"dashboard preflight failed: {exc}")
            stdout = preflight.stdout if preflight.success else ""
            if "PREFLIGHT_OK" not in stdout:
                error_detail = stdout.replace("PREFLIGHT_FAIL\n", "").strip()
                raise HTTPException(
                    status_code=422,
                    detail=f"dashboard code failed pre-flight check:\n{error_detail}",
                )

            prefix = f"/dash/{kernel_id}"
            try:
                await entry.session.execute(
                    "import os, tempfile\n"
                    f"code = '''{escaped}'''\n"
                    "tmp = tempfile.mktemp(dir='/apps', suffix='.py')\n"
                    "with open(tmp, 'w') as f: f.write(code)\n"
                    "os.replace(tmp, '/apps/app.py')\n"
                    f"with open('/apps/.prefix', 'w') as f: f.write('{prefix}')\n"
                    "print('dashboard deployed')"
                )
                await entry.session.execute(
                    "import time, urllib.request\n"
                    "for _ in range(20):\n"
                    "    try:\n"
                    f"        r = urllib.request.urlopen('http://127.0.0.1:5006{prefix}/app', timeout=2)\n"
                    "        if r.status == 200:\n"
                    "            print('panel ready')\n"
                    "            break\n"
                    "    except Exception:\n"
                    "        pass\n"
                    "    time.sleep(0.5)\n"
                    "else:\n"
                    "    print('panel not ready after 10s')\n"
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
