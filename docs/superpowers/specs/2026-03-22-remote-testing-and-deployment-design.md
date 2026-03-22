# Remote Integration Testing & Production Deployment — Design Specification

> **Date**: 2026-03-22
> **Status**: Approved
> **Approach**: Two independent shell scripts — one for testing, one for deployment

---

## 1. Overview

Two self-contained shell scripts for working with remote Linux hosts:

1. **`scripts/remote-test.sh`** — One-command integration test runner. Syncs code to a remote KVM host, sets up the environment, runs the full test suite (unit + smoke + integration), and tears down services afterward.

2. **`scripts/deploy.sh`** — Production lifecycle manager. Deploys the pool manager and Kernel Gateway as systemd services on a remote host, with subcommands for updates, restarts, status, logs, and full teardown.

The scripts are independent — neither depends on the other. Both call existing setup scripts (`setup-host.sh`, `build_rootfs.sh`, `setup_network.sh`) on the remote host.

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Script language | Bash | Matches existing project scripts (setup-host.sh, run-tests.sh) |
| SSH key management | Delegated to ~/.ssh/config | Scripts don't manage keys; SSH config is the right place |
| Test teardown scope | Services only, not host setup | Host setup is expensive and idempotent — leave it for fast re-runs |
| Deploy service management | systemd | Standard, supports restart-on-failure, journald logging |
| Script independence | Fully independent | Can iterate on either without affecting the other |

---

## 2. Prerequisites

### SSH

- Key-based SSH access to the remote host (password auth not supported)
- SSH key configured via `~/.ssh/config` or default key files (`~/.ssh/id_rsa`, `~/.ssh/id_ed25519`)
- Remote user must have passwordless `sudo`

Example `~/.ssh/config`:

```
Host fc-test
    HostName 203.0.113.10
    User ubuntu
    IdentityFile ~/.ssh/my-key.pem
```

Then use: `./scripts/remote-test.sh fc-test` or `./scripts/deploy.sh fc-test deploy`

### Remote Host

- Ubuntu 24.04 LTS
- 8+ CPU cores, 16+ GB RAM
- KVM enabled (`/dev/kvm` accessible)
- Internet access (for downloading Firecracker, Alpine packages)

---

## 3. Remote Integration Test Script

### Usage

```
scripts/remote-test.sh user@host [options]

Options:
  --skip-setup      Skip host setup and rootfs build (fast re-run after code changes)
  --keep-services   Don't tear down services after tests (for debugging)
  --unit-only       Only run unit tests on the remote host
  --help            Show usage and teardown behavior
```

### Flow

1. **Sync** — rsync project to `~/fc-kernel-provisioner` on remote host (excludes `.venv/`, `__pycache__/`, `.git/`, `.worktrees/`)

2. **Setup** (skipped with `--skip-setup`):
   - `sudo ./scripts/setup-host.sh` — install Firecracker, kernel, system deps (idempotent)
   - `uv sync --group dev` — install Python deps
   - `sudo ./guest/build_rootfs.sh` — build rootfs (skips if `/opt/firecracker/rootfs.ext4` exists)
   - `sudo ./config/setup_network.sh` — configure bridge and NAT (idempotent)

3. **Start services** (in background):
   - Install kernelspec: `sudo uv run jupyter kernelspec install config/kernelspec/ --name python3-firecracker --sys-prefix`
   - Pool manager: `sudo uv run python -m fc_pool_manager.server --config config/fc-pool.yaml --socket /var/run/fc-pool.sock -v`
   - Kernel Gateway: `uv run jupyter kernelgateway --KernelGatewayApp.default_kernel_name=python3-firecracker --KernelGatewayApp.port=8888`
   - Poll until ready (timeout 120s):
     - Pool manager: `curl -s --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status` returns HTTP 200
     - Kernel Gateway: `curl -sf http://localhost:8888/api/kernels` returns HTTP 200
   - The 120s timeout accounts for first-run pool pre-warming; subsequent runs are faster

4. **Run tests**:
   - Unit tests: `uv run pytest tests/ -v -m "not integration"`
   - Smoke test: `./scripts/run-tests.sh smoke`
   - Integration tests: `uv run pytest tests/test_integration.py -v -m integration`
   - With `--unit-only`: only unit tests

5. **Teardown** (always runs, even on test failure; skipped with `--keep-services`):
   - Capture the test exit code before teardown begins
   - Kill pool manager and Kernel Gateway processes
   - Remove Unix socket file (`/var/run/fc-pool.sock`)
   - Clean up leftover VMs (kill jailer processes, remove TAP devices, delete jail dirs under `/srv/jailer/`)
   - Implementation: use `trap 'RC=$?; teardown; exit $RC' EXIT` to preserve the test exit code even if teardown commands fail

6. **Report** — exit with the captured test result code (not the teardown exit code)

### What persists between runs

| Persists | Torn down |
|----------|-----------|
| Firecracker binaries | Pool manager process |
| Linux kernel (`vmlinux`) | Kernel Gateway process |
| Rootfs image | Unix socket file |
| Network bridge (`fcbr0`) | Running VMs / jailer processes |
| System deps (apt packages) | TAP devices |
| Python venv | Jail directories |

### Help text

The `--help` output documents:
- What each flag does
- What gets torn down after tests (services, socket, leftover VMs)
- What persists between runs (Firecracker, rootfs, bridge, venv)
- How to fully remove host setup: `ssh user@host 'sudo ~/fc-kernel-provisioner/scripts/setup-host.sh teardown'`

---

## 4. Production Deployment Script

### Usage

```
scripts/deploy.sh user@host <command>

Commands:
  deploy    Full setup: sync code, install deps, build rootfs, configure network, install + start systemd services
  update    Fast update: sync code, rebuild rootfs if guest/ changed, restart services
  stop      Stop systemd services
  start     Start systemd services
  restart   Restart systemd services
  status    Show service status, pool stats, host resources
  logs      Tail service logs (journalctl -f)
  teardown  Remove everything and restore host to pre-deployment state
```

### Subcommand Details

**`deploy`** (idempotent — safe to re-run):
1. rsync project to `~/fc-kernel-provisioner`
2. Create symlink: `sudo ln -sfn ~/fc-kernel-provisioner /opt/fc-kernel-provisioner` — systemd units use `/opt/` as a stable path; the symlink means `update` rsync to `~/` automatically propagates
3. `sudo ./scripts/setup-host.sh` — install Firecracker, kernel, system deps
4. `uv sync --group dev` — install Python deps
5. `sudo ./guest/build_rootfs.sh` — build rootfs
6. Record guest checksum: `find guest/ -type f | sort | xargs sha256sum > .guest-checksum`
7. `sudo ./config/setup_network.sh` — configure bridge
8. Install systemd units:
   - Copy `config/fc-pool-manager.service` to `/etc/systemd/system/`
   - Copy `config/fc-kernel-gateway.service` to `/etc/systemd/system/`
   - `systemctl daemon-reload`
9. `systemctl enable --now fc-pool-manager fc-kernel-gateway`
10. Wait for services to be ready (poll pool API and gateway, timeout 120s), report status

**`update`** (fast path for code changes):
1. rsync project to `~/fc-kernel-provisioner` (symlink to `/opt/` means changes propagate automatically)
2. Compute current guest checksum and compare against `.guest-checksum`; if changed (or `.guest-checksum` missing), rebuild rootfs and update the checksum file
3. `sudo systemctl restart fc-pool-manager fc-kernel-gateway`
4. Wait for services to be ready, report status

**`stop` / `start` / `restart`**:
- Thin wrappers around `sudo systemctl` for both services

**`status`**:
- `sudo systemctl status fc-pool-manager fc-kernel-gateway`
- Query pool API: `sudo curl -s --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status`
- Host resources: `free -h`, `nproc`, active VM count

**`logs`**:
- `journalctl -u fc-pool-manager -u fc-kernel-gateway -f`

**`teardown`** (destructive — prompts for confirmation):
1. Stop and disable both systemd services (`fc-pool-manager` and `fc-kernel-gateway`) — the deploy script handles both services explicitly, since `setup-host.sh teardown` only knows about `fc-pool-manager`
2. Remove systemd unit files from `/etc/systemd/system/` and `systemctl daemon-reload`
3. Clean up running VMs (kill jailers, remove TAPs, delete jail dirs)
4. `sudo ./config/setup_network.sh teardown` — remove bridge, NAT rules
5. `sudo ./guest/build_rootfs.sh --clean` — remove rootfs image
6. `sudo ./scripts/setup-host.sh teardown` — remove Firecracker, kernel, jailer user (note: this also attempts to stop `fc-pool-manager` which is already stopped — harmless)
7. Remove symlink: `sudo rm -f /opt/fc-kernel-provisioner`
8. Remove deployed code (`~/fc-kernel-provisioner`)

---

## 5. New Systemd Unit: Kernel Gateway

**`config/fc-kernel-gateway.service`**:

```ini
[Unit]
Description=Jupyter Kernel Gateway (Firecracker)
After=fc-pool-manager.service
Requires=fc-pool-manager.service

[Service]
Type=simple
ExecStartPre=/usr/bin/env uv run jupyter kernelspec install /opt/fc-kernel-provisioner/config/kernelspec/ --name python3-firecracker --sys-prefix
ExecStart=/usr/bin/env uv run jupyter kernelgateway --KernelGatewayApp.default_kernel_name=python3-firecracker --KernelGatewayApp.port=8888
Restart=on-failure
RestartSec=5
# Root required: pool manager socket is root-owned, and Kernel Gateway
# needs to start kernels via the provisioner which talks to the pool socket
User=root
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/opt/fc-kernel-provisioner

[Install]
WantedBy=multi-user.target
```

The gateway depends on the pool manager — systemd starts them in order. `--sys-prefix` installs the kernelspec into the active Python environment (not `--user` which would go to root's home).

**Path resolution**: The deploy script syncs code to `~/fc-kernel-provisioner` and creates a symlink at `/opt/fc-kernel-provisioner → ~/fc-kernel-provisioner`. Both systemd units use `/opt/` as their `WorkingDirectory` for a stable path. Since it's a symlink, `uv run` resolves `.venv` correctly through the link.

**Consistency**: The existing `fc-pool-manager.service` uses `/usr/bin/python3 -m fc_pool_manager.server` (bypassing `uv`). It should be updated to use `uv run python -m fc_pool_manager.server` for consistency with the gateway unit and to ensure the correct venv is used.

### Kernelspec directory

Move `config/kernel.json` to `config/kernelspec/kernel.json` so the `kernelspec install` command only copies the `kernel.json` file, not unrelated config files (YAML, systemd units, shell scripts).

---

## 6. Documentation Updates

### testing.md additions

**Remote Integration Testing** section:
- SSH prerequisites and `~/.ssh/config` example
- `remote-test.sh` usage and all flags
- What gets set up / torn down
- Troubleshooting (SSH connectivity, KVM access, service startup failures)

**Production Deployment** section:
- `deploy.sh` usage and all subcommands
- Systemd service details
- `update` vs `deploy` — when to use which
- Teardown behavior and confirmation prompt

---

## 7. Project Structure Changes

```
scripts/
├── setup-host.sh          # existing
├── run-tests.sh           # existing
├── remote-test.sh         # NEW — remote integration test runner
└── deploy.sh              # NEW — production deployment manager

config/
├── fc-pool-manager.service    # existing (MODIFY — switch to uv run)
├── fc-kernel-gateway.service  # NEW — Kernel Gateway systemd unit
├── fc-pool.yaml               # existing
├── kernelspec/                # NEW — dedicated kernelspec directory
│   └── kernel.json            # MOVED from config/kernel.json
└── setup_network.sh           # existing
```
