import base64
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution_api.models import (
    CreateSessionRequest,
    CreateSessionResponse,
    DeleteResponse,
    ErrorDetail,
    ErrorResponse,
    ExecuteRequest,
    ExecuteResponse,
    FileInfo,
    FileListResponse,
    FileUploadResponse,
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

    def test_file_upload_response_model(self):
        resp = FileUploadResponse(path="/data/test.csv", filename="test.csv", size=123)
        data = resp.model_dump()
        assert data["path"] == "/data/test.csv"
        assert data["filename"] == "test.csv"
        assert data["size"] == 123

    def test_file_list_response_model(self):
        resp = FileListResponse(
            files=[FileInfo(filename="a.csv", path="/data/a.csv", size=10)],
        )
        data = resp.model_dump()
        assert len(data["files"]) == 1
        assert data["files"][0]["filename"] == "a.csv"
        assert data["files"][0]["path"] == "/data/a.csv"
        assert data["files"][0]["size"] == 10

    def test_dashboard_request(self):
        from execution_api.models import DashboardRequest
        req = DashboardRequest(code="import panel as pn")
        assert req.code == "import panel as pn"

    def test_dashboard_response(self):
        from execution_api.models import DashboardResponse
        resp = DashboardResponse(
            url="/dash/sess123/dash_abcd",
            session_id="sess123",
            app_id="abcd",
        )
        data = resp.model_dump(mode="json")
        assert data["url"] == "/dash/sess123/dash_abcd"
        assert data["session_id"] == "sess123"
        assert data["app_id"] == "abcd"


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
        assert len(entry.session_id) == 32
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
        assert deleted is True

    @patch("execution_api.server.SandboxSession")
    async def test_properties(self, MockSession):
        mgr = SessionManager(
            gateway_url="http://gw:8888", default_timeout=45,
            max_sessions=10, session_ttl=300,
        )
        assert mgr.gateway_url == "http://gw:8888"
        assert mgr.default_timeout == 45
        assert mgr.is_full is False


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
        entry.last_active = time.time() - 9999
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
        await mgr.shutdown()
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
        assert len(mgr.sessions) == 0

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


class TestSessionManagerDashboardState:
    @patch("execution_api.server.SandboxSession")
    async def test_create_sets_active_dashboard_none(self, MockSession):
        session = AsyncMock()
        session._kernel_id = "kid-1"
        MockSession.return_value = session
        mgr = SessionManager(gateway_url="http://gw:8888")
        entry = await mgr.create()
        assert entry.active_dashboard is None


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
        assert resp.error is not None
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


@pytest.fixture
def mock_sandbox_session():
    session = AsyncMock()
    session.start = AsyncMock()
    session.stop = AsyncMock()
    session._kernel_id = "test-kernel-id"
    session.execute = AsyncMock(return_value=ExecutionResult(
        success=True, stdout="hello\n", stderr="", error=None,
        outputs=[], execution_count=1,
    ))
    return session


@pytest.fixture
async def client(mock_sandbox_session):
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
        mock.start.assert_awaited()
        mock.execute.assert_awaited()

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
        for _ in range(20):
            resp = await c.post("/sessions")
            assert resp.status_code == 200
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

    async def test_validation_error_returns_error_envelope(self, client):
        c, mock = client
        resp = await c.post(
            "/sessions/abc/execute", content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422
        assert "error" in resp.json()

    async def test_one_shot_start_failure_returns_503(self, client):
        c, mock = client
        mock.start = AsyncMock(
            side_effect=RuntimeError("No VMs available"),
        )
        resp = await c.post("/execute", json={"code": "x"})
        assert resp.status_code == 503

    @patch("execution_api.server.SandboxSession")
    async def test_create_start_failure_calls_stop(self, MockSession):
        mock = AsyncMock()
        mock.start = AsyncMock(side_effect=RuntimeError("No VMs"))
        MockSession.return_value = mock
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=20, session_ttl=600,
        )
        with pytest.raises(RuntimeError):
            await mgr.create()
        assert len(mgr.sessions) == 0
        mock.stop.assert_awaited_once()

    @patch("execution_api.server.SandboxSession")
    async def test_create_enforces_max_sessions_atomically(self, MockSession):
        MockSession.return_value = AsyncMock()
        mgr = SessionManager(
            gateway_url="http://test:8888", default_timeout=30,
            max_sessions=2, session_ttl=600,
        )
        await mgr.create()
        await mgr.create()
        with pytest.raises(RuntimeError, match="max sessions reached"):
            await mgr.create()
        assert len(mgr.sessions) == 2


class TestDashboardEndpoints:
    async def test_launch_dashboard_success(self, client):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=True, stdout="dashboard deployed\n", stderr="", error=None, outputs=[], execution_count=1,
        ))
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.post(f"/sessions/{sid}/dashboard", json={"code": "app = 1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == sid
        assert "/dash/" in data["url"]
        assert "test-kernel-id" in data["url"]
        mock.execute.assert_awaited()

    async def test_launch_dashboard_session_not_found(self, client):
        c, _ = client
        resp = await c.post("/sessions/missing/dashboard", json={"code": "x"})
        assert resp.status_code == 404

    async def test_launch_dashboard_no_kernel(self, client):
        c, mock = client
        sid = (await c.post("/sessions")).json()["session_id"]
        mgr = c._transport.app.state.session_manager
        mgr._sessions[sid].session._kernel_id = None
        resp = await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        assert resp.status_code == 503

    async def test_launch_dashboard_execute_error(self, client):
        c, mock = client
        mock.execute = AsyncMock(side_effect=RuntimeError("kernel dead"))
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        assert resp.status_code == 503

    async def test_launch_dashboard_replaces_existing(self, client):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=True, stdout="ok\n", stderr="", error=None, outputs=[], execution_count=1,
        ))
        sid = (await c.post("/sessions")).json()["session_id"]
        r1 = await c.post(f"/sessions/{sid}/dashboard", json={"code": "v1"})
        r2 = await c.post(f"/sessions/{sid}/dashboard", json={"code": "v2"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["app_id"] != r2.json()["app_id"]

    async def test_stop_dashboard_success(self, client):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=True, stdout="ok\n", stderr="", error=None, outputs=[], execution_count=1,
        ))
        sid = (await c.post("/sessions")).json()["session_id"]
        await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        resp = await c.delete(f"/sessions/{sid}/dashboard")
        assert resp.status_code == 200

    async def test_stop_dashboard_session_not_found(self, client):
        c, _ = client
        resp = await c.delete("/sessions/missing/dashboard")
        assert resp.status_code == 404

    async def test_delete_session_clears_dashboard(self, client):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=True, stdout="ok\n", stderr="", error=None, outputs=[], execution_count=1,
        ))
        sid = (await c.post("/sessions")).json()["session_id"]
        await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        resp = await c.delete(f"/sessions/{sid}")
        assert resp.status_code == 200


class TestFileEndpoints:
    async def test_upload_file_success(self, client, monkeypatch):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=True, stdout="", stderr="", error=None, outputs=[], execution_count=1,
        ))
        sid = (await c.post("/sessions")).json()["session_id"]
        monkeypatch.setattr("execution_api.server.UPLOAD_CHUNK_SIZE", 2)

        resp = await c.post(
            f"/sessions/{sid}/files",
            files={"file": ("test.csv", b"abcdef", "text/csv")},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == "/data/test.csv"
        assert data["filename"] == "test.csv"
        assert data["size"] == 6
        assert mock.execute.await_count == 4
        first_code = mock.execute.await_args_list[0].args[0]
        second_code = mock.execute.await_args_list[1].args[0]
        rename_code = mock.execute.await_args_list[3].args[0]
        assert "with open('/data/test.csv.tmp', 'wb')" in first_code
        assert "with open('/data/test.csv.tmp', 'ab')" in second_code
        assert "os.replace('/data/test.csv.tmp', '/data/test.csv')" in rename_code

    async def test_upload_zero_byte_file(self, client):
        c, mock = client

        async def _exec_side_effect(code: str):
            if "open('/data/empty.txt', 'wb').close()" in code:
                return ExecutionResult(
                    success=True, stdout="", stderr="", error=None, outputs=[], execution_count=1,
                )
            if "os.path.isdir('/data')" in code:
                return ExecutionResult(
                    success=True,
                    stdout='[{"filename":"empty.txt","path":"/data/empty.txt","size":0}]\n',
                    stderr="",
                    error=None,
                    outputs=[],
                    execution_count=2,
                )
            return ExecutionResult(
                success=True, stdout="", stderr="", error=None, outputs=[], execution_count=3,
            )

        mock.execute = AsyncMock(side_effect=_exec_side_effect)
        sid = (await c.post("/sessions")).json()["session_id"]

        upload_resp = await c.post(
            f"/sessions/{sid}/files",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert upload_resp.status_code == 200
        assert upload_resp.json()["size"] == 0

        list_resp = await c.get(f"/sessions/{sid}/files")
        assert list_resp.status_code == 200
        assert list_resp.json()["files"][0]["filename"] == "empty.txt"

    async def test_upload_file_session_not_found(self, client):
        c, _ = client
        resp = await c.post(
            "/sessions/missing/files",
            files={"file": ("test.csv", b"x", "text/csv")},
        )
        assert resp.status_code == 404

    async def test_upload_file_too_large(self, client, monkeypatch):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        monkeypatch.setattr("execution_api.server.UPLOAD_MAX_BYTES", 4)
        resp = await c.post(
            f"/sessions/{sid}/files",
            files={"file": ("big.bin", b"12345", "application/octet-stream")},
        )
        assert resp.status_code == 413

    async def test_upload_file_unsafe_filename(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.post(
            f"/sessions/{sid}/files",
            files={"file": ("../etc/passwd", b"x", "text/plain")},
        )
        assert resp.status_code == 422

    async def test_upload_no_path_parameter(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.post(
            f"/sessions/{sid}/files",
            files={"file": ("test.csv", b"x", "text/csv")},
        )
        assert resp.status_code == 200
        assert resp.json()["path"] == "/data/test.csv"

    async def test_list_files_success(self, client):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=True,
            stdout='[{"filename":"a.csv","path":"/data/a.csv","size":10}]\n',
            stderr="",
            error=None,
            outputs=[],
            execution_count=1,
        ))
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.get(f"/sessions/{sid}/files")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["files"]) == 1
        assert data["files"][0]["filename"] == "a.csv"
        assert mock.execute.await_args is not None
        code = mock.execute.await_args.args[0]
        assert "os.listdir('/data/')" in code

    async def test_delete_file_success(self, client):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=True, stdout='{"ok": true}\n', stderr="", error=None, outputs=[], execution_count=1,
        ))
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.delete(f"/sessions/{sid}/files/test.csv")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert mock.execute.await_args is not None
        code = mock.execute.await_args.args[0]
        assert "path = '/data/test.csv'" in code
        assert "os.remove(path)" in code

    async def test_delete_file_unsafe_filename(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.delete(f"/sessions/{sid}/files/bad$name")
        assert resp.status_code == 422

    async def test_delete_nonexistent_file(self, client):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=True,
            stdout='{"error":"not_found"}\n',
            stderr="",
            error=None,
            outputs=[],
            execution_count=1,
        ))
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.delete(f"/sessions/{sid}/files/missing.csv")
        assert resp.status_code == 404
        assert resp.json()["error"] == "file not found"

    async def test_one_shot_malformed_json(self, client):
        c, _ = client
        resp = await c.post(
            "/execute",
            content=b"{not valid json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"] == "invalid JSON body"

    async def test_one_shot_missing_code(self, client):
        c, _ = client
        resp = await c.post("/execute", json={})
        assert resp.status_code == 422

    async def test_list_files_empty_session(self, client):
        c, mock = client
        mock.execute = AsyncMock(return_value=ExecutionResult(
            success=True,
            stdout="[]\n",
            stderr="",
            error=None,
            outputs=[],
            execution_count=1,
        ))
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.get(f"/sessions/{sid}/files")
        assert resp.status_code == 200
        assert resp.json()["files"] == []

    async def test_one_shot_execute_supports_multipart_files(self, client, monkeypatch):
        c, mock = client

        async def _exec_side_effect(code: str):
            if "with open('/data/input.csv.tmp', 'wb')" in code:
                return ExecutionResult(
                    success=True, stdout="", stderr="", error=None, outputs=[], execution_count=1,
                )
            if "os.replace('/data/input.csv.tmp', '/data/input.csv')" in code:
                return ExecutionResult(
                    success=True, stdout="", stderr="", error=None, outputs=[], execution_count=2,
                )
            return ExecutionResult(
                success=True, stdout="done\n", stderr="", error=None, outputs=[], execution_count=3,
            )

        mock.execute = AsyncMock(side_effect=_exec_side_effect)
        monkeypatch.setattr("execution_api.server.UPLOAD_CHUNK_SIZE", 1024)
        resp = await c.post(
            "/execute",
            files={
                "code": (None, "print('done')"),
                "file": ("input.csv", b"a,b\n1,2\n", "text/csv"),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["stdout"] == "done\n"
        assert mock.execute.await_count == 3
