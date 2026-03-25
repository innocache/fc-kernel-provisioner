# Testing Plan — Firecracker Kernel Provisioner

## Target Environment

| Resource | Value |
|----------|-------|
| OS | Ubuntu 24.04 LTS |
| CPU | 8 cores |
| RAM | 16 GB |
| KVM | Required (`/dev/kvm`) |
| Firecracker | v1.6.0 |
| Python | 3.11+ |
| Package manager | uv |

### Resource Budget

With 8 cores / 16 GB and 512 MB per VM:

| Allocation | Value |
|------------|-------|
| Host OS + services reserve | 2 GB |
| Available for VMs | 14 GB |
| Max concurrent VMs | 28 (memory-limited) |
| Recommended `max_vms` | 24 (leave headroom) |
| Recommended `pool.size` | 4 (idle pre-warmed) |
| CPU overcommit ratio | 4:1 max (32 vCPUs on 8 cores) |

Update `config/fc-pool.yaml` for your host:

```yaml
pool:
  size: 4          # idle pre-warmed VMs
  max_vms: 24      # hard ceiling
```

---

## Test Levels

### Level 1: Unit Tests (no KVM required)

Run anywhere — macOS, Linux, CI. Tests use mocks for all system interactions.

```bash
uv run pytest tests/ -v -m "not integration"
```

**515 unit tests (plus 30 integration tests).** Unit suite expected to pass in < 5 seconds.

**What's covered:**

| Module | Test files | Coverage |
|--------|-----------|----------|
| Guest agent | `test_guest_agent.py`, `test_guest_agent_edge_cases.py` | Message handling, vsock protocol, start/restart/signal/ping, malformed input, oversized messages, kernel crash on launch, missing ports/key |
| Config | `test_config.py`, `test_config_edge_cases.py` | YAML loading, validation, missing sections, empty/invalid YAML, frozen dataclass, extra fields |
| Network | `test_network.py`, `test_network_edge_cases.py` | IP allocation/release/exhaustion, TAP naming, MAC generation, double release, full subnet |
| VM state machine | `test_vm.py`, `test_vm_edge_cases.py` | All 16 state transitions (4×4 matrix), CID allocation/recycling, MAX_CID boundary, lifecycle paths |
| Pool manager | `test_pool_manager.py`, `test_pool_manager_edge_cases.py` | Acquire/release logic, concurrent acquire serialization, pool status, health check, shutdown, replenish, resource mismatch, STOPPING state handling |
| HTTP server | `test_server.py`, `test_server_edge_cases.py` | All API endpoints, error codes, pool exhaustion 503, malformed body handling |
| Firecracker API | `test_firecracker_api.py` | Request body construction |
| Vsock client | `test_vsock_client.py`, `test_vsock_client_edge_cases.py` | Message framing, encode/decode roundtrip, unicode, large payloads, truncated messages |
| Pool client | `test_pool_client.py` | HTTP client construction |
| Provisioner | `test_provisioner.py`, `test_provisioner_edge_cases.py` | Lifecycle (pre_launch, launch, cleanup), restart state reset, connection info roundtrip, signal forwarding |
| Pre-warm | `test_prewarm.py` | Pre-warm kernel after boot, get_kernel_info after restore, acquire returns kernel info, ephemeral VM pre-warms, provisioner uses pre-warm key |
| Snapshot reconfig | `test_snapshot_reconfig.py` | TAP detach/attach ordering, reconfig payload, golden TAP rename, fail-closed on reconfig failure |
| Warm pool provisioner | `test_warm_pool_provisioner.py` | Pop from pool, fallback on empty, pool size decrement, replenish adds/rejects/retries, cleanup, initialization |
| Data analyst agent | `test_data_analyst.py` | LLM providers (Anthropic/OpenAI/Ollama creation, tool format conversion, message format), agent session lifecycle, file upload (small/chunked/oversized/sanitized), file download (success/oversized/path restriction), chat loop (text/tools/images/dashboards), context compaction, result formatting |
| Output parser | `test_output_parser.py` | Jupyter message parsing → ExecutionResult. Stream/error/display_data/execute_reply handling. Mime bundle priority. Binary (PNG) vs text output. Malformed messages, edge cases |
| Sandbox session | `test_session.py` | Lifecycle (start/stop/context manager), execute message format, timeout + interrupt, WebSocket error types, artifact store integration, HTTPS→WSS, msg_id filtering |
| Artifact store | `test_artifact_store.py` | LocalArtifactStore file creation, URL generation, directory creation, overwrite, protocol compliance |
| Execution API | `test_execution_api.py` | Pydantic models (incl. dashboard), SessionManager lifecycle + edge cases (TTL cleanup, max sessions, shutdown, startup failure cleanup), result conversion (binary base64, text inline, URLs), all endpoints via httpx AsyncClient (CRUD, execute, one-shot, dashboard launch/stop/cleanup, error responses, 503/404), validation error envelope |
| Caddy client | `test_caddy_client.py` | Route add (PUT success, POST fallback on 404, server key discovery), route remove (success, 404 idempotent, server error), route ID format, route structure validation |
| Pool server (bind/lookup) | `test_pool_server.py` | Kernel-to-VM bind, lookup success/404, missing kernel_id 400, unknown VM 404, bind replaces previous |
| VM timestamps | `test_vm_timestamps.py` | created_at on construction, assigned_at set/cleared on transitions, immutability across lifecycle |
| Auto-cull | `test_auto_cull.py` | Stale VM culled, active VM preserved, disabled when timeout=0, destroy failure continues, release/cull race safety, cull metric incremented |
| Metrics | `test_metrics.py` | VM gauges (boot/acquire/release), acquire counters (success/exhausted/invalid_request), acquire duration histogram, boot duration, health check failure counter, max_vms gauge |

### Level 2: Smoke Test (manual, requires running services)

Interactive step-by-step verification of the pool manager and VM lifecycle without the Kernel Gateway. Useful for debugging infrastructure issues before running full integration.

```bash
./scripts/run-tests.sh smoke
```

**What it does:**
1. Checks pool manager is running and reports pool status
2. Acquires a VM via HTTP API, reports VM ID and IP
3. Pings the VM over the TAP network
4. Calls health check — verifies guest agent responds via vsock
5. Releases the VM
6. Verifies pool replenishes

**Does NOT require:** Kernel Gateway, jupyter_client

### Level 3: Integration Tests (full pipeline, KVM required)

End-to-end test: code execution through the entire Firecracker path via the Kernel Gateway.

```bash
uv run pytest tests/test_integration.py -v -m integration
```

**27 tests.** Expected to pass in < 2 minutes.

**What's covered:**

| Test class | Tests |
|-----------|-------|
| `TestFullPipeline` | hello_world, state persistence, error handling, imports (numpy), multiline output |
| `TestSandboxClient` | hello_world, state persistence, error handling (with traceback), rich output (matplotlib PNG), HTML output (pandas DataFrame), timeout + interrupt, artifact store (file written + URL), explicit lifecycle (start/stop), error recovery (session reuse after error), execution_count increments, stderr capture, stdout before error |
| `TestExecutionAPI` | hello_world via REST, session lifecycle (create → execute × 2 → delete, state persists), error result (ZeroDivisionError), rich output (matplotlib base64 PNG), session not found 404 |
| `TestPoolMetrics` | Live /api/metrics endpoint returns Prometheus format with pool gauges |
| `TestDashboardIntegration` | Dashboard launch + browser access, data from kernel, dashboard replace, cleanup on session delete |

**Prerequisites:** Pool manager running, Kernel Gateway running, Execution API running, Caddy running, rootfs built, network configured.

> **Note:** Run Kernel Gateway as root (`sudo`) for vsock access to jailer chroot paths.

---

## Environment Setup

### Quick Start

```bash
# 1. Setup host (installs Firecracker, kernel, system deps)
sudo ./scripts/setup-host.sh

# 2. Clone and install project
git clone git@github.com:innocache/fc-kernel-provisioner.git
cd fc-kernel-provisioner
uv sync --group dev

# 3. Build guest rootfs (~5 min)
sudo ./guest/build_rootfs.sh

# 4. Setup network bridge (once per boot)
sudo ./config/setup_network.sh

# 5. Run unit tests (verify project works)
./scripts/run-tests.sh unit

# 6. Start pool manager (terminal 1)
sudo uv run python -m fc_pool_manager.server \
    --config config/fc-pool.yaml \
    --socket /var/run/fc-pool.sock -v

# 7. Install kernelspec
uv run jupyter kernelspec install config/kernelspec/ \
    --name python3-firecracker \
    --user

# 8. Start Kernel Gateway (terminal 2)
uv run jupyter kernelgateway \
    --KernelGatewayApp.default_kernel_name=python3-firecracker \
    --KernelGatewayApp.port=8888

# 9. Start Execution API (terminal 3)
uv run python -m execution_api.server

# 10. Run smoke test
./scripts/run-tests.sh smoke

# 11. Run integration tests
./scripts/run-tests.sh integration
```

### Cleanup / Teardown

All setup scripts support full reversal:

```bash
# Remove network bridge, iptables/ebtables rules (volatile — also lost on reboot)
sudo ./config/setup_network.sh teardown

# Remove rootfs image
sudo ./guest/build_rootfs.sh --clean

# Remove Firecracker binaries, kernel, jailer dirs, user, sysctl/limits configs
sudo ./scripts/setup-host.sh teardown

# Check what's currently installed
sudo ./scripts/setup-host.sh status
```

**Host impact summary:**

| Script | What it modifies | Teardown? | Notes |
|--------|-----------------|-----------|-------|
| `build_rootfs.sh` | Only the output ext4 file | `--clean` | All work in temp dirs + chroot. Zero host pollution. |
| `setup-host.sh` | Binaries, dirs, user, sysctl, limits | `teardown` | Apt packages intentionally kept (shared utilities). |
| `setup_network.sh` | Bridge, iptables, ebtables | `teardown` | All volatile — lost on reboot anyway. |

### Detailed: `setup-host.sh`

The script at `scripts/setup-host.sh` automates:

1. **KVM verification** — checks `/dev/kvm` exists
2. **System packages** — `iproute2`, `iptables`, `ebtables`, `e2fsprogs`, `python3`
3. **uv** — installs if not present
4. **Firecracker v1.6.0** — downloads binaries to `/usr/bin/`
5. **Linux kernel** — downloads `vmlinux` to `/opt/firecracker/`
6. **Jailer user** — creates `fc-jailer` (UID 123, GID 100)
7. **System tuning** — file descriptor limits, IP forwarding
8. **Resource budget** — calculates max VMs based on host resources, writes to `/opt/firecracker/host-limits.env`

### Detailed: Network Setup

`config/setup_network.sh` creates:

- Bridge `fcbr0` at `172.16.0.1/24`
- NAT masquerade for VM internet access
- `ebtables` rule blocking VM-to-VM direct traffic

Must be re-run after each host reboot. The systemd service (`config/fc-pool-manager.service`) runs this as `ExecStartPre`.

### Detailed: Rootfs

`guest/build_rootfs.sh` creates a 512 MB ext4 image at `/opt/firecracker/rootfs.ext4` containing:

- Alpine 3.19 (minimal)
- Python 3.11 + ipykernel + jupyter-client
- numpy, pandas, matplotlib, scipy, plotly, seaborn
- Guest agent at `/usr/local/bin/fc-guest-agent`
- Init script at `/init`

Takes ~5 minutes. Only needs to be rebuilt when guest packages change.

---

## Pool Manager API

The pool manager exposes a Unix socket HTTP API:

| Endpoint | Method | Request | Response |
|----------|--------|---------|----------|
| `/api/vms/acquire` | POST | `{"vcpu": 1, "mem_mib": 512}` | `{"id": "vm-...", "ip": "172.16.0.x", "vsock_path": "..."}` |
| `/api/vms/{vm_id}` | DELETE | `{"destroy": true}` (optional body) | `{"ok": true}` |
| `/api/vms/{vm_id}/health` | GET | — | `{"alive": true, "uptime": 123.4, "kernel_alive": true}` |
| `/api/pool/status` | GET | — | `{"idle": 3, "assigned": 1, "booting": 0, "max": 30}` |

---

## Running Services for Integration Tests

### Pool Manager

```bash
# Foreground (for debugging)
sudo uv run python -m fc_pool_manager.server \
    --config config/fc-pool.yaml \
    --socket /var/run/fc-pool.sock -v

# Or via systemd
sudo cp config/fc-pool-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start fc-pool-manager
sudo journalctl -u fc-pool-manager -f
```

Verify:
```bash
curl -s --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status | python3 -m json.tool
```

### Kernel Gateway

```bash
uv run jupyter kernelgateway \
    --KernelGatewayApp.default_kernel_name=python3-firecracker \
    --KernelGatewayApp.port=8888
```

Verify:
```bash
curl -s http://localhost:8888/api/kernels | python3 -m json.tool
```

---

## Troubleshooting

### KVM not available

```bash
# Check CPU virtualization support
grep -E 'vmx|svm' /proc/cpuinfo

# Load KVM module
sudo modprobe kvm
sudo modprobe kvm_intel  # or kvm_amd

# Check permissions
ls -la /dev/kvm
sudo chmod 666 /dev/kvm  # or add user to kvm group
```

### VM fails to boot

```bash
# Check jailer directory permissions
ls -la /srv/jailer/

# Try booting manually without jailer
firecracker --api-sock /tmp/test-fc.sock &
curl --unix-socket /tmp/test-fc.sock -X PUT http://localhost/machine-config \
    -d '{"vcpu_count": 1, "mem_size_mib": 512}'
# ... (see Firecracker quickstart guide)
```

### Guest agent not responding

```bash
# Check VM console output (visible in jailer stdout)
# Try manual vsock connection
python3 -c "
import socket, struct, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('/srv/jailer/firecracker/vm-XXXX/root/v.sock')
s.send(b'CONNECT 52\n')
print(s.recv(100))
msg = json.dumps({'action': 'ping'}).encode()
s.send(struct.pack('!I', len(msg)) + msg)
hdr = s.recv(4)
length = struct.unpack('!I', hdr)[0]
print(json.loads(s.recv(length)))
"
```

### Network issues

```bash
# Verify bridge exists
ip link show fcbr0

# Check TAP devices
ip link show | grep tap-

# Test connectivity from host to VM
ping -c 1 172.16.0.2

# Check NAT rules
sudo iptables -t nat -L POSTROUTING -v
sudo iptables -L FORWARD -v

# Check ebtables VM isolation
sudo ebtables -L FORWARD
```

### Pool exhaustion

```bash
# Check pool status
curl -s --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status

# Check host resources
free -h
nproc
ls /srv/jailer/firecracker/ | wc -l  # active jail dirs
ip link show | grep tap- | wc -l     # active TAP devices
```

---

## CI Considerations

Unit tests run in standard CI without KVM. For integration tests in CI:

1. **GitHub Actions** — use self-hosted runners with KVM, or larger runners (not available on free tier)
2. **AWS CodeBuild** — custom environment with KVM support
3. **Self-hosted** — any Linux runner with `/dev/kvm`

Recommended CI config:
```yaml
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --group dev
      - run: uv run pytest tests/ -v -m "not integration"

  integration:
    runs-on: [self-hosted, kvm]  # requires KVM-enabled runner
    steps:
      - uses: actions/checkout@v4
      - run: sudo ./scripts/setup-host.sh
      - run: sudo ./guest/build_rootfs.sh
      - run: sudo ./config/setup_network.sh
      - run: uv sync --group dev
      - run: |
          sudo uv run python -m fc_pool_manager.server --config config/fc-pool.yaml &
          sleep 10
          uv run jupyter kernelgateway --default_kernel_name=python3-firecracker &
          sleep 5
          uv run python -m execution_api.server &
          sleep 2
          EXECUTION_API_URL=http://localhost:8000 uv run pytest tests/test_integration.py -v -m integration
```

---

## Remote Integration Testing

Run the full test suite on a remote KVM host from your local machine with a single command.

### Prerequisites

- **SSH access**: Key-based SSH to the remote host (password auth not supported)
- **SSH config**: Configure your key via `~/.ssh/config` or use default key files
- **Remote user**: Must have passwordless `sudo`
- **Remote host**: Ubuntu 24.04, 8+ cores, 16+ GB RAM, KVM enabled (`/dev/kvm`)

Example `~/.ssh/config`:

```
Host fc-test
    HostName 203.0.113.10
    User ubuntu
    IdentityFile ~/.ssh/my-key.pem
```

### Usage

```bash
# Full run: setup + unit + smoke + integration + teardown
./scripts/remote-test.sh fc-test

# Fast re-run after code changes (skip host setup)
./scripts/remote-test.sh fc-test --skip-setup

# Unit tests only (no services needed)
./scripts/remote-test.sh fc-test --unit-only

# Keep services running after tests (for debugging)
./scripts/remote-test.sh fc-test --keep-services
```

### What happens

1. **Sync** — rsync project to `~/fc-kernel-provisioner` on remote host
2. **Setup** — install Firecracker, Python deps, rootfs, network bridge (skipped with `--skip-setup`)
3. **Start services** — pool manager + Kernel Gateway + Execution API in background, poll until ready (120s timeout)
4. **Run tests** — unit → smoke → integration (or just unit with `--unit-only`)
5. **Teardown** — kill services, remove socket, clean up leftover VMs

### What persists between runs

| Persists | Torn down |
|----------|-----------|
| Firecracker binaries | Pool manager process |
| Linux kernel (`vmlinux`) | Kernel Gateway process |
| Rootfs image | Unix socket file |
| Network bridge (`fcbr0`) | Running VMs / jailer processes |
| System deps (apt packages) | TAP devices |
| Python venv | Jail directories |

### Troubleshooting

**SSH connection fails:**
```bash
# Test connectivity
ssh fc-test 'echo hello'

# Check key permissions
ls -la ~/.ssh/my-key.pem  # should be 600

# Verbose SSH for debugging
ssh -vv fc-test 'echo hello'
```

**Services don't start within 120s:**
- Check remote logs: `ssh fc-test 'cat /tmp/fc-pool-manager.log'`
- Check KVM: `ssh fc-test 'ls -la /dev/kvm'`
- First run takes longer due to pool pre-warming

**Tests fail but services are fine:**
- Re-run with `--keep-services` to debug interactively
- SSH in and run tests manually: `ssh fc-test 'cd ~/fc-kernel-provisioner && uv run pytest tests/test_integration.py -v -m integration -s'`

---

## Production Deployment

Deploy the pool manager and Kernel Gateway as systemd services on a remote host.

### Prerequisites

Same SSH and host requirements as remote testing (see above).

### Usage

```bash
# Full deployment (idempotent — safe to re-run)
./scripts/deploy.sh fc-prod deploy

# Fast update after code changes
./scripts/deploy.sh fc-prod update

# Service management
./scripts/deploy.sh fc-prod stop
./scripts/deploy.sh fc-prod start
./scripts/deploy.sh fc-prod restart
./scripts/deploy.sh fc-prod status
./scripts/deploy.sh fc-prod logs

# Remove everything (prompts for confirmation)
./scripts/deploy.sh fc-prod teardown
```

### deploy vs update

| | `deploy` | `update` |
|---|---------|----------|
| Sync code | ✓ | ✓ |
| Host setup (Firecracker, deps) | ✓ | — |
| Build rootfs | Always | Only if `guest/` changed |
| Network setup | ✓ | — |
| Install systemd units | ✓ | — |
| Restart services | ✓ | ✓ |

Use `deploy` for first-time setup or after infrastructure changes. Use `update` for code-only changes (faster).

### Systemd services

Two services are installed:

- **`fc-pool-manager`** — VM pool manager daemon (Unix socket API at `/var/run/fc-pool.sock`)
- **`fc-kernel-gateway`** — Jupyter Kernel Gateway on port 8888 (depends on pool manager)

Both run as `root` (required for KVM/jailer access) and auto-restart on failure.

```bash
# Check service status
sudo systemctl status fc-pool-manager fc-kernel-gateway

# View logs
sudo journalctl -u fc-pool-manager -u fc-kernel-gateway -f
```

### Teardown

`teardown` removes everything and restores the host:

1. Stop and disable systemd services
2. Remove systemd unit files
3. Kill leftover VMs, remove TAP devices, clean jail directories
4. Remove network bridge and NAT rules
5. Remove rootfs image
6. Remove Firecracker binaries, kernel, jailer user
7. Remove deployed code and `/opt/fc-kernel-provisioner` symlink

**Confirmation required** — you must type `yes` to proceed.
