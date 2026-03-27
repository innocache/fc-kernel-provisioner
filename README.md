# Pyrobox

Secure Python code execution in [Firecracker](https://firecracker-microvm.github.io/) microVMs for LLM agents.

Give your AI agent a sandboxed Python environment where it can execute code, upload files, run data analysis, and create interactive dashboards — all inside hardware-isolated microVMs that boot in 190ms.

- **37ms** session create (pre-warmed kernel pool with snapshot restore)
- **48ms** code execution (trivial), **107ms** one-shot end-to-end
- **Hardware isolation**: every session runs in its own Firecracker VM with jailer, seccomp, cgroups, and ebtables

## Using It With LLM Agents

### Tool Definitions

Register these tools with your LLM. The agent decides when to execute code, create dashboards, or download files.

```python
tools = [
    {
        "name": "execute_python_code",
        "description": (
            "Execute Python code in an isolated sandbox. "
            "Pre-installed: numpy, pandas, matplotlib, scipy, plotly, seaborn. "
            "State persists across calls. Uploaded files are at /data/<filename>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "launch_dashboard",
        "description": (
            "Launch an interactive Panel dashboard in the sandbox and return a URL. "
            "The dashboard can access the same data and variables as execute_python_code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Panel dashboard Python code"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "download_file",
        "description": (
            "Read a file from the sandbox and send it to the user for download. "
            "First use execute_python_code to create the file (e.g., df.to_csv), "
            "then call this with the file path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path in /data/ (e.g., /data/report.csv)",
                },
            },
            "required": ["path"],
        },
    },
]
```

### The Workflow

```
User: "Analyze sales.csv"       Agent (LLM)                Execution API
  + attaches file                    |                           |
                                     |  POST /sessions           |
                                     |-------------------------->| Create session
                                     |                           |
                                     |  POST /sessions/{id}/files|
                                     |  (multipart: sales.csv)   |
                                     |-------------------------->| Upload to VM /data/
                                     |                           |
                                     |  Tool: execute_python_code|
                                     |  "pd.read_csv('/data/sales.csv')..."
                                     |-------------------------->| Execute in VM
                                     |  {stdout, images, ...}    |
                                     |<--------------------------|
                                     |                           |
                                     |  Tool: launch_dashboard   |
                                     |  "import panel as pn..."  |
                                     |-------------------------->| Deploy Panel app
                                     |  {url: "/dash/{id}/app"}  |
                                     |<--------------------------|
```

### Complete Example

```python
import anthropic
import httpx

API_URL = "http://localhost:8000"
client = httpx.Client(base_url=API_URL, timeout=120)
llm = anthropic.Anthropic()

# Create a session
sid = client.post("/sessions").json()["session_id"]

# Upload a file
with open("sales.csv", "rb") as f:
    client.post(f"/sessions/{sid}/files", files={"file": ("sales.csv", f)})

# Let the LLM analyze it
messages = [{"role": "user", "content": "Analyze the sales data and show trends"}]

response = llm.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    tools=tools,
    messages=messages,
)

# Handle tool calls
for block in response.content:
    if block.type == "tool_use":
        if block.name == "execute_python_code":
            result = client.post(
                f"/sessions/{sid}/execute",
                json={"code": block.input["code"]},
            ).json()
            print(result["stdout"])

        elif block.name == "launch_dashboard":
            result = client.post(
                f"/sessions/{sid}/dashboard",
                json={"code": block.input["code"]},
            ).json()
            print(f"Dashboard: http://localhost:8080{result['url']}")

# Clean up
client.delete(f"/sessions/{sid}")
```

### Execution API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/sessions` | POST | Create a sandbox session |
| `/sessions` | GET | List active sessions |
| `/sessions/{id}/execute` | POST | Execute code (JSON: `{"code": "..."}`) |
| `/sessions/{id}/files` | POST | Upload file (multipart) |
| `/sessions/{id}/files` | GET | List files in /data/ |
| `/sessions/{id}/files/{name}` | DELETE | Delete a file |
| `/sessions/{id}/dashboard` | POST | Launch Panel dashboard |
| `/sessions/{id}/dashboard` | DELETE | Stop dashboard |
| `/sessions/{id}` | DELETE | Destroy session and VM |
| `/execute` | POST | One-shot: create, execute, destroy |

## Data Analyst Agent

A reference implementation using [Chainlit](https://chainlit.io/). Supports file upload/download, inline matplotlib charts, embedded Panel dashboards, context window management, and session recovery.

```bash
# Anthropic
export ANTHROPIC_API_KEY=sk-ant-...
uv run --group apps chainlit run apps/data_analyst/app.py --port 8501

# OpenAI
LLM_PROVIDER=openai LLM_MODEL=gpt-4o OPENAI_API_KEY=sk-... \
  uv run --group apps chainlit run apps/data_analyst/app.py

# Local (Ollama)
LLM_PROVIDER=ollama LLM_MODEL=llama3.1 \
  uv run --group apps chainlit run apps/data_analyst/app.py
```

## Architecture

```
+------------------------------------------------------------+
|  LLM Agent / Chatbot / curl                                |
+----------------------------+-------------------------------+
                             | HTTP (REST)
+----------------------------v-------------------------------+
|  Execution API (FastAPI, port 8000)                        |
|  Sessions, file upload, code execution, dashboard launch   |
+----------------------------+-------------------------------+
                             | WebSocket (Jupyter protocol)
+----------------------------v-------------------------------+
|  Kernel Gateway + WarmPoolProvisioner (port 8888)          |
|  Pre-warmed kernel pool, ZMQ protocol translation          |
+----------------------------+-------------------------------+
                             | HTTP (pool API) + vsock
+----------------------------v-------------------------------+
|  Pool Manager (Unix socket)                                |
|  VM lifecycle, snapshots, networking, Caddy routes, metrics|
+----------------------------+-------------------------------+
                             | Firecracker API + TAP networking
+----------------------------v-------------------------------+
|  Firecracker microVMs (one per session)                    |
|  jailer (namespaces, seccomp, cgroups) + ebtables isolation|
|  ipykernel + Panel dispatcher + guest agent                |
+------------------------------------------------------------+
```

**Execution API** receives HTTP requests from agents. Manages session lifecycle, file uploads (chunked base64 via kernel execute), dashboard deployment, and TTL-based cleanup.

**Kernel Gateway** translates WebSocket to ZMQ (Jupyter wire protocol). The WarmPoolProvisioner plugin pre-acquires VMs from the pool manager so sessions start in 37ms instead of 4.7s.

**Pool Manager** maintains a warm pool of Firecracker VMs. Boots VMs from golden snapshots (190ms restore vs 4.7s cold boot), manages TAP networking and IP allocation, registers Caddy routes for dashboard proxying, and exposes Prometheus metrics.

**Firecracker microVMs** provide hardware-level isolation. Each VM runs in a jailer with separate user/pid/mount namespaces, seccomp filter, cgroup limits, and ebtables rules blocking VM-to-VM traffic. The guest agent manages the ipykernel process and a Panel dispatcher that auto-reloads dashboard apps.

## Deployment

### Requirements

- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- Linux with KVM (`/dev/kvm`)
- Firecracker v1.6.0

Unit tests run anywhere (macOS, Linux, CI) — no KVM needed.

### Deploy to a KVM Host

```bash
# Full deployment (rootfs build, service install, network setup, Caddy)
./scripts/deploy.sh user@host deploy

# Update code and restart services
./scripts/deploy.sh user@host update

# Tear everything down
./scripts/deploy.sh user@host teardown --force
```

### Configuration

Pool manager config (`config/fc-pool.yaml`):

```yaml
pool:
  size: 5                    # pre-warmed idle VMs
  max_vms: 30                # hard ceiling
  health_check_interval: 30
  vm_idle_timeout: 900       # auto-cull idle VMs (seconds)

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

| Tier | Count | Needs KVM? | What it tests |
|---|---|---|---|
| Unit | 562 | No | Models, parsers, provisioners, API endpoints |
| Service | 7 | No | Full API against fake Kernel Gateway |
| Infrastructure | 38 | Yes | Real Firecracker VMs on remote host |
| E2E | 13 | Yes | Agent + LLM integration |

```bash
# Run locally (no KVM needed)
uv run pytest tests/unit tests/service -q

# Run on remote KVM host
ssh user@host "cd pyrobox && uv run pytest tests/infrastructure/ -v"
```

## Performance

| Metric | Value |
|---|---|
| Session create (warm pool) | 37ms |
| One-shot execution | 107ms |
| Code execution (trivial) | 48ms |
| Dashboard deploy | 52ms |
| VM boot (snapshot restore) | 190ms |
| VM boot (cold) | 4,700ms |

## License

MIT
