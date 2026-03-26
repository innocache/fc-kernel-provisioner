# Application Packaging — Design Spec

## 1. Summary

Three independently deployable applications from the fc-kernel-provisioner monorepo:

| App | Package | Runs On | Connects To |
|-----|---------|---------|------------|
| **Data Analyst Agent** | Docker image `fc-data-analyst` | Anywhere | Execution API (:8000) |
| **Execution API** | Docker image `fc-execution-api` | Anywhere | KG (:8888) only |
| **Infrastructure** | Bare metal via `deploy.sh` | Linux KVM host | Firecracker VMs, Caddy, network bridge |

Key design decisions:
- **Panel dispatcher** auto-starts at VM boot (captured in golden snapshot), dynamically loads dashboard code by mtime
- **Caddy routes registered at VM boot** by Pool Manager (not Execution API)
- **Execution API is a pure KG client** — zero infrastructure dependencies, one env var Docker image

## 2. Architecture

```
┌─────────────────────┐     ┌─────────────────────┐
│ fc-data-analyst      │     │ fc-execution-api     │
│ (Docker)             │     │ (Docker)             │
│                      │     │                      │
│ Chainlit :8501       │────►│ FastAPI :8000         │
│ LLM Provider         │HTTP │ SandboxSession       │
│                      │     │ (KG client only)     │
└──────────────────────┘     └───────────┬──────────┘
                                         │ HTTP + WebSocket
                                         ▼
                    ┌──────────────────────────────────────┐
                    │ Infrastructure (bare metal)           │
                    │                                      │
                    │  KG :8888 ←──────── WarmPool         │
                    │    │                Provisioner       │
                    │    │                    │             │
                    │  Pool Manager ──────────┘             │
                    │    │  (boot VM → register Caddy)     │
                    │    │                                  │
                    │  Caddy :8080                          │
                    │    │  /dash/{vm_id}/* → VM:5006      │
                    │    │                                  │
                    │  Firecracker VMs                      │
                    │    ├── ipykernel :5555 (pre-warmed)  │
                    │    └── dispatcher.py → Panel :5006   │
                    │        (auto-reload from /apps/)      │
                    └──────────────────────────────────────┘
```

### Why the Execution API has zero infrastructure dependencies

```
VM boot (Pool Manager):
  restore snapshot → Panel dispatcher already running
  → register Caddy route: /dash/{vm_id}/* → vm_ip:5006

Dashboard create (Execution API):
  session.execute() writes .py to /apps/ → dispatcher auto-loads
  return URL: /dash/{vm_id}/app  (route already exists)

Session delete (Pool Manager via KG provisioner):
  destroy VM → Caddy route removed → /apps/ gone with VM
```

The Execution API never touches Caddy, Pool Manager, or vsock. It only talks to KG.

### Why no Pool Manager connection from Execution API

Panel auto-starts during VM boot and is captured in the golden snapshot. Every restored VM already has Panel running on :5006. The dashboard endpoint just registers a Caddy route — a pure HTTP call. No vsock, no Unix socket, no root privilege needed.

```
BEFORE (needs Pool Manager socket):
  POST /dashboard → Exec API → Pool Mgr (vsock) → Guest Agent → start Panel → register Caddy
  
AFTER (auto-start, HTTP only):
  POST /dashboard → Exec API → register Caddy route (Panel already running)
```

## 3. Docker Image: fc-execution-api

### What's included
- `execution_api/` — FastAPI server, models, caddy_client
- `sandbox_client/` — SandboxSession library (used by the API)
- Dependencies: fastapi, uvicorn, aiohttp, httpx, pydantic

### What's NOT included
- Pool manager, provisioner, guest agent (bare metal only)
- Firecracker, jailer, KVM dependencies
- No pool_client — dashboard uses Caddy HTTP only (Panel auto-started in VM)

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY execution_api/ execution_api/
COPY sandbox_client/ sandbox_client/

RUN pip install --no-cache-dir uv && \
    uv pip install --system --no-cache \
    fastapi uvicorn aiohttp httpx pydantic prometheus_client

EXPOSE 8000

ENV GATEWAY_URL=http://host.docker.internal:8888
ENV CADDY_ADMIN_URL=http://host.docker.internal:2019

CMD ["uvicorn", "execution_api.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

### Network connections from container

| Destination | Protocol | Default | Config |
|------------|----------|---------|--------|
| Kernel Gateway | HTTP + WebSocket | `host.docker.internal:8888` | `GATEWAY_URL` |

One dependency. No volume mounts. No Caddy. No Pool Manager.

```bash
docker run -d \
  -p 8000:8000 \
  -e GATEWAY_URL=http://host.docker.internal:8888 \
  fc-execution-api
```

## 4. Docker Image: fc-data-analyst

### What's included
- `apps/data_analyst/` — Chainlit app, agent, LLM providers
- Dependencies: chainlit, anthropic, openai, httpx

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY apps/data_analyst/ apps/data_analyst/
COPY pyproject.toml uv.lock ./

RUN pip install --no-cache-dir uv && \
    uv pip install --system --no-cache \
    chainlit anthropic openai httpx

EXPOSE 8501

ENV EXECUTION_API_URL=http://host.docker.internal:8000
ENV LLM_PROVIDER=anthropic
ENV LLM_MODEL=claude-sonnet-4-20250514

CMD ["chainlit", "run", "apps/data_analyst/app.py", "--host", "0.0.0.0", "--port", "8501"]
```

### Network connections from container

| Destination | Protocol | Config |
|------------|----------|--------|
| Execution API | HTTP | `EXECUTION_API_URL` |
| LLM API (Anthropic/OpenAI) | HTTPS | `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` |

No volume mounts needed — fully stateless.

## 5. docker-compose.yml

For local development with both Docker apps + bare metal infrastructure:

```yaml
version: "3.8"

services:
  execution-api:
    build:
      context: .
      dockerfile: execution_api/Dockerfile
    ports:
      - "8000:8000"
    environment:
      - GATEWAY_URL=http://host.docker.internal:8888
    extra_hosts:
      - "host.docker.internal:host-gateway"

  data-analyst:
    build:
      context: .
      dockerfile: apps/data_analyst/Dockerfile
    ports:
      - "8501:8501"
    environment:
      - EXECUTION_API_URL=http://execution-api:8000
      - LLM_PROVIDER=${LLM_PROVIDER:-anthropic}
      - LLM_MODEL=${LLM_MODEL:-claude-sonnet-4-20250514}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
    depends_on:
      - execution-api
```

### Usage

```bash
# Prerequisites: infrastructure running on host
# (deploy.sh host deploy, or manual service start)

# Start both Docker apps
docker compose up -d

# Open chatbot
open http://localhost:8501

# Stop
docker compose down
```

### Network topology with compose

```
Browser (:8501) → data-analyst container → execution-api container (:8000)
                                                    ↓ WebSocket
                                          KG on host (:8888)
                                                    ↓ vsock
                                          Firecracker VMs
```

The `data-analyst` container talks to `execution-api` by service name (Docker DNS). The `execution-api` container talks to the KG via `host.docker.internal` (host network).

## 6. Panel Dispatcher Design

Panel auto-starts at VM boot with a **dispatcher app** that dynamically loads dashboard code written by the kernel. Caddy routes are registered by the Pool Manager at VM boot time. This eliminates ALL infrastructure dependencies from the Execution API.

### Dashboard contract

LLM-generated dashboard code must export an `app` variable (not use `.servable()`):

```python
# CORRECT — dispatcher loads this
import panel as pn
import pandas as pd

df = pd.read_parquet("/data/sales.parquet")
col = pn.widgets.Select(options=list(df.columns))
plot = pn.bind(lambda c: df[c].plot.hist(), col)
app = pn.Column(col, plot)  # ← must export 'app'
```

```python
# WRONG — .servable() doesn't work with dispatcher's exec_module
pn.Column(col, plot).servable()  # ← dispatcher can't capture this
```

### How it works

```
VM boot:
  Guest agent starts dispatcher.py → Panel listening on :5006
  (Captured in golden snapshot — zero cost on restore)

User: "Create a revenue dashboard"
  LLM → session.execute():
    1. Writes /apps/dash_abc.py with Panel code
    2. print("dashboard ready")
  Execution API → caddy.add_route(sid, vm_ip:5006)
  Execution API → return {url: "/dash/{sid}/app"}
  Browser → Caddy → VM:5006/app → dispatcher loads dash_abc.py → renders

User: "Add a date filter"
  LLM → session.execute():
    1. Writes /apps/dash_def.py (newer mtime)
  Browser refreshes → dispatcher detects newer file → reloads → instant update
```

### dispatcher.py (installed at /opt/agent/dispatcher.py in rootfs)

```python
import importlib.util
import os

import panel as pn

_current_mtime = 0
_APP_DIR = "/apps"


def get_dashboard():
    """Called per-session by Panel. Returns a fresh app object each time."""
    global _current_mtime

    try:
        apps = [f for f in os.listdir(_APP_DIR)
                if f.startswith("dash_") and f.endswith(".py")]
    except FileNotFoundError:
        return pn.pane.Markdown("# No dashboard yet\nAsk the assistant to create one.")

    if not apps:
        return pn.pane.Markdown("# No dashboard yet\nAsk the assistant to create one.")

    apps.sort(key=lambda f: os.path.getmtime(os.path.join(_APP_DIR, f)), reverse=True)
    latest = os.path.join(_APP_DIR, apps[0])
    mtime = os.path.getmtime(latest)

    try:
        spec = importlib.util.spec_from_file_location("dashboard", latest)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return pn.pane.Markdown(
            f"## Dashboard Error\n\n```\n{type(e).__name__}: {e}\n```\n\n"
            "Ask the assistant to fix the code."
        )

    app = getattr(mod, "app", None)
    if app is None:
        return pn.pane.Markdown(
            "## Dashboard Error\n\n"
            "Dashboard code must export an `app` variable.\n\n"
            "Example: `app = pn.Column(widget, plot)`"
        )

    return app


pn.serve(
    {"app": get_dashboard},
    port=5006,
    address="0.0.0.0",
    allow_websocket_origin=["*"],
    show=False,
)
```

Key design choices:
- **Fresh module per request** — no global `_current_mod`, avoids shared widget state between sessions
- **Factory pattern** — `get_dashboard()` returns a new app each time, safe for concurrent sessions
- **Error display** — bad code shows error in iframe, not crash
- **Missing `app` export** — clear message telling the LLM what to fix

### Guest agent changes

`pre_warm_kernel()` starts both kernel and dispatcher:

```python
def pre_warm_kernel() -> dict:
    # Start ipykernel (existing)
    _kernel_key = secrets.token_hex(32)
    pid = start_kernel(_DEFAULT_PORTS, _kernel_key, "0.0.0.0")

    # Start dispatcher (new)
    os.makedirs("/apps", exist_ok=True)
    subprocess.Popen([
        sys.executable, "/opt/agent/dispatcher.py",
    ], stdout=open("/tmp/panel.log", "w"), stderr=subprocess.STDOUT)

    return {"key": _kernel_key, "ports": _DEFAULT_PORTS, "pid": pid, "panel_port": 5006}
```

### Golden snapshot captures both processes

```
Boot fresh VM → guest agent → start kernel + start dispatcher
  → both running, both listening on ports
  → pause → snapshot/create → golden snapshot includes:
    - ipykernel (warm, imports loaded)
    - Panel dispatcher (warm, Bokeh loaded, listening on :5006)

Restore from snapshot → VM resumes → both alive → ready
```

### What changes in Pool Manager

`_boot_vm()` registers Caddy route after VM is ready:
```python
# After network reconfig + guest agent ready:
await self._caddy.add_route(vm.vm_id, f"{vm.ip}:5006")
```

`_destroy_vm()` removes Caddy route before teardown:
```python
await self._caddy.remove_route(vm.vm_id)
```

The Pool Manager needs a `CaddyClient` instance (same class as currently in execution_api — move to shared location or duplicate).

### What changes in Execution API

Dashboard endpoint writes code via `session.execute()` and returns the URL. No Caddy. No Pool Manager. No vsock.

```python
@app.post("/sessions/{session_id}/dashboard")
async def launch_dashboard(session_id, req: DashboardRequest):
    entry = session_manager.get(session_id)

    # Atomic write: temp file + os.replace
    app_id = uuid.uuid4().hex[:12]
    escaped = req.code.replace("\\", "\\\\").replace("'''", "\\'\\'\\''")
    await entry.session.execute(
        "import os, tempfile\n"
        "os.makedirs('/apps', exist_ok=True)\n"
        f"code = '''{escaped}'''\n"
        "tmp = tempfile.mktemp(dir='/apps', suffix='.py')\n"
        "with open(tmp, 'w') as f: f.write(code)\n"
        f"os.replace(tmp, '/apps/dash_{app_id}.py')\n"
        "print('dashboard deployed')"
    )

    # URL uses vm_id — route was registered at VM boot by Pool Manager
    return DashboardResponse(
        url=f"/dash/{entry.vm_id}/app",
        session_id=session_id,
        app_id=app_id,
    )

@app.delete("/sessions/{session_id}/dashboard")
async def stop_dashboard(session_id):
    entry = session_manager.get(session_id)
    # Best-effort file cleanup (VM destroy will clean anyway)
    try:
        await entry.session.execute(
            "import glob, os\n"
            "for f in glob.glob('/apps/dash_*.py'): os.remove(f)"
        )
    except Exception:
        pass
    entry.active_dashboard = None
    return DeleteResponse()
```

### What gets removed from Execution API

- `pool_client` import and usage — **removed**
- `_pool_client` module-level variable — **removed**
- `POOL_SOCKET` env var — **removed**
- `CADDY_ADMIN_URL` env var — **removed**
- `CaddyClient` import — **removed** (moved to Pool Manager)
- `_lookup_vm_by_kernel()` — **removed** (vm_id from provisioner acquire response)
- All vsock calls for dashboards — **removed**

### Caddy route configuration

Pool Manager registers routes with path stripping so Panel receives clean paths:

```python
# In CaddyClient.add_route():
route = {
    "@id": f"dash-{vm_id}",
    "match": [{"path": [f"/dash/{vm_id}/*"]}],
    "handle": [
        {"handler": "rewrite", "strip_path_prefix": f"/dash/{vm_id}"},
        {"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]},
    ],
}
```

Browser requests `/dash/vm-12345/app` → Caddy strips `/dash/vm-12345` → Panel receives `/app` → dispatcher serves the dashboard.

### Dashboard iteration UX

| Action | Latency | What happens |
|--------|---------|-------------|
| First dashboard | ~50ms | Write .py + register Caddy route |
| Modify dashboard | ~50ms | Write new .py (dispatcher auto-reloads on next request) |
| Replace dashboard | ~50ms | Write newer .py (dispatcher picks newest by mtime) |
| Delete dashboard | ~20ms | Remove Caddy route + clean /apps/ |

No Panel restart for any iteration. The dispatcher reloads automatically.

### Error handling

Bad dashboard code → dispatcher catches the exception → shows error in iframe:

```
## Dashboard Error

```
NameError: name 'df' is not defined
```

Ask the assistant to fix the code.
```

The user tells the LLM "fix the dashboard error", LLM generates corrected code, writes new .py, dispatcher reloads.

### Memory overhead

| Config | Per-VM Memory | Recommendation |
|--------|-------------|---------------|
| Kernel only | ~200MB | 512MB VM |
| Kernel + Panel dispatcher | ~350MB | 768MB minimum, **1GB for pandas workloads** |

Configurable via `fc-pool.yaml`:
```yaml
pool:
  auto_start_panel: true    # false to skip dispatcher
  vm_mem_mib: 1024          # 1GB for data analysis with dashboards
```

## 7. Infrastructure (App 3) — No Docker

The infrastructure components (pool manager, KG, Caddy, Firecracker) run on bare metal via systemd services managed by `deploy.sh`:

```bash
scripts/deploy.sh user@host deploy     # full setup
scripts/deploy.sh user@host update     # code sync + restart
scripts/deploy.sh user@host teardown   # complete removal
```

### Why no Docker for infrastructure

| Reason | Detail |
|--------|--------|
| KVM access | Firecracker needs `/dev/kvm` — requires `--privileged` which defeats isolation |
| Network bridge | TAP devices, ebtables, iptables — needs host network namespace |
| Jailer chroot | Creates chroot jails under `/srv/jailer/` — needs host filesystem |
| XFS reflink | Requires host XFS mount for CoW rootfs copies |
| vsock | AF_VSOCK sockets are in the host's jailer chroot paths |
| Performance | Any container overhead on the VM management path defeats the 37ms session create |

## 8. pyproject.toml Changes

Split dependency groups for independent installation:

```toml
[project]
dependencies = [
    # Core: shared by all apps
    "aiohttp>=3.9",
    "pyyaml>=6.0",
]

[dependency-groups]
api = [
    # Execution API only
    "fastapi>=0.115",
    "uvicorn>=0.34",
    "prometheus_client>=0.20",
]
agent = [
    # Data Analyst Agent only
    "chainlit>=2.0",
    "anthropic>=0.40",
    "openai>=1.0",
]
infra = [
    # Infrastructure (bare metal) only
    "jupyter_client>=7.0",
    "jupyter_kernel_gateway>=3.0",
]
dev = [
    # Testing
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-aiohttp>=1.0",
    "aioresponses>=0.7",
    "httpx>=0.27",
]
```

## 9. Build Commands

```bash
# Build both images
docker build -f execution_api/Dockerfile -t fc-execution-api .
docker build -f apps/data_analyst/Dockerfile -t fc-data-analyst .

# Or via compose
docker compose build

# Push to registry
docker tag fc-execution-api registry.example.com/fc-execution-api:v1.0.0
docker push registry.example.com/fc-execution-api:v1.0.0
```

## 10. Environment Variables Reference

### fc-execution-api

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_URL` | `http://localhost:8888` | Kernel Gateway endpoint |
| `PORT` | `8000` | API listen port |
| `SESSION_TTL` | `600` | Session idle timeout (seconds) |
| `MAX_SESSIONS` | `20` | Maximum concurrent sessions |

### fc-data-analyst

| Variable | Default | Description |
|----------|---------|-------------|
| `EXECUTION_API_URL` | `http://localhost:8000` | Execution API endpoint |
| `LLM_PROVIDER` | `anthropic` | LLM provider (anthropic/openai/ollama) |
| `LLM_MODEL` | `claude-sonnet-4-20250514` | Model name |
| `ANTHROPIC_API_KEY` | — | Required for Anthropic provider |
| `OPENAI_API_KEY` | — | Required for OpenAI provider |
| `CADDY_BASE_URL` | `http://localhost:8080` | Dashboard iframe base URL |

## 11. File Inventory

| File | New/Modify | Responsibility |
|------|-----------|---------------|
| `execution_api/Dockerfile` | New | Docker image for Execution API |
| `apps/data_analyst/Dockerfile` | New | Docker image for Data Analyst Agent |
| `docker-compose.yml` | New | Local dev: both apps + host infra |
| `.dockerignore` | New | Exclude tests, docs, .git from images |
| `pyproject.toml` | Modify | Split dependency groups |
