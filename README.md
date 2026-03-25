# fc-kernel-provisioner

A Jupyter kernel provisioner that runs Python code inside [Firecracker](https://firecracker-microvm.github.io/) microVM sandboxes. Built for LLM-powered chatbots that need secure, isolated code execution with stdout/stderr capture.

## How It Works

```
Kernel Gateway ──→ FirecrackerProvisioner ──→ Pool Manager ──→ Jailed Firecracker VM
                   (Jupyter plugin)           (asyncio daemon)   (ipykernel via vsock)
```

1. **Pool Manager** maintains a warm pool of jailed Firecracker microVMs for ~37ms session creation, with snapshot restore for ~190ms VM boot
2. **WarmPoolProvisioner** (a Jupyter `KernelProvisionerBase` plugin) acquires pre-warmed kernels so sessions skip the ~780ms in-VM ipykernel startup path
3. The **Kernel Gateway** talks to the kernel as if it were a local process — standard Jupyter protocol, no modifications

Each VM is fully isolated: separate cgroups, user/pid/mount namespaces, seccomp filter (via jailer), and ebtables rules blocking VM-to-VM traffic.

## Architecture

```
fc-kernel-provisioner/
├── fc_provisioner/          # Jupyter kernel provisioner plugin
│   ├── provisioner.py       # FirecrackerProvisioner + FirecrackerProcess
│   ├── warm_pool.py         # WarmPoolProvisioner (warm kernel pool)
│   ├── pool_client.py       # Async HTTP client for pool manager
│   └── vsock_client.py      # Length-prefixed JSON over AF_VSOCK
│
├── fc_pool_manager/         # Pool manager daemon
│   ├── manager.py           # VM lifecycle, pool maintenance, health checks
│   ├── vm.py                # VMInstance, VMState, CIDAllocator
│   ├── network.py           # TAP creation, IP allocation, MAC generation
│   ├── config.py            # YAML config loader
│   ├── snapshot.py          # Golden snapshot management
│   ├── metrics.py           # Prometheus metric definitions
│   ├── firecracker_api.py   # Firecracker REST client
│   └── server.py            # aiohttp Unix socket API
│
├── guest/                   # Guest VM contents
│   ├── fc_guest_agent.py    # Vsock agent (port 52, manages ipykernel + Panel dashboards)
│   ├── init.sh              # PID 1 init script
│   └── build_rootfs.sh      # Builds Alpine rootfs with Python + data science libs
│
├── config/
│   ├── fc-pool-manager.service    # systemd unit (pool manager)
│   ├── fc-kernel-gateway.service  # systemd unit (Kernel Gateway)
│   ├── fc-pool.yaml               # Pool manager configuration
│   ├── kernelspec/
│   │   └── kernel.json            # Jupyter kernelspec
│   └── setup_network.sh           # Host bridge + NAT setup (with teardown mode)
│
├── scripts/
│   ├── setup-host.sh        # Host setup (with teardown + status modes)
│   ├── run-tests.sh         # Test runner (unit/smoke/integration)
│   ├── remote-test.sh       # Remote integration test runner
│   ├── benchmark_api.py     # API performance profiler
│   ├── benchmark_snapshot.py # Snapshot restore benchmark
│   └── deploy.sh            # Production deployment manager
│
├── sandbox_client/         # Python client library for chatbot backends
│   ├── session.py          # SandboxSession — execute code, get structured results
│   ├── output.py           # OutputParser, ExecutionResult, DisplayOutput
│   └── artifact_store.py   # ArtifactStore protocol, LocalArtifactStore
│
├── execution_api/          # REST API server for chatbot integration
│   ├── server.py           # FastAPI app, SessionManager, endpoints
│   ├── models.py           # Pydantic request/response models
│   ├── caddy_client.py     # Dynamic Caddy route management for dashboards
│   └── tool_schemas/       # Claude and OpenAI tool definitions
│
├── examples/               # Runnable chatbot integration examples
│   ├── oneshot_example.py  # Single-turn Claude + SandboxSession
│   └── conversation_example.py  # Multi-turn with persistent session
│
├── config/
│   ├── Caddyfile                # Caddy reverse proxy config (dashboard routing)
│   └── ...
│
└── tests/                   # 486 unit + 30 integration tests
```

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- Linux with KVM (`/dev/kvm`) for running VMs
- Firecracker v1.6.0

Unit tests run anywhere (macOS, Linux, CI) — no KVM needed.

## Quick Start

```bash
# Install project
git clone https://github.com/innocache/fc-kernel-provisioner.git
cd fc-kernel-provisioner
uv sync --group dev

# Run unit tests (works anywhere)
uv run pytest tests/ -v -m "not integration"
```

### Full Setup (Linux with KVM)

```bash
# 1. Setup host (Firecracker, kernel, system deps)
sudo ./scripts/setup-host.sh

# 2. Build guest rootfs (~5 min)
sudo ./guest/build_rootfs.sh

# 3. Setup network bridge
sudo ./config/setup_network.sh

# 4. Start pool manager
sudo uv run python -m fc_pool_manager.server \
    --config config/fc-pool.yaml \
    --socket /var/run/fc-pool.sock -v

# 5. Install kernelspec + start Kernel Gateway
uv run jupyter kernelspec install config/kernelspec/ --name python3-firecracker --user
uv run jupyter kernelgateway \
    --KernelGatewayApp.default_kernel_name=python3-firecracker \
    --KernelGatewayApp.port=8888

# 6. Run integration test
uv run pytest tests/test_integration.py -v -m integration
```

## Sandbox Client

A Python client library for chatbot backends to execute code and get structured results:

```python
from sandbox_client import SandboxSession

async with SandboxSession("http://localhost:8888") as session:
    result = await session.execute("print('hello')")
    print(result.stdout)      # "hello\n"
    print(result.success)     # True

    # Rich output (images, HTML)
    result = await session.execute("import matplotlib.pyplot as plt; plt.plot([1,2,3]); plt.show()")
    print(result.outputs[0].mime_type)  # "image/png"
    print(type(result.outputs[0].data)) # <class 'bytes'>

    # Error handling
    result = await session.execute("1/0")
    print(result.error.name)  # "ZeroDivisionError"
```

Optional artifact storage for URL-based output delivery:

```python
from sandbox_client import SandboxSession, LocalArtifactStore

store = LocalArtifactStore(base_dir="/var/lib/artifacts", url_prefix="http://localhost:8080/artifacts")
async with SandboxSession("http://localhost:8888", artifact_store=store) as session:
    result = await session.execute("...")
    print(result.outputs[0].url)  # "http://localhost:8080/artifacts/{session_id}/output_0.png"
```

## Execution API

A REST API server for chatbot integration. Wraps `SandboxSession` with server-managed sessions:

```bash
# Start the API server (requires Kernel Gateway running)
uv run python -m execution_api.server
```

| Endpoint | Method | Description |
|----------|--------|-------------|
| `POST /sessions` | POST | Create a sandbox session |
| `GET /sessions` | GET | List active sessions |
| `POST /sessions/{id}/execute` | POST | Execute code in session |
| `POST /sessions/{id}/dashboard` | POST | Launch Panel dashboard in session |
| `DELETE /sessions/{id}/dashboard` | DELETE | Stop dashboard |
| `DELETE /sessions/{id}` | DELETE | Destroy session |
| `POST /execute` | POST | One-shot: create + execute + destroy |

```python
import httpx

# One-shot execution
resp = httpx.post("http://localhost:8000/execute", json={"code": "print('hello')"})
print(resp.json()["stdout"])  # "hello\n"

# Session-based (state persists)
session = httpx.post("http://localhost:8000/sessions").json()
sid = session["session_id"]
httpx.post(f"http://localhost:8000/sessions/{sid}/execute", json={"code": "x = 42"})
resp = httpx.post(f"http://localhost:8000/sessions/{sid}/execute", json={"code": "print(x)"})
print(resp.json()["stdout"])  # "42\n"
```

Tool schemas for Claude and OpenAI (`execute_python_code` + `launch_dashboard`) are in `execution_api/tool_schemas/`. See `examples/` for complete chatbot integration scripts. Dashboards run inside the Firecracker VM (Panel-in-VM) and are served to browsers through Caddy reverse proxy at `/dash/{session_id}/`.

## Data Analyst Agent

Interactive data analysis chatbot powered by LLMs + sandboxed Python execution.

```bash
# Prerequisites: Execution API + Kernel Gateway + Pool Manager running
export ANTHROPIC_API_KEY=sk-ant-...
cd apps/data_analyst
uv run --group apps chainlit run app.py --port 8501
```

Supports any LLM with tool calling:
```bash
# OpenAI
LLM_PROVIDER=openai LLM_MODEL=gpt-4o OPENAI_API_KEY=sk-... uv run --group apps chainlit run app.py

# Local (Ollama)
LLM_PROVIDER=ollama LLM_MODEL=llama3.1 uv run --group apps chainlit run app.py
```

Features: file upload/download, inline matplotlib charts, embedded Panel dashboards, context window management, session recovery.

## Pool Manager API

The pool manager exposes a Unix socket HTTP API at `/var/run/fc-pool.sock`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/vms/acquire` | POST | Acquire an idle VM (`{vcpu, mem_mib}` → `{id, ip, vsock_path}`) |
| `/api/vms/{id}` | DELETE | Release a VM (`{destroy: bool}`) |
| `/api/vms/{id}/health` | GET | Health check via vsock ping |
| `/api/vms/{id}/bind-kernel` | POST | Bind kernel_id to VM for dashboard routing |
| `/api/vms/by-kernel/{kernel_id}` | GET | Lookup VM by kernel_id |
| `/api/pool/status` | GET | Pool stats (`{idle, assigned, booting, max}`) |
| `/api/metrics` | GET | Prometheus metrics |

## Configuration

Pool manager config (`config/fc-pool.yaml`):

```yaml
pool:
  size: 5              # pre-warmed idle VMs
  max_vms: 30          # hard ceiling
  health_check_interval: 30
  vm_idle_timeout: 600     # auto-cull idle assigned VMs

vm_defaults:
  vcpu: 1
  mem_mib: 512
  kernel: /opt/firecracker/vmlinux
  rootfs: /opt/firecracker/rootfs.ext4

network:
  bridge: fcbr0
  subnet: "172.16.0.0/24"
  gateway: "172.16.0.1"

jailer:
  enabled: true
  chroot_base: /srv/jailer
  exec_path: /usr/bin/firecracker
  uid: 123
  gid: 100
```

## Testing

See [docs/testing.md](docs/testing.md) for the full testing plan.

```bash
# Unit tests (486 tests, no KVM required)
uv run pytest tests/ -v -m "not integration"

# Smoke test (requires running services)
./scripts/run-tests.sh smoke

# Integration tests (full pipeline)
./scripts/run-tests.sh integration
```

### Remote Testing & Deployment

```bash
# Run full test suite on a remote KVM host
./scripts/remote-test.sh user@host

# Starts pool manager, Kernel Gateway, Execution API, and Caddy automatically

# Deploy as systemd services
./scripts/deploy.sh user@host deploy
```

See [docs/testing.md](docs/testing.md) for full details.

## Host Cleanup

All setup scripts support teardown:

```bash
sudo ./scripts/setup-host.sh teardown    # Remove Firecracker, kernel, jailer user
sudo ./config/setup_network.sh teardown   # Remove bridge, NAT rules
sudo ./guest/build_rootfs.sh --clean      # Remove built rootfs image
```

## Status

All **8 spec components are complete**: sandboxed Python execution in Firecracker microVMs, structured result capture (stdout, stderr, errors, images, HTML) via `sandbox_client`, REST API (`execution_api`) with server-managed sessions, Prometheus metrics, VM auto-cull, interactive Panel dashboards served via Caddy, and Claude/OpenAI tool schemas. All GitHub issues are closed.

Session create is 37ms (25× optimized from 1,133ms). See [docs/performance-enhancements.md](docs/performance-enhancements.md) for details.

## License

MIT
