# Dashboard Serving — Design Specification

> **Date**: 2026-03-23
> **Status**: Approved
> **Issue**: #18
> **Approach**: Panel inside VM + Caddy reverse proxy + vsock control plane

---

## 1. Overview

Enable LLMs to launch interactive Panel dashboards that run **inside** the same Firecracker microVM that already holds the user's data and kernel session. The dashboard is served to the browser via Caddy acting as a path-prefix reverse proxy.

### Architecture Decision: Panel Runs Inside the VM

Dashboard code is LLM-generated from user prompts. Running it on the host would be a sandbox escape. Panel runs inside each Firecracker microVM, maintaining full isolation:

```
Browser → Caddy (/dash/{session_id}/*) → Panel in VM:5006
                                          (same VM as ipykernel — same /data/ filesystem)
```

This means:
- Dashboard code can read `/data/processed.parquet` directly (no inter-process copy)
- A compromised Panel process can only affect its own VM
- VM destruction at session end kills Panel automatically

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Where Panel runs | Inside each VM | Untrusted LLM code; sandbox escape prevention |
| Reverse proxy | Caddy | Dynamic admin API for route management; WebSocket support via `flush_interval -1` |
| Path prefix | `/dash/{session_id}/` | Single Caddy instance, multi-tenant; Panel's `--prefix` handles this natively |
| Panel package in rootfs | Already present | `bokeh` and `panel` already installed in `build_rootfs.sh` line 90 |
| Dashboard process lifecycle | Tied to session | Panel killed on session delete; Caddy route removed atomically |
| App ID scope | One active dashboard per session | Simplest model; stop+restart replaces the previous dashboard |

### What This Does NOT Include

- Multiple simultaneous dashboards per session (one at a time)
- Dashboard authentication or user isolation beyond VM isolation
- Panel hot-reload / file watching (`--dev` mode)
- Dashboard persistence across session deletion

---

## 2. Rootfs

### Current State

`guest/build_rootfs.sh` line 90 already installs `bokeh` and `panel`:

```bash
chroot "$ROOTFS_DIR" pip3 install --break-system-packages \
    ipykernel jupyter-client \
    plotly seaborn bokeh panel hvplot   # ← panel + bokeh already present
```

No rootfs changes are needed. Panel is loaded lazily — the `panel serve` process is only started when `launch_dashboard` is called, not at boot. The VM boot path is unaffected.

### Memory Note

VMs running both ipykernel and `panel serve` simultaneously will use more memory than kernel-only sessions. Observed baseline:

- ipykernel alone: ~150–200 MB RSS
- `panel serve` with a modest dashboard: ~200–400 MB RSS
- Combined peak: up to ~600 MB

The current default `mem_mib: 512` in `config/fc-pool.yaml` is tight. **Document as a prerequisite** that operators running dashboards should set `mem_mib: 768` or higher for dashboard-capable pools. Do not change the default in this spec — that is a deployment configuration decision.

---

## 3. Guest Agent Extension

### New Actions

Two new actions added to `handle_message()` in `guest/fc_guest_agent.py`:

#### `launch_dashboard`

```json
{
  "action": "launch_dashboard",
  "code": "import panel as pn\n...",
  "port": 5006,
  "app_id": "abc123",
  "session_id": "sess_xyz"
}
```

Behavior:
1. Creates `/apps/` directory if it does not exist
2. Writes `code` to `/apps/dash_{app_id}.py`
3. Kills any existing `panel serve` process (tracked in `panel_proc` module global)
4. Starts `panel serve /apps/dash_{app_id}.py --port {port} --address 0.0.0.0 --allow-websocket-origin "*" --prefix /dash/{session_id}`
5. Waits up to 10 s for port `{port}` to open (TCP connect check, same pattern as `wait_for_kernel_ports`)
6. Returns `{"status": "ok", "app_id": "...", "port": 5006}`

On failure (port never opened, process exits immediately):
- Returns `{"status": "error", "message": "panel serve did not start: ..."}`

#### `stop_dashboard`

```json
{"action": "stop_dashboard"}
```

Behavior:
1. If `panel_proc` is running: sends SIGTERM, waits up to 5 s, then SIGKILL
2. Deletes `/apps/dash_*.py` files (cleanup)
3. Returns `{"status": "ok"}`

If no dashboard is running: returns `{"status": "ok"}` (idempotent).

### Module-Level State

```python
panel_proc: subprocess.Popen | None = None  # new global alongside kernel_proc
```

### Code Sketch — `guest/fc_guest_agent.py` additions

```python
# Add alongside kernel_proc at module level:
panel_proc = None
_APPS_DIR = "/apps"

def _kill_proc(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Terminate a process gracefully, then SIGKILL if needed."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def start_dashboard(code: str, port: int, app_id: str, session_id: str) -> None:
    """Write dashboard code and start panel serve. Blocks until port is open."""
    global panel_proc

    os.makedirs(_APPS_DIR, exist_ok=True)
    app_path = f"{_APPS_DIR}/dash_{app_id}.py"
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

    # Wait for panel to open its port
    try:
        wait_for_kernel_ports("127.0.0.1", {"panel": port}, timeout=10.0)
    except RuntimeError as exc:
        _kill_proc(panel_proc)
        panel_proc = None
        raise RuntimeError(f"panel serve did not start: {exc}") from exc


def stop_dashboard() -> None:
    """Kill the running panel serve process and clean up app files."""
    global panel_proc

    if panel_proc is not None:
        _kill_proc(panel_proc)
        panel_proc = None

    # Remove app files
    if os.path.isdir(_APPS_DIR):
        for fname in os.listdir(_APPS_DIR):
            if fname.startswith("dash_"):
                try:
                    os.remove(os.path.join(_APPS_DIR, fname))
                except OSError:
                    pass
```

In `handle_message()`, add two new branches before the `else` clause:

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

---

## 4. Execution API Extension

### New Endpoints

| Endpoint | Method | Request | Response | Purpose |
|----------|--------|---------|----------|---------|
| `POST /sessions/{session_id}/dashboard` | POST | `{"code": str}` | `DashboardResponse` | Launch dashboard in VM |
| `DELETE /sessions/{session_id}/dashboard` | DELETE | — | `{"ok": true}` | Stop dashboard + remove route |

### How the API Finds VM Connection Info

`SandboxSession.start()` only stores `session._kernel_id` locally after `POST /api/kernels`; it does **not** expose VM network details. The dashboard endpoint needs both `vm_ip` (for Caddy upstream) and `vsock_path` (for guest-agent control).

`FirecrackerProvisioner.pre_launch()` already has the required VM info (`vm_id`, `vm_ip`, `vsock_path`) immediately after `pool_client.acquire()`. The clean lookup path is:

1. Provisioner binds `kernel_id → vm_id` in the pool manager
2. Execution API resolves `kernel_id` to `{vm_id, ip, vsock_path}` via pool manager Unix socket API
3. `SessionEntry` persists `vm_ip` + `vsock_path` for dashboard lifecycle operations

This avoids brittle heuristics (e.g., guessing "most recently assigned VM") and does not require changing `sandbox_client` API shape.

### `SessionEntry` Extension

```python
@dataclass
class SessionEntry:
    session: SandboxSession
    session_id: str
    created_at: float
    last_active: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    vm_ip: str | None = None         # NEW — populated after session.start()
    vsock_path: str | None = None    # NEW — guest-agent control channel
    active_dashboard: str | None = None  # NEW — current app_id, or None
```

### Dashboard Endpoint Logic

```
POST /sessions/{session_id}/dashboard
  ├─ Resolve session → 404 if missing
  ├─ Resolve vm_ip → 503 if not available
  ├─ Generate app_id = uuid4().hex[:12]
  ├─ Send vsock launch_dashboard → guest agent in VM
  ├─ Register Caddy route: /dash/{session_id}/* → {vm_ip}:5006
  ├─ Store app_id in entry.active_dashboard
  └─ Return {url, session_id, app_id}

DELETE /sessions/{session_id}/dashboard
  ├─ Resolve session → 404 if missing
  ├─ Send vsock stop_dashboard → guest agent in VM
  ├─ Remove Caddy route for session_id
  ├─ Clear entry.active_dashboard
  └─ Return {ok: true}
```

### Dashboard Cleanup on Session Delete

Existing `DELETE /sessions/{session_id}` calls `session_manager.delete()`. Extend this to also:
1. Send `stop_dashboard` vsock message (best-effort, errors suppressed)
2. Remove Caddy route (best-effort, errors suppressed)

Same error-suppression pattern used by `session.stop()` throughout `server.py`.

### How vsock is called from the Execution API

The pool manager's `vsock_request()` is in `fc_pool_manager/vsock.py`. The Execution API must send vsock messages but should not import from `fc_pool_manager` (separation of concerns). Extract the vsock helper into a shared module or duplicate the ~40-line function in `execution_api/`.

**Decision:** Duplicate the function in `execution_api/caddy_client.py` as a private helper `_vsock_request()`. It is small, self-contained, and avoids a cross-package dependency. If it grows, extract to a shared `vsock_utils` module later.

The vsock path for a session's VM is not directly known to the Execution API. **Solution:** Store `vsock_path` in `SessionEntry` alongside `vm_ip`.

```python
@dataclass
class SessionEntry:
    ...
    vm_ip: str | None = None
    vsock_path: str | None = None   # path like /srv/jailer/{vm_id}/root/v.sock
    active_dashboard: str | None = None
```

Populate `vm_ip` and `vsock_path` during `SessionManager.create()` by querying the pool manager after `session.start()`. The kernel ID is available as `entry.session._kernel_id` after `start()`. Call `GET /api/vms/by-kernel/{kernel_id}` on the pool manager Unix socket.

### 4.5 Pool Manager Extension

`GET /api/vms/by-kernel/{kernel_id}` needs an explicit kernel→VM index; `kernel_id` is not derivable from `vm_id`. The index is populated when the provisioner binds `self.kernel_id` to the acquired VM.

```python
# In fc_pool_manager/server.py — add route handlers

from fc_pool_manager.vm import VMState


async def handle_bind_kernel(request: web.Request) -> web.Response:
    vm_id = request.match_info["vm_id"]
    body = await request.json()
    kernel_id = body.get("kernel_id")
    if not kernel_id:
        return web.json_response({"error": "kernel_id required"}, status=400)

    manager = request.app["manager"]
    vm = manager._vms.get(vm_id)
    if vm is None:
        return web.json_response({"error": "VM not found"}, status=404)

    manager._kernel_to_vm[kernel_id] = vm_id
    return web.json_response({"ok": True})


async def handle_vm_by_kernel(request: web.Request) -> web.Response:
    kernel_id = request.match_info["kernel_id"]
    manager = request.app["manager"]

    vm_id = manager._kernel_to_vm.get(kernel_id)
    if vm_id is None:
        return web.json_response({"error": "VM not found for kernel"}, status=404)

    vm = manager._vms.get(vm_id)
    if vm is None or vm.state != VMState.ASSIGNED:
        return web.json_response({"error": "VM not found for kernel"}, status=404)

    return web.json_response({
        "vm_id": vm.vm_id,
        "ip": vm.ip,
        "vsock_path": vm.vsock_path,
    })
```

Route wiring sketch:

```python
app.router.add_post("/api/vms/{vm_id}/bind-kernel", handle_bind_kernel)
app.router.add_get("/api/vms/by-kernel/{kernel_id}", handle_vm_by_kernel)
```

Provisioner-side bind call sketch (after successful acquire):

```python
# in FirecrackerProvisioner.pre_launch()
await self.pool_client.bind_kernel(self.vm_id, self.kernel_id)
```

This is the minimum robust plumbing that makes `GET /api/vms/by-kernel/{kernel_id}` deterministic.

### Code Sketch — `execution_api/server.py` additions

```python
# New imports
import aiohttp
from .caddy_client import CaddyClient
from .models import DashboardRequest, DashboardResponse

CADDY_ADMIN_URL = os.environ.get("CADDY_ADMIN_URL", "http://localhost:2019")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "5006"))
POOL_SOCKET = os.environ.get("POOL_SOCKET", "/var/run/fc-pool.sock")

caddy = CaddyClient(admin_url=CADDY_ADMIN_URL)

# ── Dashboard endpoints ──────────────────────────────────────────────────

@app.post("/sessions/{session_id}/dashboard", response_model=DashboardResponse)
async def launch_dashboard(session_id: str, req: DashboardRequest):
    entry = session_manager.get(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="session not found")

    if entry.vsock_path is None:
        raise HTTPException(status_code=503, detail="VM info not available")

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

    upstream = f"{entry.vm_ip}:{DASHBOARD_PORT}"
    try:
        await caddy.add_route(session_id, upstream)
    except Exception as exc:
        # Best-effort cleanup: stop the dashboard we just started
        try:
            await _vsock_request(entry.vsock_path, {"action": "stop_dashboard"}, timeout=10)
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=f"caddy route error: {exc}")

    entry.active_dashboard = app_id
    url = f"/dash/{session_id}/dash_{app_id}"
    return DashboardResponse(url=url, session_id=session_id, app_id=app_id)


@app.delete("/sessions/{session_id}/dashboard", response_model=DeleteResponse)
async def stop_dashboard(session_id: str):
    entry = session_manager.get(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="session not found")

    if entry.vsock_path is not None:
        try:
            await _vsock_request(entry.vsock_path, {"action": "stop_dashboard"}, timeout=10)
        except Exception:
            logger.debug("Failed to send stop_dashboard vsock", exc_info=True)

    try:
        await caddy.remove_route(session_id)
    except Exception:
        logger.debug("Failed to remove Caddy route for %s", session_id, exc_info=True)

    entry.active_dashboard = None
    return DeleteResponse()
```

### `SessionManager.create()` extension

After `session.start()`, populate `vm_ip` and `vsock_path` by querying the pool manager:

```python
# After await session.start()
kernel_id = session._kernel_id
vm_info = await _lookup_vm_by_kernel(POOL_SOCKET, kernel_id)
entry.vm_ip = vm_info.get("ip")
entry.vsock_path = vm_info.get("vsock_path")
```

Where `_lookup_vm_by_kernel` is a small aiohttp helper that calls the pool manager's Unix socket HTTP API. If the lookup fails (e.g., pool manager not running), `vm_ip` and `vsock_path` remain `None` — the session still works for code execution, just not for dashboards.

### `SessionManager.delete()` extension

```python
async def delete(self, session_id: str, caddy: CaddyClient | None = None) -> bool:
    entry = self._sessions.pop(session_id, None)
    if entry is None:
        return False

    # Stop dashboard if active
    if entry.vsock_path is not None and entry.active_dashboard is not None:
        try:
            await _vsock_request(entry.vsock_path, {"action": "stop_dashboard"}, timeout=10)
        except Exception:
            logger.debug("Failed to stop dashboard during session delete", exc_info=True)

    if caddy is not None and entry.active_dashboard is not None:
        try:
            await caddy.remove_route(session_id)
        except Exception:
            logger.debug("Failed to remove Caddy route during session delete", exc_info=True)

    try:
        await entry.session.stop()
    except Exception:
        logger.debug("Failed to stop session %s", session_id, exc_info=True)
    return True
```

---

## 5. Caddy Client Module

### New File: `execution_api/caddy_client.py`

Complete self-contained module with no external dependencies beyond `aiohttp`.

```python
"""CaddyClient — manage dashboard routes via Caddy admin API."""

import asyncio
import json
import struct
from typing import Any

import aiohttp

HEADER_FMT = "!I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
GUEST_AGENT_PORT = 52


class CaddyClient:
    """Manages dynamic Caddy routes via the admin API (localhost:2019).

    Routes are added/removed per session:
        /dash/{session_id}/*  →  reverse_proxy {vm_ip}:{port}
    """

    def __init__(self, admin_url: str = "http://localhost:2019"):
        self._admin_url = admin_url.rstrip("/")

    def _route_id(self, session_id: str) -> str:
        return f"dashboard_{session_id}"

    def _build_route(self, session_id: str, upstream: str) -> dict:
        """Build a Caddy route config object for a dashboard session."""
        return {
            "@id": self._route_id(session_id),
            "match": [{"path": [f"/dash/{session_id}/*"]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": upstream}],
                    "flush_interval": -1,
                    "headers": {
                        "request": {
                            "set": {
                                "X-Forwarded-Proto": ["{http.request.scheme}"],
                                "X-Forwarded-Host": ["{http.request.host}"],
                            }
                        }
                    },
                }
            ],
        }

    async def add_route(self, session_id: str, upstream: str) -> None:
        """Register /dash/{session_id}/* → upstream in Caddy."""
        route = self._build_route(session_id, upstream)
        route_id = self._route_id(session_id)
        url = f"{self._admin_url}/config/apps/http/servers/main/routes/..."
        # Use the named-route (ID) API: PUT /id/{id}
        put_url = f"{self._admin_url}/id/{route_id}"
        async with aiohttp.ClientSession() as http:
            # First try to update existing route; if 404, add a new one
            resp = await http.put(
                put_url,
                json=route,
                headers={"Content-Type": "application/json"},
            )
            if resp.status == 404:
                # Route doesn't exist yet — append to routes array
                add_url = f"{self._admin_url}/config/apps/http/servers/main/routes/0"
                resp = await http.post(
                    add_url,
                    json=route,
                    headers={"Content-Type": "application/json"},
                )
            if resp.status not in (200, 201):
                body = await resp.text()
                raise RuntimeError(f"Caddy add_route failed ({resp.status}): {body}")

    async def remove_route(self, session_id: str) -> None:
        """Remove the /dash/{session_id}/* route from Caddy."""
        route_id = self._route_id(session_id)
        del_url = f"{self._admin_url}/id/{route_id}"
        async with aiohttp.ClientSession() as http:
            resp = await http.delete(del_url)
            if resp.status not in (200, 204, 404):
                body = await resp.text()
                raise RuntimeError(f"Caddy remove_route failed ({resp.status}): {body}")
        # 404 is acceptable — route may have already been removed


async def _vsock_request(
    vsock_uds_path: str,
    msg: dict[str, Any],
    timeout: float = 30,
) -> dict[str, Any]:
    """Send a vsock request to a guest agent via Firecracker's UDS proxy.

    Duplicated from fc_pool_manager/vsock.py to avoid cross-package dependency.
    Wire protocol: Firecracker CONNECT handshake + 4-byte big-endian length + JSON.
    """
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
        resp_data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        return json.loads(resp_data)
    finally:
        writer.close()
        await writer.wait_closed()
```

---

## 6. Pydantic Models

### New models added to `execution_api/models.py`

```python
class DashboardRequest(BaseModel):
    code: str


class DashboardResponse(BaseModel):
    url: str          # e.g. "/dash/{session_id}/dash_{app_id}"
    session_id: str
    app_id: str
```

No changes to existing models.

---

## 7. Tool Schemas

### New tool added to both schema files

The `launch_dashboard` tool is added alongside the existing `execute_python_code` tool.

### `execution_api/tool_schemas/claude.json` addition

```json
{
  "name": "launch_dashboard",
  "description": "Launch an interactive Panel dashboard inside the sandbox VM. Use after execute_python_code has computed data — the dashboard reads the same files (e.g. /data/processed.parquet) that the kernel wrote. Returns a URL the user can open in their browser. The dashboard persists until the session ends or a new dashboard replaces it.",
  "input_schema": {
    "type": "object",
    "properties": {
      "code": {
        "type": "string",
        "description": "Python code for the Panel dashboard. Must import panel and call pn.serve() or define a serveable object. Can read files written by execute_python_code (e.g. pd.read_parquet('/data/results.parquet'))."
      },
      "framework": {
        "type": "string",
        "description": "Visualization framework. Currently only 'panel' is supported.",
        "enum": ["panel"]
      }
    },
    "required": ["code"]
  }
}
```

### `execution_api/tool_schemas/openai.json` addition

```json
{
  "type": "function",
  "function": {
    "name": "launch_dashboard",
    "description": "Launch an interactive Panel dashboard inside the sandbox VM. Use after execute_python_code has computed data — the dashboard reads the same files (e.g. /data/processed.parquet) that the kernel wrote. Returns a URL the user can open in their browser. The dashboard persists until the session ends or a new dashboard replaces it.",
    "parameters": {
      "type": "object",
      "properties": {
        "code": {
          "type": "string",
          "description": "Python code for the Panel dashboard. Must import panel and define a serveable object. Can read files written by execute_python_code."
        },
        "framework": {
          "type": "string",
          "description": "Visualization framework. Currently only 'panel' is supported.",
          "enum": ["panel"]
        }
      },
      "required": ["code"]
    }
  }
}
```

---

## 8. Caddy Configuration

### `config/Caddyfile` (new file)

Base static configuration. Dynamic dashboard routes are added/removed via the admin API at runtime.

```caddy
{
    admin localhost:2019
    auto_https off
}

:8080 {
    # Jupyter Kernel Gateway — notebook execution API
    handle /api/kernels/* {
        reverse_proxy localhost:8888
    }

    # Static artifact files (images, HTML, etc.)
    handle /artifacts/* {
        root * /var/lib/sandbox-artifacts
        file_server
    }

    # Health check
    handle /health {
        respond "ok" 200
    }

    # Dynamic dashboard routes are added here at runtime via Caddy admin API.
    # Each route has pattern: /dash/{session_id}/* → {vm_ip}:5006
    # Routes are managed by execution_api/caddy_client.py.
}
```

### Running Caddy

Caddy must be running before the Execution API starts (prerequisite):

```bash
# Start Caddy with the base config
caddy run --config config/Caddyfile

# Or as a background service:
caddy start --config config/Caddyfile
```

Caddy is **not** a Python dependency and must be installed separately on the host:

```bash
# Debian/Ubuntu
apt install caddy

# Or from binary
curl -L https://github.com/caddyserver/caddy/releases/latest/download/caddy_linux_amd64.tar.gz | tar xz
```

### How Caddy Route Registration Works

Caddy's admin API supports named routes via `@id` tags. When `CaddyClient.add_route()` is called:

1. It builds a route object with `"@id": "dashboard_{session_id}"` — this registers the route with a stable ID
2. `PUT /id/dashboard_{session_id}` updates or creates the route in-place
3. If `PUT` returns 404 (first time), `POST /config/apps/http/servers/main/routes/0` prepends the route

When `CaddyClient.remove_route()` is called:
1. `DELETE /id/dashboard_{session_id}` removes it atomically
2. 404 is tolerated — idempotent

### Path Rewriting and WebSocket Support

Panel's `--prefix /dash/{session_id}` flag instructs Panel to serve its assets and routes under that prefix. Caddy does **not** strip the prefix before forwarding — Panel sees the full path and handles it correctly.

WebSocket upgrade is automatic in Caddy's `reverse_proxy` handler. The `flush_interval: -1` setting disables response buffering, which is required for WebSocket and streaming HTTP (Server-Sent Events used by Panel's Bokeh backend).

---

## 9. Data Flow

### Complete Request Flow

```
1. LLM calls execute_python_code
   → Execution API → Kernel Gateway → ipykernel in VM
   → VM writes /data/processed.parquet

2. LLM calls launch_dashboard(code="import panel as pn; ...")
   → POST /sessions/{session_id}/dashboard

3. Execution API:
   a. Looks up session entry → vm_ip, vsock_path
   b. Generates app_id = "a1b2c3d4e5f6"
   c. Sends vsock message to guest agent:
      {action: "launch_dashboard", code: "...", port: 5006,
       app_id: "a1b2c3d4e5f6", session_id: "sess_abc"}
   d. Guest agent writes /apps/dash_a1b2c3d4e5f6.py
   e. Guest agent starts: panel serve /apps/dash_a1b2c3d4e5f6.py
                                      --port 5006 --address 0.0.0.0
                                      --allow-websocket-origin "*"
                                      --prefix /dash/sess_abc
   f. Guest agent waits for port 5006 to open (TCP connect)
   g. Returns {status: "ok", app_id: "...", port: 5006}
   h. Execution API calls CaddyClient.add_route("sess_abc", "172.16.0.5:5006")
   i. Caddy admin API registers: /dash/sess_abc/* → 172.16.0.5:5006
   j. Returns {url: "/dash/sess_abc/dash_a1b2c3d4e5f6", session_id: "sess_abc", app_id: "..."}

4. Dashboard code reads /data/processed.parquet (local to VM, same filesystem as kernel)

5. Browser loads /dash/sess_abc/dash_a1b2c3d4e5f6
   → Caddy receives request
   → Matches route /dash/sess_abc/*
   → Reverse proxies to 172.16.0.5:5006
   → Panel serves the dashboard HTML

6. WebSocket for live updates:
   Browser ↔ Caddy (ws upgrade via flush_interval -1) ↔ Panel in VM

7. Session delete (user or TTL):
   a. Execution API sends stop_dashboard vsock → guest agent kills panel_proc
   b. CaddyClient.remove_route("sess_abc") → route deleted
   c. SandboxSession.stop() → kernel deleted, VM released/destroyed
```

### Data Isolation

```
VM_A (session sess_abc)          VM_B (session sess_xyz)
├── ipykernel                    ├── ipykernel
├── panel_proc                   ├── (no dashboard)
├── /data/processed.parquet      ├── /data/
└── /apps/dash_a1b2c3d4.py       └── /apps/

 ↕ vsock (isolated)               ↕ vsock (isolated)
 ↕ TAP network (ebtables: no VM↔VM traffic)

Caddy (host)
├── /dash/sess_abc/* → 172.16.0.5:5006  (VM_A)
└── /dash/sess_xyz/* → (no route)
```

No cross-session data access. No cross-VM network traffic. VM destruction is atomic cleanup.

---

## 10. Session Lifecycle

```
Session created
  → VM acquired from pool (ip + vsock_path stored in SessionEntry)
  → ipykernel started inside VM
  → SessionEntry: {session_id, vm_ip, vsock_path, active_dashboard=None}
    ↓
execute_python_code × N
  → Data computed, artifacts saved
  → Variables and files persist in VM
    ↓
launch_dashboard (optional)
  → Panel starts inside VM on port 5006
  → Caddy route: /dash/{session_id}/* → {vm_ip}:5006
  → SessionEntry: {active_dashboard: app_id}
    ↓
User interacts via browser
  → HTTP/WebSocket through Caddy → Panel in VM
  → Panel reads /data/*.parquet directly
    ↓
Session deleted (explicit DELETE or TTL expiry)
  → stop_dashboard vsock (kills panel_proc)       ← new
  → caddy.remove_route(session_id)                 ← new
  → session.stop() (deletes Kernel Gateway kernel)
  → Pool manager destroys VM
```

---

## 11. Configuration

### New Environment Variables (Execution API)

| Variable | Default | Purpose |
|----------|---------|---------|
| `CADDY_ADMIN_URL` | `http://localhost:2019` | Caddy admin API URL |
| `DASHBOARD_PORT` | `5006` | Port Panel serves on inside the VM |
| `POOL_SOCKET` | `/var/run/fc-pool.sock` | Pool manager Unix socket (for VM IP lookup) |

These are additive — all existing variables from the chatbot integration spec remain unchanged.

### Full Configuration Reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `GATEWAY_URL` | `http://localhost:8888` | Kernel Gateway URL |
| `SESSION_TTL` | `600` | Idle session timeout (seconds) |
| `MAX_SESSIONS` | `20` | Max concurrent sessions |
| `DEFAULT_TIMEOUT` | `30` | Default code execution timeout (seconds) |
| `ARTIFACT_BASE_DIR` | None | Artifact storage directory |
| `ARTIFACT_URL_PREFIX` | None | URL prefix for artifact URLs |
| `SERVE_ARTIFACTS` | `true` | Serve artifacts at `/artifacts/` |
| `PORT` | `8000` | API server port |
| `CADDY_ADMIN_URL` | `http://localhost:2019` | Caddy admin API (new) |
| `DASHBOARD_PORT` | `5006` | Panel serve port in VM (new) |
| `POOL_SOCKET` | `/var/run/fc-pool.sock` | Pool manager socket (new) |

### Running with Dashboard Support

```bash
# 1. Start Caddy (prerequisite)
caddy start --config config/Caddyfile

# 2. Start Execution API with dashboard support
CADDY_ADMIN_URL=http://localhost:2019 \
DASHBOARD_PORT=5006 \
POOL_SOCKET=/var/run/fc-pool.sock \
uv run python -m execution_api.server
```

### Pool Manager Memory Note

For pools intended to support dashboard sessions, increase VM memory:

```yaml
# config/fc-pool.yaml — dashboard-capable pool
vm_defaults:
  vcpu: 1
  mem_mib: 768    # was 512; Panel + ipykernel need ~600 MB peak
```

---

## 12. Error Handling

### HTTP Status Codes

| Scenario | HTTP Status | Body |
|----------|-------------|------|
| Session not found | 404 | `{"error": "session not found"}` |
| VM info not available (pool manager unreachable at session create) | 503 | `{"error": "VM info not available"}` |
| vsock error (VM unreachable, timeout) | 503 | `{"error": "vsock error: ..."}` |
| Panel failed to start (port never opened) | 500 | `{"error": "panel serve did not start: ..."}` |
| Caddy not running / route registration failed | 503 | `{"error": "caddy route error: ..."}` |
| Dashboard code has import error | 500 | Panel error propagated via guest agent message |

**Key principle:** Panel startup errors (import errors in dashboard code, missing library) surface as `status: "error"` from the guest agent, which become HTTP 500. These are distinct from infrastructure errors (503).

### Guest Agent Error Handling

| Scenario | Guest Agent Response |
|----------|---------------------|
| Panel process exits before port opens | `{"status": "error", "message": "panel serve did not start: ports did not open..."}` |
| Port times out (10 s) | `{"status": "error", "message": "panel serve did not start: kernel ports did not open within 10s"}` |
| `stop_dashboard` with no running dashboard | `{"status": "ok"}` (idempotent) |
| OS error writing app file | `{"status": "error", "message": "..."}` |

### Caddy Not Running

If Caddy is not running when `launch_dashboard` is called:
1. The guest agent succeeds (Panel starts in the VM)
2. `CaddyClient.add_route()` raises `aiohttp.ClientConnectorError`
3. Execution API returns HTTP 503 and attempts best-effort `stop_dashboard` vsock
4. The partial state (Panel running without a route) is cleaned up

If Caddy dies while a dashboard is active, browsers get connection refused. The VM and session remain intact; restarting Caddy and re-calling `POST /sessions/{id}/dashboard` restores the route.

### Cleanup Guarantees

| Failure Point | What Gets Cleaned Up | What May Leak |
|---------------|---------------------|---------------|
| vsock timeout during launch | Nothing launched | — |
| Panel starts, Caddy fails | Panel stopped (best-effort vsock) | Panel process if vsock also fails |
| Session delete with Panel running | Panel killed (vsock), route removed (Caddy) | Panel if VM already dead |
| Caddy route add fails mid-session | Panel stopped (best-effort) | Panel process if stop also fails |
| VM crashes | Panel dies with VM | Caddy route (orphaned until next remove_route call or Caddy restart) |

Orphaned Caddy routes return 502 to the browser (upstream unreachable), not 200. They are removed when the session is eventually deleted from the Execution API's session map.

---

## 13. Testing

### 13.1 Guest agent tests (`tests/test_guest_agent.py`)

Add focused unit tests for process lifecycle, startup failure handling, and file cleanup:

- `test_launch_dashboard_writes_file_and_starts_panel`
- `test_launch_dashboard_kills_existing_panel_first`
- `test_launch_dashboard_port_timeout_returns_error`
- `test_launch_dashboard_process_exits_early_returns_error`
- `test_launch_dashboard_missing_code_returns_error`
- `test_stop_dashboard_kills_process`
- `test_stop_dashboard_idempotent_when_no_panel`
- `test_stop_dashboard_cleans_app_files`
- `test_kill_proc_sigterm_then_sigkill`

### 13.2 Caddy client tests (`tests/test_caddy_client.py`)

Use mocked `aiohttp.ClientSession` responses (no live Caddy dependency):

- `test_add_route_put_success`
- `test_add_route_fallback_to_post_on_404`
- `test_add_route_server_error_raises`
- `test_remove_route_success`
- `test_remove_route_404_is_ok`
- `test_remove_route_server_error_raises`
- `test_route_id_format`
- `test_build_route_structure`

### 13.3 Execution API tests (`tests/test_execution_api.py`)

Add endpoint and cleanup behavior coverage with mocked vsock/Caddy dependencies:

- `test_launch_dashboard_success`
- `test_launch_dashboard_session_not_found`
- `test_launch_dashboard_no_vm_info`
- `test_launch_dashboard_vsock_error`
- `test_launch_dashboard_panel_start_error`
- `test_launch_dashboard_caddy_error_triggers_cleanup`
- `test_launch_dashboard_replaces_existing`
- `test_stop_dashboard_success`
- `test_stop_dashboard_session_not_found`
- `test_stop_dashboard_no_active_dashboard_is_noop`
- `test_delete_session_stops_dashboard_and_removes_route`
- `test_delete_session_without_dashboard_is_unchanged`

### 13.4 Pool manager tests (new endpoint coverage)

If `GET /api/vms/by-kernel/{kernel_id}` is implemented, add endpoint tests covering:

- successful lookup for assigned VM
- 404 when kernel is unknown
- 404 when mapped VM is no longer assigned/present
- bad bind payload handling (`kernel_id` missing)

### 13.5 Integration tests (KVM host + Caddy running)

- `test_dashboard_launch_and_access`
- `test_dashboard_data_from_kernel`
- `test_dashboard_replace`
- `test_dashboard_cleanup_on_session_delete`

These run in `tests/test_integration.py` behind `@pytest.mark.integration` and validate the complete path: Execution API → vsock guest agent → Caddy route → browser-reachable dashboard.

---

## 14. File Inventory

### New Files

| File | Description |
|------|-------------|
| `execution_api/caddy_client.py` | `CaddyClient` class + private `_vsock_request` helper |
| `config/Caddyfile` | Base Caddy configuration with admin API enabled |
| `tests/test_caddy_client.py` | Unit tests for `CaddyClient` |
| `tests/test_pool_server.py` | Pool manager API endpoint tests for kernel↔VM lookup/bind routes |

### Modified Files

| File | Change |
|------|--------|
| `guest/fc_guest_agent.py` | Add `panel_proc` global; `start_dashboard()`, `stop_dashboard()`, `_kill_proc()`; `launch_dashboard` and `stop_dashboard` branches in `handle_message()` |
| `execution_api/server.py` | Extend `SessionEntry` with `vm_ip`, `vsock_path`, `active_dashboard`; add dashboard endpoints; add cleanup on session delete; add VM lookup helper call during `create()` |
| `execution_api/models.py` | Add `DashboardRequest`, `DashboardResponse` |
| `execution_api/tool_schemas/claude.json` | Add `launch_dashboard` tool definition |
| `execution_api/tool_schemas/openai.json` | Add `launch_dashboard` tool definition |
| `fc_pool_manager/server.py` | Add bind/lookup routes: `POST /api/vms/{vm_id}/bind-kernel`, `GET /api/vms/by-kernel/{kernel_id}` |
| `fc_pool_manager/manager.py` | Add in-memory `kernel_id -> vm_id` index and cleanup on release/shutdown |
| `fc_provisioner/pool_client.py` | Add `bind_kernel(vm_id, kernel_id)` client method |
| `fc_provisioner/provisioner.py` | Bind acquired VM to `self.kernel_id` during `pre_launch()` |
| `tests/test_guest_agent.py` | Add dashboard lifecycle tests |
| `tests/test_caddy_client.py` | Add comprehensive Caddy route tests |
| `tests/test_execution_api.py` | Add dashboard endpoint/session-cleanup tests |
| `tests/test_pool_client.py` | Add `bind_kernel` client tests |
| `tests/test_integration.py` | Add dashboard integration tests |

### Unchanged Files

| File | Reason |
|------|--------|
| `guest/build_rootfs.sh` | `panel` and `bokeh` already installed |
| `sandbox_client/session.py` | Kernel execution flow remains unchanged; dashboard control is additive |
| `fc_pool_manager/vsock.py` | No protocol changes required |
