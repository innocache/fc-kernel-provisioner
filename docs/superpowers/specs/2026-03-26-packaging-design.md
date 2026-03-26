# Application Packaging — Design Spec

## 1. Summary

Three independently deployable applications from the fc-kernel-provisioner monorepo:

| App | Package | Runs On | Connects To |
|-----|---------|---------|------------|
| **Data Analyst Agent** | Docker image `fc-data-analyst` | Anywhere | Execution API (:8000) |
| **Execution API** | Docker image `fc-execution-api` | Anywhere | KG (:8888), Caddy (:2019) |
| **Infrastructure** | Bare metal via `deploy.sh` | Linux KVM host | Firecracker VMs, network bridge |

Key design decision: **Panel auto-starts at VM boot** (captured in golden snapshot). This eliminates the Execution API → Pool Manager dependency. Dashboard launch is just a Caddy route registration (~5ms) — no vsock, no socket mount.

## 2. Architecture

```
┌─────────────────────┐     ┌─────────────────────┐
│ fc-data-analyst      │     │ fc-execution-api     │
│ (Docker)             │     │ (Docker)             │
│                      │     │                      │
│ Chainlit :8501       │────►│ FastAPI :8000         │
│ LLM Provider         │HTTP │ SandboxSession       │
│                      │     │ CaddyClient          │
└──────────────────────┘     └───┬───────────┬──────┘
                                 │WS         │HTTP
                                 ▼           ▼
                    ┌────────────────┐ ┌──────────────┐
                    │ KG :8888       │ │Caddy :8080   │
                    │ (bare metal)   │ │(bare metal)  │
                    │ WarmPool       │ │              │
                    │ Provisioner    │ │ /dash/{sid}/ │
                    └───────┬───────┘ │  → VM:5006   │
                            │         └──────────────┘
                    ┌───────▼───────┐       ↑
                    │Pool Manager   │       │TCP
                    │(bare metal)   │       │
                    │               │ ┌─────┴──────┐
                    └───────┬───────┘ │ Panel:5006 │
                            │vsock    │ (auto-start)│
                    ┌───────▼─────────┴────────────┐
                    │ Firecracker VM                 │
                    │ ipykernel:5555 + Panel:5006   │
                    │ (both in golden snapshot)      │
                    └───────────────────────────────┘
```

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
| Caddy admin | HTTP | `host.docker.internal:2019` | `CADDY_ADMIN_URL` |

No volume mounts needed — all connections are standard TCP/HTTP.

```bash
docker run -d \
  -p 8000:8000 \
  -e GATEWAY_URL=http://host.docker.internal:8888 \
  -e CADDY_ADMIN_URL=http://host.docker.internal:2019 \
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
      - CADDY_ADMIN_URL=http://host.docker.internal:2019
      - DASHBOARD_ALLOWED_ORIGINS=localhost:8080,localhost:8501
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

## 6. Auto-Start Panel Design

### What changes in the VM boot

The guest agent's `pre_warm_kernel()` is extended to also start Panel:

```python
def pre_warm_kernel() -> dict:
    # Start ipykernel (existing)
    _kernel_key = secrets.token_hex(32)
    _kernel_ports = dict(_DEFAULT_PORTS)
    pid = start_kernel(_kernel_ports, _kernel_key, "0.0.0.0")
    
    # Start Panel (new)
    subprocess.Popen([
        sys.executable, "-m", "panel", "serve",
        "--port", "5006", "--address", "0.0.0.0",
        "--allow-websocket-origin", "*",
        "--prefix", "/dash/placeholder",
    ])
    
    return {"key": _kernel_key, "ports": _kernel_ports, "pid": pid, "panel_port": 5006}
```

### Golden snapshot captures both

```
Boot fresh VM → guest agent → start kernel + start Panel → both running
  → pause → snapshot/create → golden snapshot includes:
    - ipykernel process (warm, imports loaded)
    - Panel process (warm, Bokeh loaded)
    - Both listening on their ports

Restore from snapshot → VM resumes → both processes alive → ready
```

### What changes in Execution API

Dashboard endpoint becomes HTTP-only (no vsock):

```python
@app.post("/sessions/{session_id}/dashboard")
async def launch_dashboard(session_id, req: DashboardRequest):
    entry = session_manager.get(session_id)
    
    # Panel is already running on vm_ip:5006
    # Just register Caddy route with the dashboard code path
    app_id = uuid.uuid4().hex[:12]
    await caddy.add_route(session_id, f"{entry.vm_ip}:5006")
    
    return DashboardResponse(
        url=f"/dash/{session_id}/dash_{app_id}",
        session_id=session_id,
        app_id=app_id,
    )
```

### What gets removed from Execution API

- `pool_client` import and usage
- `_pool_client` module-level variable
- `POOL_SOCKET` env var
- All `pool_client.launch_dashboard()` / `pool_client.stop_dashboard()` calls
- Pool Manager vsock proxy endpoints (can remain in pool manager but unused by API)

### Panel prefix handling

The golden snapshot starts Panel with `--prefix /dash/placeholder`. On dashboard launch, the Caddy route maps `/dash/{session_id}/*` to the VM. Panel doesn't need the session-specific prefix at startup — Caddy handles the path rewriting.

For dashboard code deployment: the `POST /dashboard` endpoint sends the code to the VM via the **Execution API's existing code execution path** (not vsock):

```python
# Execute the dashboard code in the kernel, which writes it to Panel's watched directory
code = f'''
import os
os.makedirs("/apps", exist_ok=True)
with open("/apps/dash_{app_id}.py", "w") as f:
    f.write("""{req.code}""")
print("dashboard deployed")
'''
await entry.session.execute(code)
```

This uses the same `SandboxSession.execute()` path as code execution — no new connections needed.

### Memory overhead

| Config | Per-VM Memory | Pool of 5 | Pool of 30 |
|--------|-------------|-----------|-----------|
| Kernel only | ~200MB | ~1GB | ~6GB |
| Kernel + Panel | ~350MB | ~1.75GB | ~10.5GB |
| Delta | +150MB | +750MB | +4.5GB |

Configurable via `fc-pool.yaml`:
```yaml
pool:
  auto_start_panel: true    # false to disable
  vm_mem_mib: 768           # increase from 512 for Panel
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
| `CADDY_ADMIN_URL` | `http://localhost:2019` | Caddy admin API |
| `PORT` | `8000` | API listen port |
| `SESSION_TTL` | `600` | Session idle timeout (seconds) |
| `MAX_SESSIONS` | `20` | Maximum concurrent sessions |
| `DASHBOARD_PORT` | `5006` | Panel port inside VMs |
| `DASHBOARD_ALLOWED_ORIGINS` | `localhost:8080,127.0.0.1:8080` | WebSocket origins for Panel |

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
