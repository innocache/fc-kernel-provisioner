# Dashboard Serving Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add session-scoped dashboard serving to the Execution API so an LLM can launch/replace/stop Panel dashboards running inside the same Firecracker VM as the kernel, with browser access through dynamic Caddy routes.

**Architecture:** `guest/fc_guest_agent.py` gains dashboard process lifecycle actions (`launch_dashboard`, `stop_dashboard`). `execution_api` adds dashboard request/response models, a `CaddyClient`, VM lookup plumbing, and `POST/DELETE /sessions/{id}/dashboard` endpoints. VM lookup is deterministic through pool-manager kernel binding (`kernel_id -> vm_id`), populated by the provisioner at acquire time. Integration tests validate end-to-end API → vsock guest agent → Caddy → browser route behavior.

**Tech Stack:** Python 3.11+, FastAPI, aiohttp, Pydantic v2, Caddy admin API, Firecracker vsock UDS messaging, pytest/pytest-asyncio/httpx.

**Spec:** `docs/superpowers/specs/2026-03-23-dashboard-serving-design.md`

**Status:** All tasks complete. 476 unit + 27 integration tests passing.

| Chunk | Status |
|-------|--------|
| 1: Pydantic Models + Caddy Client (Tasks 1-2) | DONE |
| 2: Guest Agent Extension (Task 3) | DONE |
| 3: VM Info Lookup (Task 4) | DONE |
| 4: Execution API Dashboard Endpoints (Tasks 5-6) | DONE |
| 5: Schemas + Config + Integration Tests (Tasks 7-8) | DONE |

---

## File Map

| File | Responsibility | Dependencies |
|------|---------------|--------------|
| `execution_api/models.py` | Add dashboard request/response models | pydantic |
| `execution_api/caddy_client.py` | Caddy route management + shared `_vsock_request` helper | aiohttp, asyncio, json, struct |
| `execution_api/server.py` | Session VM metadata lookup, dashboard lifecycle endpoints, cleanup hooks | fastapi, `sandbox_client`, `execution_api.models`, `execution_api.caddy_client` |
| `guest/fc_guest_agent.py` | Start/stop Panel process in VM; app file lifecycle | subprocess, socket, os |
| `fc_pool_manager/server.py` | VM bind/lookup HTTP endpoints over Unix socket | aiohttp, `PoolManager` |
| `fc_pool_manager/manager.py` | Kernel→VM index lifecycle | internal pool state |
| `fc_provisioner/pool_client.py` | Bind API call helper | aiohttp |
| `fc_provisioner/provisioner.py` | Bind kernel_id to acquired VM after `acquire()` | PoolClient |
| `execution_api/tool_schemas/claude.json` | Add `launch_dashboard` tool schema | None |
| `execution_api/tool_schemas/openai.json` | Add `launch_dashboard` tool schema | None |
| `config/Caddyfile` | Base Caddy route + admin API config | Caddy |
| `scripts/remote-test.sh` | Start Caddy during remote integration run | bash/system services |
| `tests/test_guest_agent.py` | Guest agent dashboard unit tests | pytest, unittest.mock |
| `tests/test_caddy_client.py` | Caddy client unit tests | pytest, aiohttp mocks |
| `tests/test_pool_client.py` | Pool client bind API tests | pytest |
| `tests/test_provisioner.py` | Provisioner bind behavior tests | pytest, AsyncMock |
| `tests/test_execution_api.py` | Dashboard API and session cleanup tests | pytest, httpx, AsyncMock |
| `tests/test_pool_server.py` | Pool server bind/lookup endpoint tests | pytest-aiohttp |
| `tests/test_integration.py` | Dashboard E2E tests | aiohttp |

---

## Chunk 1: Pydantic Models + Caddy Client

### Task 1: Add `DashboardRequest`/`DashboardResponse` models and model tests

**Files:**
- Modify: `execution_api/models.py`
- Modify: `tests/test_execution_api.py`

- [ ] **Step 1: Write failing model tests first**

Add to `tests/test_execution_api.py`:

```python
from execution_api.models import DashboardRequest, DashboardResponse


class TestDashboardModels:
    def test_dashboard_request(self):
        req = DashboardRequest(code="import panel as pn")
        assert req.code == "import panel as pn"

    def test_dashboard_response(self):
        resp = DashboardResponse(
            url="/dash/sess123/dash_abcd",
            session_id="sess123",
            app_id="abcd",
        )
        data = resp.model_dump(mode="json")
        assert data["url"] == "/dash/sess123/dash_abcd"
        assert data["session_id"] == "sess123"
        assert data["app_id"] == "abcd"
```

- [ ] **Step 2: Run tests to confirm failure**

Run:
```bash
uv run pytest tests/test_execution_api.py::TestDashboardModels -v
```

Expected: FAIL (`ImportError` for missing `DashboardRequest`/`DashboardResponse`).

- [ ] **Step 3: Implement models**

Update `execution_api/models.py`:

```python
class DashboardRequest(BaseModel):
    code: str


class DashboardResponse(BaseModel):
    url: str
    session_id: str
    app_id: str
```

- [ ] **Step 4: Re-run model tests**

Run:
```bash
uv run pytest tests/test_execution_api.py::TestDashboardModels -v
```

- [ ] **Step 5: Commit**

```bash
git add execution_api/models.py tests/test_execution_api.py
git commit -m "feat(api): add dashboard request and response models"
```

---

### Task 2: Add `CaddyClient` module and comprehensive unit tests

**Files:**
- Create: `execution_api/caddy_client.py`
- Create: `tests/test_caddy_client.py`

- [ ] **Step 1: Write failing Caddy client tests first**

Create `tests/test_caddy_client.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution_api.caddy_client import CaddyClient


def _mock_response(status: int, text: str = ""):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    return resp


class TestCaddyClient:
    @pytest.fixture
    def client(self):
        return CaddyClient(admin_url="http://localhost:2019")

    async def test_add_route_put_success(self, client):
        put_resp = _mock_response(200)
        session = AsyncMock()
        session.put = AsyncMock(return_value=put_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            await client.add_route("sess1", "172.16.0.2:5006")
        session.put.assert_awaited_once()

    async def test_add_route_fallback_to_post_on_404(self, client):
        put_resp = _mock_response(404)
        post_resp = _mock_response(201)
        session = AsyncMock()
        session.put = AsyncMock(return_value=put_resp)
        session.post = AsyncMock(return_value=post_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            await client.add_route("sess1", "172.16.0.2:5006")
        session.put.assert_awaited_once()
        session.post.assert_awaited_once()

    async def test_add_route_server_error_raises(self, client):
        put_resp = _mock_response(500, "boom")
        session = AsyncMock()
        session.put = AsyncMock(return_value=put_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            with pytest.raises(RuntimeError, match="Caddy add_route failed"):
                await client.add_route("sess1", "172.16.0.2:5006")

    async def test_remove_route_success(self, client):
        del_resp = _mock_response(204)
        session = AsyncMock()
        session.delete = AsyncMock(return_value=del_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            await client.remove_route("sess1")
        session.delete.assert_awaited_once()

    async def test_remove_route_404_is_ok(self, client):
        del_resp = _mock_response(404)
        session = AsyncMock()
        session.delete = AsyncMock(return_value=del_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            await client.remove_route("sess1")

    async def test_remove_route_server_error_raises(self, client):
        del_resp = _mock_response(500, "boom")
        session = AsyncMock()
        session.delete = AsyncMock(return_value=del_resp)
        session.__aenter__.return_value = session
        with patch("execution_api.caddy_client.aiohttp.ClientSession", return_value=session):
            with pytest.raises(RuntimeError, match="Caddy remove_route failed"):
                await client.remove_route("sess1")

    def test_route_id_format(self, client):
        assert client._route_id("abc") == "dashboard_abc"

    def test_build_route_structure(self, client):
        route = client._build_route("sess1", "172.16.0.2:5006")
        assert route["@id"] == "dashboard_sess1"
        assert route["match"] == [{"path": ["/dash/sess1/*"]}]
        rp = route["handle"][0]
        assert rp["handler"] == "reverse_proxy"
        assert rp["upstreams"] == [{"dial": "172.16.0.2:5006"}]
        assert rp["flush_interval"] == -1
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_caddy_client.py -v
```

Expected: FAIL (`ModuleNotFoundError: execution_api.caddy_client`).

- [ ] **Step 3: Implement `execution_api/caddy_client.py`**

```python
"""CaddyClient — manage dashboard routes and vsock messages."""

import asyncio
import json
import struct
from typing import Any

import aiohttp

HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
GUEST_AGENT_PORT = 52


class CaddyClient:
    def __init__(self, admin_url: str = "http://localhost:2019"):
        self._admin_url = admin_url.rstrip("/")

    def _route_id(self, session_id: str) -> str:
        return f"dashboard_{session_id}"

    def _build_route(self, session_id: str, upstream: str) -> dict[str, Any]:
        return {
            "@id": self._route_id(session_id),
            "match": [{"path": [f"/dash/{session_id}/*"]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": upstream}],
                    "flush_interval": -1,
                }
            ],
        }

    async def add_route(self, session_id: str, upstream: str) -> None:
        route = self._build_route(session_id, upstream)
        route_id = self._route_id(session_id)
        put_url = f"{self._admin_url}/id/{route_id}"
        add_url = f"{self._admin_url}/config/apps/http/servers/main/routes/0"

        async with aiohttp.ClientSession() as http:
            resp = await http.put(put_url, json=route)
            if resp.status == 404:
                resp = await http.post(add_url, json=route)
            if resp.status not in (200, 201):
                body = await resp.text()
                raise RuntimeError(f"Caddy add_route failed ({resp.status}): {body}")

    async def remove_route(self, session_id: str) -> None:
        route_id = self._route_id(session_id)
        del_url = f"{self._admin_url}/id/{route_id}"
        async with aiohttp.ClientSession() as http:
            resp = await http.delete(del_url)
            if resp.status not in (200, 204, 404):
                body = await resp.text()
                raise RuntimeError(f"Caddy remove_route failed ({resp.status}): {body}")


async def _vsock_request(vsock_uds_path: str, msg: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(vsock_uds_path)
    try:
        writer.write(f"CONNECT {GUEST_AGENT_PORT}\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line.startswith(b"OK"):
            raise ConnectionError(f"vsock handshake failed: {line.decode().strip()}")

        payload = json.dumps(msg).encode()
        writer.write(struct.pack(HEADER_FMT, len(payload)) + payload)
        await writer.drain()

        header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=timeout)
        length = struct.unpack(HEADER_FMT, header)[0]
        body = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(body)
    finally:
        writer.close()
        await writer.wait_closed()
```

- [ ] **Step 4: Re-run tests**

```bash
uv run pytest tests/test_caddy_client.py -v
```

- [ ] **Step 5: Commit**

```bash
git add execution_api/caddy_client.py tests/test_caddy_client.py
git commit -m "feat(api): add caddy client and route management tests"
```

---

## Chunk 2: Guest Agent Extension

### Task 3: Implement dashboard process controls in guest agent + tests

**Files:**
- Modify: `guest/fc_guest_agent.py`
- Modify: `tests/test_guest_agent.py`

- [ ] **Step 1: Add failing tests for dashboard lifecycle**

Add these tests to `tests/test_guest_agent.py` (complete bodies):

```python
class TestDashboardLifecycle:
    def _get_fresh_mod(self):
        mod = load_agent_module()
        mod.kernel_proc = None
        mod.panel_proc = None
        return mod

    def test_launch_dashboard_writes_file_and_starts_panel(self, tmp_path):
        mod = self._get_fresh_mod()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch.object(mod, "_APPS_DIR", str(tmp_path)), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(mod, "wait_for_kernel_ports"):
            mod.start_dashboard("print('x')", 5006, "app1", "sess1")
        assert (tmp_path / "dash_app1.py").exists()

    def test_launch_dashboard_kills_existing_panel_first(self, tmp_path):
        mod = self._get_fresh_mod()
        existing = MagicMock()
        existing.poll.return_value = None
        mod.panel_proc = existing
        new_proc = MagicMock()
        new_proc.poll.return_value = None
        with patch.object(mod, "_APPS_DIR", str(tmp_path)), \
             patch.object(mod, "_kill_proc") as kill_proc, \
             patch("subprocess.Popen", return_value=new_proc), \
             patch.object(mod, "wait_for_kernel_ports"):
            mod.start_dashboard("print('x')", 5006, "app1", "sess1")
        kill_proc.assert_called_once_with(existing)

    def test_launch_dashboard_port_timeout_returns_error(self):
        mod = self._get_fresh_mod()
        msg = {
            "action": "launch_dashboard",
            "code": "import panel as pn",
            "port": 5006,
            "app_id": "app1",
            "session_id": "sess1",
        }
        with patch.object(mod, "start_dashboard", side_effect=RuntimeError("panel serve did not start: timeout")):
            response = _decode(mod.handle_message(_encode(msg)))
        assert response["status"] == "error"

    def test_launch_dashboard_process_exits_early_returns_error(self):
        mod = self._get_fresh_mod()
        msg = {
            "action": "launch_dashboard",
            "code": "import panel as pn",
            "port": 5006,
            "app_id": "app1",
            "session_id": "sess1",
        }
        with patch.object(mod, "start_dashboard", side_effect=RuntimeError("panel exited immediately")):
            response = _decode(mod.handle_message(_encode(msg)))
        assert response["status"] == "error"

    def test_launch_dashboard_missing_code_returns_error(self):
        mod = self._get_fresh_mod()
        response = _decode(mod.handle_message(_encode({"action": "launch_dashboard", "session_id": "s1"})))
        assert response["status"] == "error"

    def test_stop_dashboard_kills_process(self):
        mod = self._get_fresh_mod()
        proc = MagicMock()
        proc.poll.return_value = None
        mod.panel_proc = proc
        with patch.object(mod, "_kill_proc") as kill_proc:
            mod.stop_dashboard()
        kill_proc.assert_called_once_with(proc)
        assert mod.panel_proc is None

    def test_stop_dashboard_idempotent_when_no_panel(self):
        mod = self._get_fresh_mod()
        mod.panel_proc = None
        mod.stop_dashboard()
        assert mod.panel_proc is None

    def test_stop_dashboard_cleans_app_files(self, tmp_path):
        mod = self._get_fresh_mod()
        (tmp_path / "dash_a.py").write_text("x")
        (tmp_path / "dash_b.py").write_text("x")
        with patch.object(mod, "_APPS_DIR", str(tmp_path)):
            mod.stop_dashboard()
        assert not (tmp_path / "dash_a.py").exists()
        assert not (tmp_path / "dash_b.py").exists()

    def test_kill_proc_sigterm_then_sigkill(self):
        mod = self._get_fresh_mod()
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="x", timeout=1), None]
        mod._kill_proc(proc, timeout=0.01)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_guest_agent.py -v
```

- [ ] **Step 3: Implement guest agent dashboard functions and action branches**

Add to `guest/fc_guest_agent.py`:

```python
import glob

panel_proc = None
_APPS_DIR = "/apps"


def _kill_proc(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def start_dashboard(code: str, port: int, app_id: str, session_id: str) -> None:
    global panel_proc
    if not code.strip():
        raise RuntimeError("dashboard code is required")

    os.makedirs(_APPS_DIR, exist_ok=True)
    app_path = os.path.join(_APPS_DIR, f"dash_{app_id}.py")
    with open(app_path, "w") as fh:
        fh.write(code)

    if panel_proc is not None and panel_proc.poll() is None:
        _kill_proc(panel_proc)
    panel_proc = None

    python = sys.executable or "/usr/bin/python3"
    panel_proc = subprocess.Popen(
        [
            python, "-m", "panel", "serve", app_path,
            "--port", str(port),
            "--address", "0.0.0.0",
            "--allow-websocket-origin", "*",
            "--prefix", f"/dash/{session_id}",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    time.sleep(0.2)
    if panel_proc.poll() is not None:
        raise RuntimeError(f"panel exited immediately with code {panel_proc.poll()}")

    try:
        wait_for_kernel_ports("127.0.0.1", {"panel": port}, timeout=10.0)
    except Exception as exc:
        _kill_proc(panel_proc)
        panel_proc = None
        raise RuntimeError(f"panel serve did not start: {exc}") from exc


def stop_dashboard() -> None:
    global panel_proc
    if panel_proc is not None:
        _kill_proc(panel_proc)
        panel_proc = None

    if os.path.isdir(_APPS_DIR):
        for app_path in glob.glob(os.path.join(_APPS_DIR, "dash_*.py")):
            try:
                os.remove(app_path)
            except OSError:
                pass
```

Update `handle_message()` branches:

```python
    elif action == "launch_dashboard":
        code = msg.get("code", "")
        port = msg.get("port", 5006)
        app_id = msg.get("app_id", "")
        session_id = msg.get("session_id", "")
        try:
            start_dashboard(code, port, app_id, session_id)
            return _encode_response({"status": "ok", "app_id": app_id, "port": port})
        except Exception as exc:
            return _encode_response({"status": "error", "message": str(exc)})

    elif action == "stop_dashboard":
        try:
            stop_dashboard()
            return _encode_response({"status": "ok"})
        except Exception as exc:
            return _encode_response({"status": "error", "message": str(exc)})
```

- [ ] **Step 4: Re-run guest-agent tests**

```bash
uv run pytest tests/test_guest_agent.py -v
```

- [ ] **Step 5: Commit**

```bash
git add guest/fc_guest_agent.py tests/test_guest_agent.py
git commit -m "feat(guest): add dashboard launch and stop actions"
```

---

## Chunk 3: VM Info Lookup

### Task 4: Implement deterministic VM lookup (`kernel_id -> vm_id -> ip/vsock_path`) + tests

**Files:**
- Modify: `fc_pool_manager/manager.py`
- Modify: `fc_pool_manager/server.py`
- Modify: `fc_provisioner/pool_client.py`
- Modify: `fc_provisioner/provisioner.py`
- Modify: `tests/test_pool_client.py`
- Modify: `tests/test_provisioner.py`
- Create: `tests/test_pool_server.py`

- [ ] **Step 1: Add failing tests for bind/lookup flow**

Create `tests/test_pool_server.py`:

```python
import pytest
from aiohttp import web

from fc_pool_manager.server import create_app
from fc_pool_manager.vm import VMInstance, VMState


class DummyManager:
    def __init__(self):
        self._vms = {}
        self._kernel_to_vm = {}


@pytest.fixture
async def client(aiohttp_client):
    m = DummyManager()
    vm = VMInstance(
        vm_id="vm-1", short_id="1", ip="172.16.0.2", cid=3,
        tap_name="tap-1", mac="aa:bb:cc:dd:ee:ff",
        jail_path="/tmp", vsock_path="/tmp/v.sock",
    )
    vm.state = VMState.ASSIGNED
    m._vms[vm.vm_id] = vm
    app = create_app(m)
    return await aiohttp_client(app)


class TestPoolServerLookup:
    async def test_bind_then_lookup_success(self, client):
        resp = await client.post("/api/vms/vm-1/bind-kernel", json={"kernel_id": "kid1"})
        assert resp.status == 200
        resp = await client.get("/api/vms/by-kernel/kid1")
        assert resp.status == 200
        data = await resp.json()
        assert data["vm_id"] == "vm-1"
        assert data["ip"] == "172.16.0.2"

    async def test_lookup_unknown_kernel_404(self, client):
        resp = await client.get("/api/vms/by-kernel/missing")
        assert resp.status == 404

    async def test_bind_missing_kernel_id_400(self, client):
        resp = await client.post("/api/vms/vm-1/bind-kernel", json={})
        assert resp.status == 400
```

Add to `tests/test_pool_client.py`:

```python
@pytest.mark.asyncio
async def test_bind_kernel_calls_expected_endpoint(monkeypatch):
    client = PoolClient(socket_path="/tmp/test.sock")
    session = MagicMock()
    response = MagicMock()
    response.status = 200
    response.raise_for_status.return_value = None
    session.post = AsyncMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("fc_provisioner.pool_client.aiohttp.ClientSession", lambda **kwargs: session)
    await client.bind_kernel("vm-1", "kid1")
    session.post.assert_awaited_once_with("http://localhost/api/vms/vm-1/bind-kernel", json={"kernel_id": "kid1"})
```

Add to `tests/test_provisioner.py`:

```python
@patch("fc_provisioner.provisioner.PoolClient")
async def test_pre_launch_binds_kernel_id(self, MockPoolClient, provisioner):
    mock_client = AsyncMock()
    mock_client.acquire = AsyncMock(return_value={
        "id": "vm-abc12345",
        "ip": "172.16.0.2",
        "vsock_path": "/srv/jailer/firecracker/vm-abc12345/root/v.sock",
    })
    mock_client.bind_kernel = AsyncMock()
    MockPoolClient.return_value = mock_client
    with patch.object(FirecrackerProvisioner.__bases__[0], "pre_launch", AsyncMock(return_value={})):
        await provisioner.pre_launch()
    mock_client.bind_kernel.assert_awaited_once_with("vm-abc12345", provisioner.kernel_id)
```

- [ ] **Step 2: Run failing tests**

```bash
uv run pytest tests/test_pool_server.py tests/test_pool_client.py tests/test_provisioner.py -v
```

- [ ] **Step 3: Implement pool manager bind/lookup and provisioner bind call**

`fc_pool_manager/manager.py` additions:

```python
class PoolManager:
    def __init__(self, config: PoolConfig):
        self._config = config
        self._vms: dict[str, VMInstance] = {}
        self._network = NetworkManager(
            bridge=config.bridge,
            gateway=config.gateway,
            vm_ip_start=config.vm_ip_start,
        )
        self._cid_alloc = CIDAllocator()
        self._boot_lock = asyncio.Lock()
        self._acquire_lock = asyncio.Lock()
        self._kernel_to_vm: dict[str, str] = {}
        POOL_MAX_VMS.set(config.max_vms)

    def clear_kernel_bindings_for_vm(self, vm_id: str) -> None:
        stale = [kid for kid, mapped_vm in self._kernel_to_vm.items() if mapped_vm == vm_id]
        for kid in stale:
            self._kernel_to_vm.pop(kid, None)

    def clear_all_kernel_bindings(self) -> None:
        self._kernel_to_vm.clear()

    def bind_kernel(self, vm_id: str, kernel_id: str) -> None:
        self._kernel_to_vm[kernel_id] = vm_id

    def vm_by_kernel(self, kernel_id: str) -> dict[str, Any] | None:
        vm_id = self._kernel_to_vm.get(kernel_id)
        if vm_id is None:
            return None
        vm = self._vms.get(vm_id)
        if vm is None or vm.state != VMState.ASSIGNED:
            return None
        return {"vm_id": vm.vm_id, "ip": vm.ip, "vsock_path": vm.vsock_path}
```

Call `self.clear_kernel_bindings_for_vm(vm_id)` at the end of `release()` and call `self.clear_all_kernel_bindings()` at the end of `shutdown()`.

`fc_pool_manager/server.py` additions:

```python
app.router.add_post("/api/vms/{vm_id}/bind-kernel", handle_bind_kernel)
app.router.add_get("/api/vms/by-kernel/{kernel_id}", handle_vm_by_kernel)


async def handle_bind_kernel(request: web.Request) -> web.Response:
    manager: PoolManager = request.app["manager"]
    vm_id = request.match_info["vm_id"]
    body = await request.json()
    kernel_id = body.get("kernel_id")
    if not kernel_id:
        return web.json_response({"error": "kernel_id required"}, status=400)
    if vm_id not in manager._vms:
        return web.json_response({"error": "VM not found"}, status=404)
    manager.bind_kernel(vm_id, kernel_id)
    return web.json_response({"ok": True})


async def handle_vm_by_kernel(request: web.Request) -> web.Response:
    manager: PoolManager = request.app["manager"]
    kernel_id = request.match_info["kernel_id"]
    data = manager.vm_by_kernel(kernel_id)
    if data is None:
        return web.json_response({"error": "VM not found for kernel"}, status=404)
    return web.json_response(data)
```

`fc_provisioner/pool_client.py` addition:

```python
    async def bind_kernel(self, vm_id: str, kernel_id: str) -> None:
        async with aiohttp.ClientSession(connector=self._connector()) as session:
            resp = await session.post(
                f"{self._base_url}/api/vms/{vm_id}/bind-kernel",
                json={"kernel_id": kernel_id},
            )
            resp.raise_for_status()
```

`fc_provisioner/provisioner.py` addition in `pre_launch` after acquire:

```python
        if self.vm_id and getattr(self, "kernel_id", None):
            await self.pool_client.bind_kernel(self.vm_id, self.kernel_id)
```

- [ ] **Step 4: Re-run tests**

```bash
uv run pytest tests/test_pool_server.py tests/test_pool_client.py tests/test_provisioner.py -v
```

- [ ] **Step 5: Commit**

```bash
git add fc_pool_manager/manager.py fc_pool_manager/server.py fc_provisioner/pool_client.py fc_provisioner/provisioner.py tests/test_pool_server.py tests/test_pool_client.py tests/test_provisioner.py
git commit -m "feat(pool): add kernel-to-vm bind and lookup endpoints"
```

---

## Chunk 4: Execution API Dashboard Endpoints

### Task 5: Extend `SessionEntry` + `SessionManager` with VM metadata and dashboard state

**Files:**
- Modify: `execution_api/server.py`
- Modify: `tests/test_execution_api.py`

- [ ] **Step 1: Add failing session-manager tests**

Add tests:

```python
class TestSessionManagerDashboardState:
    @patch("execution_api.server.SandboxSession")
    @patch("execution_api.server._lookup_vm_by_kernel")
    async def test_create_populates_vm_info(self, mock_lookup, MockSession):
        session = AsyncMock()
        session._kernel_id = "kid-1"
        MockSession.return_value = session
        mock_lookup.return_value = {"ip": "172.16.0.2", "vsock_path": "/tmp/v.sock"}
        mgr = SessionManager(gateway_url="http://gw:8888")
        entry = await mgr.create()
        assert entry.vm_ip == "172.16.0.2"
        assert entry.vsock_path == "/tmp/v.sock"
        assert entry.active_dashboard is None

    @patch("execution_api.server.SandboxSession")
    @patch("execution_api.server._lookup_vm_by_kernel", side_effect=RuntimeError("down"))
    async def test_create_vm_lookup_failure_keeps_session_usable(self, _, MockSession):
        session = AsyncMock()
        session._kernel_id = "kid-1"
        MockSession.return_value = session
        mgr = SessionManager(gateway_url="http://gw:8888")
        entry = await mgr.create()
        assert entry.vm_ip is None
        assert entry.vsock_path is None
```

- [ ] **Step 2: Run failing tests**

```bash
uv run pytest tests/test_execution_api.py::TestSessionManagerDashboardState -v
```

- [ ] **Step 3: Implement session metadata and lookup helper**

Add to `execution_api/server.py`:

```python
import aiohttp
from dataclasses import dataclass, field

POOL_SOCKET = os.environ.get("POOL_SOCKET", "/var/run/fc-pool.sock")


async def _lookup_vm_by_kernel(pool_socket: str, kernel_id: str | None) -> dict[str, str]:
    if not kernel_id:
        raise RuntimeError("kernel_id unavailable")
    conn = aiohttp.UnixConnector(path=pool_socket)
    async with aiohttp.ClientSession(connector=conn) as http:
        resp = await http.get(f"http://localhost/api/vms/by-kernel/{kernel_id}")
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"vm lookup failed ({resp.status}): {body}")
        return await resp.json()


@dataclass
class SessionEntry:
    session: SandboxSession
    session_id: str
    created_at: float
    last_active: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    vm_ip: str | None = None
    vsock_path: str | None = None
    active_dashboard: str | None = None
```

Inside `SessionManager.create()` after `await session.start()` and before storing entry:

```python
            vm_ip = None
            vsock_path = None
            try:
                vm_info = await _lookup_vm_by_kernel(POOL_SOCKET, getattr(session, "_kernel_id", None))
                vm_ip = vm_info.get("ip")
                vsock_path = vm_info.get("vsock_path")
            except Exception:
                logger.debug("VM lookup failed after session start", exc_info=True)
```

And set on the `SessionEntry` constructor call:

```python
                vm_ip=vm_ip,
                vsock_path=vsock_path,
                active_dashboard=None,
```

- [ ] **Step 4: Re-run tests**

```bash
uv run pytest tests/test_execution_api.py::TestSessionManagerDashboardState -v
```

- [ ] **Step 5: Commit**

```bash
git add execution_api/server.py tests/test_execution_api.py
git commit -m "feat(api): persist vm metadata for dashboard operations"
```

---

### Task 6: Add dashboard endpoints + cleanup on session delete with comprehensive tests

**Files:**
- Modify: `execution_api/server.py`
- Modify: `tests/test_execution_api.py`

- [ ] **Step 1: Add failing endpoint tests**

Add these tests to `tests/test_execution_api.py`:

```python
class TestDashboardEndpoints:
    async def test_launch_dashboard_success(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.post(f"/sessions/{sid}/dashboard", json={"code": "import panel as pn"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == sid
        assert data["url"].startswith(f"/dash/{sid}/")

    async def test_launch_dashboard_session_not_found(self, client):
        c, _ = client
        resp = await c.post("/sessions/missing/dashboard", json={"code": "x"})
        assert resp.status_code == 404

    async def test_launch_dashboard_no_vm_info(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        # force vm info missing via manager mutation in test fixture
        app_mgr = c._transport.app.state.session_manager
        app_mgr.sessions[sid].vsock_path = None
        resp = await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        assert resp.status_code == 503

    async def test_launch_dashboard_vsock_error(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        with patch("execution_api.server._vsock_request", side_effect=RuntimeError("down")):
            resp = await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        assert resp.status_code == 503

    async def test_launch_dashboard_panel_start_error(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        with patch("execution_api.server._vsock_request", return_value={"status": "error", "message": "bad code"}):
            resp = await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        assert resp.status_code == 500

    async def test_launch_dashboard_caddy_error_triggers_cleanup(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        with patch("execution_api.server._vsock_request", side_effect=[{"status": "ok"}, {"status": "ok"}]) as vsock, \
             patch("execution_api.server.caddy.add_route", side_effect=RuntimeError("caddy down")):
            resp = await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        assert resp.status_code == 503
        assert vsock.await_count == 2

    async def test_launch_dashboard_replaces_existing(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        resp = await c.post(f"/sessions/{sid}/dashboard", json={"code": "y"})
        assert resp.status_code == 200

    async def test_stop_dashboard_success(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        resp = await c.delete(f"/sessions/{sid}/dashboard")
        assert resp.status_code == 200

    async def test_stop_dashboard_session_not_found(self, client):
        c, _ = client
        resp = await c.delete("/sessions/missing/dashboard")
        assert resp.status_code == 404

    async def test_stop_dashboard_no_active_dashboard_is_noop(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.delete(f"/sessions/{sid}/dashboard")
        assert resp.status_code == 200

    async def test_delete_session_stops_dashboard_and_removes_route(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        await c.post(f"/sessions/{sid}/dashboard", json={"code": "x"})
        with patch("execution_api.server._vsock_request", return_value={"status": "ok"}) as vsock, \
             patch("execution_api.server.caddy.remove_route", new_callable=AsyncMock) as rm:
            resp = await c.delete(f"/sessions/{sid}")
        assert resp.status_code == 200
        assert vsock.await_count >= 1
        rm.assert_awaited_once_with(sid)

    async def test_delete_session_without_dashboard_is_unchanged(self, client):
        c, _ = client
        sid = (await c.post("/sessions")).json()["session_id"]
        resp = await c.delete(f"/sessions/{sid}")
        assert resp.status_code == 200
```

- [ ] **Step 2: Run failing endpoint tests**

```bash
uv run pytest tests/test_execution_api.py::TestDashboardEndpoints -v
```

- [ ] **Step 3: Implement dashboard endpoints and cleanup behavior**

Add imports/config near top of `execution_api/server.py`:

```python
from .caddy_client import CaddyClient, _vsock_request
from .models import DashboardRequest, DashboardResponse

CADDY_ADMIN_URL = os.environ.get("CADDY_ADMIN_URL", "http://localhost:2019")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "5006"))

caddy = CaddyClient(admin_url=CADDY_ADMIN_URL)
```

In `create_app()` after execute endpoints:

```python
    @app.post("/sessions/{session_id}/dashboard", response_model=DashboardResponse)
    async def launch_dashboard(session_id: str, req: DashboardRequest):
        entry = session_manager.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")
        if entry.vsock_path is None or entry.vm_ip is None:
            raise HTTPException(status_code=503, detail="VM info not available")

        if entry.active_dashboard is not None:
            try:
                await _vsock_request(entry.vsock_path, {"action": "stop_dashboard"}, timeout=10)
            except Exception:
                logger.debug("Failed to stop existing dashboard before replacement", exc_info=True)

        app_id = uuid.uuid4().hex[:12]
        try:
            resp = await _vsock_request(
                entry.vsock_path,
                {
                    "action": "launch_dashboard",
                    "code": req.code,
                    "port": DASHBOARD_PORT,
                    "app_id": app_id,
                    "session_id": session_id,
                },
                timeout=30,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"vsock error: {exc}")

        if resp.get("status") != "ok":
            raise HTTPException(status_code=500, detail=resp.get("message", "launch failed"))

        try:
            await caddy.add_route(session_id, f"{entry.vm_ip}:{DASHBOARD_PORT}")
        except Exception as exc:
            try:
                await _vsock_request(entry.vsock_path, {"action": "stop_dashboard"}, timeout=10)
            except Exception:
                pass
            raise HTTPException(status_code=503, detail=f"caddy route error: {exc}")

        entry.active_dashboard = app_id
        return DashboardResponse(
            url=f"/dash/{session_id}/dash_{app_id}",
            session_id=session_id,
            app_id=app_id,
        )

    @app.delete("/sessions/{session_id}/dashboard", response_model=DeleteResponse)
    async def stop_dashboard(session_id: str):
        entry = session_manager.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="session not found")

        if entry.vsock_path and entry.active_dashboard:
            try:
                await _vsock_request(entry.vsock_path, {"action": "stop_dashboard"}, timeout=10)
            except Exception:
                logger.debug("Failed to stop dashboard", exc_info=True)

        try:
            await caddy.remove_route(session_id)
        except Exception:
            logger.debug("Failed to remove Caddy route", exc_info=True)

        entry.active_dashboard = None
        return DeleteResponse()
```

Update `SessionManager.delete()` cleanup block:

```python
        if entry.vsock_path and entry.active_dashboard:
            try:
                await _vsock_request(entry.vsock_path, {"action": "stop_dashboard"}, timeout=10)
            except Exception:
                logger.debug("Failed to stop dashboard during delete", exc_info=True)

            try:
                await caddy.remove_route(session_id)
            except Exception:
                logger.debug("Failed to remove dashboard route during delete", exc_info=True)

            entry.active_dashboard = None
```

- [ ] **Step 4: Run test suite for execution API**

```bash
uv run pytest tests/test_execution_api.py -v
```

- [ ] **Step 5: Commit**

```bash
git add execution_api/server.py tests/test_execution_api.py
git commit -m "feat(api): add dashboard lifecycle endpoints and cleanup"
```

---

## Chunk 5: Schemas + Config + Integration

### Task 7: Tool schemas, Caddy config, and remote-test orchestration

**Files:**
- Modify: `execution_api/tool_schemas/claude.json`
- Modify: `execution_api/tool_schemas/openai.json`
- Create: `config/Caddyfile`
- Modify: `scripts/remote-test.sh`

- [ ] **Step 1: Add failing assertions for schema/config smoke checks**

Use quick validation commands first:

```bash
uv run python -c "import json; d=json.load(open('execution_api/tool_schemas/claude.json')); assert any(t['name']=='launch_dashboard' for t in d)"
uv run python -c "import json; d=json.load(open('execution_api/tool_schemas/openai.json')); assert any(t['function']['name']=='launch_dashboard' for t in d)"
```

Expected: FAIL before changes.

- [ ] **Step 2: Update tool schemas with complete additions**

Append to `execution_api/tool_schemas/claude.json` array:

```json
{
  "name": "launch_dashboard",
  "description": "Launch an interactive Panel dashboard inside the sandbox VM and return a browser URL.",
  "input_schema": {
    "type": "object",
    "properties": {
      "code": {
        "type": "string",
        "description": "Panel dashboard Python code executed inside the VM."
      },
      "framework": {
        "type": "string",
        "enum": ["panel"],
        "description": "Visualization framework. Only panel is supported."
      }
    },
    "required": ["code"]
  }
}
```

Append to `execution_api/tool_schemas/openai.json` array:

```json
{
  "type": "function",
  "function": {
    "name": "launch_dashboard",
    "description": "Launch an interactive Panel dashboard inside the sandbox VM and return a browser URL.",
    "parameters": {
      "type": "object",
      "properties": {
        "code": {
          "type": "string",
          "description": "Panel dashboard Python code executed inside the VM."
        },
        "framework": {
          "type": "string",
          "enum": ["panel"],
          "description": "Visualization framework. Only panel is supported."
        }
      },
      "required": ["code"]
    }
  }
}
```

- [ ] **Step 3: Add `config/Caddyfile`**

```caddy
{
    admin localhost:2019
    auto_https off
}

:8080 {
    handle /api/kernels/* {
        reverse_proxy localhost:8888
    }

    handle /artifacts/* {
        root * /var/lib/sandbox-artifacts
        file_server
    }

    handle /health {
        respond "ok" 200
    }

    # Dynamic routes added by execution_api.caddy_client:
    # /dash/{session_id}/* -> {vm_ip}:5006
}
```

- [ ] **Step 4: Update `scripts/remote-test.sh` to start/stop Caddy**

Add startup after Execution API boot section:

```bash
step "Starting Caddy"
ssh -f "$HOST" "cd $REMOTE_DIR && nohup caddy run --config config/Caddyfile </dev/null >/tmp/fc-caddy.log 2>&1"
```

Add readiness check in loop:

```bash
CADDY_READY=false

if [[ "$CADDY_READY" == "false" ]]; then
    if ssh "$HOST" "curl -sf http://localhost:8080/health" &>/dev/null; then
        CADDY_READY=true
        info "Caddy is ready ✓"
    fi
fi

if [[ "$POOL_READY" == "true" && "$GW_READY" == "true" && "$API_READY" == "true" && "$CADDY_READY" == "true" ]]; then
    break
fi

sleep 2
ELAPSED=$((ELAPSED + 2))

if [[ "$CADDY_READY" == "false" ]]; then
    if ssh "$HOST" "curl -sf http://localhost:8080/health" &>/dev/null; then
        CADDY_READY=true
        info "Caddy is ready ✓"
    fi
fi
if [[ "$POOL_READY" == "true" && "$GW_READY" == "true" && "$API_READY" == "true" && "$CADDY_READY" == "true" ]]; then
    break
fi
```

Add teardown:

```bash
if pgrep -f "caddy run --config config/Caddyfile" >/dev/null 2>&1; then
    pkill -f "caddy run --config config/Caddyfile" 2>/dev/null
    echo "  Killed Caddy"
fi
```

- [ ] **Step 5: Validate JSON and shell syntax + commit**

Run:
```bash
uv run python -c "import json; json.load(open('execution_api/tool_schemas/claude.json')); json.load(open('execution_api/tool_schemas/openai.json')); print('ok')"
bash -n scripts/remote-test.sh
```

Commit:
```bash
git add execution_api/tool_schemas/claude.json execution_api/tool_schemas/openai.json config/Caddyfile scripts/remote-test.sh
git commit -m "feat(dashboard): add tool schemas, caddy config, and remote test orchestration"
```

---

### Task 8: Add dashboard integration tests

**Files:**
- Modify: `tests/test_integration.py`

- [ ] **Step 1: Add integration tests (failing first)**

Append to `tests/test_integration.py`:

```python
class TestDashboardIntegration:
    async def test_dashboard_launch_and_access(self):
        async with aiohttp.ClientSession() as http:
            sid = (await (await http.post(f"{EXECUTION_API_URL}/sessions")).json())["session_id"]
            resp = await http.post(
                f"{EXECUTION_API_URL}/sessions/{sid}/dashboard",
                json={"code": "import panel as pn\npn.panel('hello').servable()"},
            )
            assert resp.status == 200
            data = await resp.json()
            dash = await http.get(f"http://localhost:8080{data['url']}")
            assert dash.status == 200

    async def test_dashboard_data_from_kernel(self):
        async with aiohttp.ClientSession() as http:
            sid = (await (await http.post(f"{EXECUTION_API_URL}/sessions")).json())["session_id"]
            await http.post(
                f"{EXECUTION_API_URL}/sessions/{sid}/execute",
                json={"code": "import pandas as pd; pd.DataFrame({'x':[1,2]}).to_parquet('/data/processed.parquet')"},
            )
            resp = await http.post(
                f"{EXECUTION_API_URL}/sessions/{sid}/dashboard",
                json={
                    "code": (
                        "import pandas as pd, panel as pn\n"
                        "df = pd.read_parquet('/data/processed.parquet')\n"
                        "pn.panel(df).servable()"
                    )
                },
            )
            assert resp.status == 200

    async def test_dashboard_replace(self):
        async with aiohttp.ClientSession() as http:
            sid = (await (await http.post(f"{EXECUTION_API_URL}/sessions")).json())["session_id"]
            r1 = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard", json={"code": "import panel as pn\npn.panel('v1').servable()"})
            r2 = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard", json={"code": "import panel as pn\npn.panel('v2').servable()"})
            assert r1.status == 200
            assert r2.status == 200

    async def test_dashboard_cleanup_on_session_delete(self):
        async with aiohttp.ClientSession() as http:
            sid = (await (await http.post(f"{EXECUTION_API_URL}/sessions")).json())["session_id"]
            launch = await http.post(f"{EXECUTION_API_URL}/sessions/{sid}/dashboard", json={"code": "import panel as pn\npn.panel('bye').servable()"})
            url = (await launch.json())["url"]
            await http.delete(f"{EXECUTION_API_URL}/sessions/{sid}")
            dead = await http.get(f"http://localhost:8080{url}")
            assert dead.status in (404, 502)
```

- [ ] **Step 2: Run integration tests on remote host**

```bash
./scripts/remote-test.sh <user@host> --skip-setup
```

Expected: unit + integration green, including new dashboard tests.

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "test(integration): add dashboard launch, data, replace, and cleanup coverage"
```

---

## Final Verification Checklist

- [ ] `uv run pytest tests/ -v -m "not integration"` passes
- [ ] `uv run pytest tests/test_integration.py -v -m integration` passes (on KVM host)
- [ ] `lsp_diagnostics` reports no errors in changed Python files
- [ ] `docs/superpowers/specs/2026-03-23-dashboard-serving-design.md` and this plan remain in sync
