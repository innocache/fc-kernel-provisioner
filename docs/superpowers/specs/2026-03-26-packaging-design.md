# Application Packaging — Design Spec

## 1. Summary

Three independently deployable applications from the fc-kernel-provisioner monorepo:

| App | Package | Runs On | Connects To |
|-----|---------|---------|------------|
| **Data Analyst Agent** | Docker image `fc-data-analyst` | Anywhere | Execution API (:8000) |
| **Execution API** | Docker image `fc-execution-api` | Anywhere (near KG) | KG (:8888), Pool Manager (socket), Caddy (:2019) |
| **Infrastructure** | Bare metal via `deploy.sh` | Linux KVM host | Firecracker VMs, network bridge |

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
                    ┌────────────────┐ ┌──────────┐
                    │ KG :8888       │ │Caddy:8080│
                    │ (bare metal)   │ │(bare     │
                    │ WarmPool       │ │ metal)   │
                    │ Provisioner    │ └──────────┘
                    └───────┬───────┘
                            │Unix socket
                    ┌───────▼───────┐
                    │Pool Manager   │
                    │(bare metal)   │
                    │Firecracker VMs│
                    └───────────────┘
```

## 3. Docker Image: fc-execution-api

### What's included
- `execution_api/` — FastAPI server, models, caddy_client
- `sandbox_client/` — SandboxSession library (used by the API)
- Dependencies: fastapi, uvicorn, aiohttp, httpx, pydantic

### What's NOT included
- Pool manager, provisioner, guest agent (bare metal only)
- Firecracker, jailer, KVM dependencies

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
ENV POOL_SOCKET=/var/run/fc-pool.sock
ENV CADDY_ADMIN_URL=http://host.docker.internal:2019

CMD ["uvicorn", "execution_api.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

### Network connections from container

| Destination | Protocol | Default | Config |
|------------|----------|---------|--------|
| Kernel Gateway | HTTP + WebSocket | `host.docker.internal:8888` | `GATEWAY_URL` |
| Pool Manager | Unix socket | `/var/run/fc-pool.sock` | `POOL_SOCKET` + volume mount |
| Caddy admin | HTTP | `host.docker.internal:2019` | `CADDY_ADMIN_URL` |

### Volume mounts

```bash
docker run -d \
  -p 8000:8000 \
  -v /var/run/fc-pool.sock:/var/run/fc-pool.sock \
  -e GATEWAY_URL=http://host.docker.internal:8888 \
  -e CADDY_ADMIN_URL=http://host.docker.internal:2019 \
  fc-execution-api
```

The Unix socket mount is needed for the dashboard vsock proxy (pool manager routes dashboard commands to VMs). If dashboards are not needed, the socket mount can be omitted.

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
    volumes:
      - /var/run/fc-pool.sock:/var/run/fc-pool.sock
    environment:
      - GATEWAY_URL=http://host.docker.internal:8888
      - POOL_SOCKET=/var/run/fc-pool.sock
      - CADDY_ADMIN_URL=http://host.docker.internal:2019
      - DASHBOARD_PORT=5006
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

## 6. Infrastructure (App 3) — No Docker

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

## 7. pyproject.toml Changes

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

## 8. Build Commands

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

## 9. Environment Variables Reference

### fc-execution-api

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_URL` | `http://localhost:8888` | Kernel Gateway endpoint |
| `POOL_SOCKET` | `/var/run/fc-pool.sock` | Pool manager Unix socket path |
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

## 10. File Inventory

| File | New/Modify | Responsibility |
|------|-----------|---------------|
| `execution_api/Dockerfile` | New | Docker image for Execution API |
| `apps/data_analyst/Dockerfile` | New | Docker image for Data Analyst Agent |
| `docker-compose.yml` | New | Local dev: both apps + host infra |
| `.dockerignore` | New | Exclude tests, docs, .git from images |
| `pyproject.toml` | Modify | Split dependency groups |
