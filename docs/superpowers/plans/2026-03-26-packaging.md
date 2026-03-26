# Packaging Implementation Plan

**Goal:** Refactor dashboard architecture (dispatcher + Caddy at boot), build Docker images for Execution API + Data Analyst Agent, simplify Execution API to pure KG client.

**Spec:** `docs/superpowers/specs/2026-03-26-packaging-design.md`

**Status:** Not started.

| Chunk | Status |
|-------|--------|
| 1: Dispatcher + guest agent + rootfs (Tasks 1-2) | Not started |
| 2: Pool Manager Caddy integration (Tasks 3-4) | Not started |
| 3: Execution API simplification (Tasks 5-6) | Not started |
| 4: Dockerfiles + compose (Tasks 7-8) | Not started |
| 5: Tests + verification (Tasks 9-10) | Not started |

---

## Chunk 1: Dispatcher + Guest Agent + Rootfs

### Task 1: Create dispatcher.py and add to rootfs build

**Files:**
- Create: `guest/dispatcher.py`
- Modify: `guest/build_rootfs.sh` (copy dispatcher.py to /opt/agent/)
- Modify: `guest/fc_guest_agent.py` (start dispatcher in pre_warm_kernel)

**Steps:**

- [ ] Create `guest/dispatcher.py` with the Panel dispatcher (per spec §6)
- [ ] Update `guest/build_rootfs.sh` to copy `dispatcher.py` to `/opt/agent/dispatcher.py`
- [ ] Update `guest/fc_guest_agent.py` `pre_warm_kernel()` to start dispatcher alongside kernel
- [ ] Verify dispatcher starts: `uv run python guest/dispatcher.py` (should listen on :5006 locally — will fail without Panel, but verify import)

### Task 2: Update CaddyClient with path stripping

**Files:**
- Modify: `execution_api/caddy_client.py` (add strip_path_prefix to route)

**Steps:**

- [ ] Update `_build_route()` to include rewrite handler that strips `/dash/{route_id}` before proxying
- [ ] Update unit tests for new route structure
- [ ] Run: `uv run pytest tests/unit/test_caddy_client.py -v`

---

## Chunk 2: Pool Manager Caddy Integration

### Task 3: Move CaddyClient to Pool Manager

**Files:**
- Create: `fc_pool_manager/caddy_client.py` (copy from execution_api or shared module)
- Modify: `fc_pool_manager/manager.py` (add Caddy route in _boot_vm, remove in _destroy_vm)
- Modify: `fc_pool_manager/config.py` (add caddy_admin_url field)
- Modify: `config/fc-pool.yaml` (add caddy_admin_url)

**Steps:**

- [ ] Copy `CaddyClient` class to `fc_pool_manager/caddy_client.py` (or import from shared location)
- [ ] Add `caddy_admin_url` to `PoolConfig` (default: `http://localhost:2019`)
- [ ] In `PoolManager.__init__()`, create `CaddyClient` instance
- [ ] In `_boot_vm()`, after network reconfig + guest agent ready: `await self._caddy.add_route(vm.vm_id, f"{vm.ip}:5006")`
- [ ] In `_destroy_vm()`, before TAP teardown: `await self._caddy.remove_route(vm.vm_id)` (try/except, non-fatal)
- [ ] In `_boot_ephemeral_vm()` (golden snapshot): same Caddy registration (route gets removed when ephemeral VM is destroyed)
- [ ] Add `caddy_admin_url: http://localhost:2019` to `config/fc-pool.yaml`
- [ ] Run: `uv run pytest tests/unit/test_pool_manager.py -v` (mock CaddyClient)

### Task 4: Tests for Pool Manager Caddy integration

**Files:**
- Modify: `tests/unit/test_pool_manager.py` or new test file

**Steps:**

- [ ] Test: _boot_vm calls caddy.add_route with vm_id and ip:5006
- [ ] Test: _destroy_vm calls caddy.remove_route with vm_id
- [ ] Test: caddy.add_route failure is non-fatal (VM still usable, just no dashboard)
- [ ] Test: caddy.remove_route failure is non-fatal
- [ ] Run full unit suite: `uv run pytest tests/unit -q`

---

## Chunk 3: Execution API Simplification

### Task 5: Remove Pool Manager + Caddy dependencies from Execution API

**Files:**
- Modify: `execution_api/server.py` (remove pool_client, caddy, _lookup_vm_by_kernel)
- Modify: `execution_api/__init__.py` (remove caddy_client re-export if any)

**Steps:**

- [ ] Remove imports: `PoolClient`, `_pool_client`, `_lookup_vm_by_kernel`, `caddy`
- [ ] Remove env vars: `POOL_SOCKET`, `CADDY_ADMIN_URL`, `DASHBOARD_PORT`, `DASHBOARD_ALLOWED_ORIGINS`
- [ ] Remove `create_app()` parameter: `pool_client`
- [ ] Remove `_pool_client` module-level variable
- [ ] Rewrite `POST /sessions/{sid}/dashboard`: write code via session.execute() (atomic), return URL using `entry.vm_id`
- [ ] Rewrite `DELETE /sessions/{sid}/dashboard`: best-effort file cleanup via execute(), no Caddy call
- [ ] Simplify `SessionManager.delete()`: remove pool_client.stop_dashboard call, remove caddy.remove_route call
- [ ] Remove `_lookup_vm_by_kernel()` function
- [ ] Keep `vm_id` in `SessionEntry` (from provisioner acquire response)
- [ ] Run: `uv run pytest tests/unit/test_execution_api.py -v` (will need test updates)

### Task 6: Update Execution API tests

**Files:**
- Modify: `tests/unit/test_execution_api.py`

**Steps:**

- [ ] Remove `mock_pool_client` fixture
- [ ] Remove all `pool_client.*` assertions from dashboard tests
- [ ] Update dashboard launch test: verify session.execute() called with atomic write code
- [ ] Update dashboard launch test: verify response URL contains vm_id
- [ ] Update dashboard delete test: verify execute() called for file cleanup
- [ ] Update session delete test: no pool_client or caddy assertions
- [ ] Remove `_lookup_vm_by_kernel` mock from client fixture
- [ ] Run: `uv run pytest tests/unit/test_execution_api.py -v`
- [ ] Run full unit + service suite: `uv run pytest tests/unit tests/service -q`

---

## Chunk 4: Dockerfiles + Compose

### Task 7: Create Dockerfiles

**Files:**
- Create: `execution_api/Dockerfile`
- Create: `apps/data_analyst/Dockerfile`
- Create: `.dockerignore`

**Steps:**

- [ ] Create `execution_api/Dockerfile` per spec §3 (python:3.12-slim, copy execution_api + sandbox_client, install deps, expose 8000)
- [ ] Create `apps/data_analyst/Dockerfile` per spec §4 (python:3.12-slim, copy apps/data_analyst, install deps, expose 8501)
- [ ] Create `.dockerignore` (exclude tests, docs, .git, .venv, guest, fc_pool_manager, fc_provisioner, __pycache__)
- [ ] Build and verify: `docker build -f execution_api/Dockerfile -t fc-execution-api .`
- [ ] Build and verify: `docker build -f apps/data_analyst/Dockerfile -t fc-data-analyst .`

### Task 8: Create docker-compose.yml

**Files:**
- Create: `docker-compose.yml`

**Steps:**

- [ ] Create per spec §5 (two services: execution-api + data-analyst)
- [ ] Add `extra_hosts: host.docker.internal:host-gateway` for Linux compatibility
- [ ] Verify: `docker compose build`
- [ ] Document Linux standalone `docker run` with `--add-host` flag

---

## Chunk 5: Tests + Verification

### Task 9: Update pyproject.toml dependency groups

**Files:**
- Modify: `pyproject.toml`

**Steps:**

- [ ] Split dependencies into groups: core, api, agent, infra, dev (per spec §8)
- [ ] Verify: `uv sync --group dev` still works
- [ ] Verify: `uv sync --group api` installs only API deps
- [ ] Verify: `uv sync --group agent` installs only agent deps

### Task 10: Full verification

**Steps:**

- [ ] Local: `uv run pytest tests/unit tests/service -q` → all pass
- [ ] Docker: `docker compose up -d` → both containers start
- [ ] Docker: `curl http://localhost:8000/sessions` → API responds (will fail without KG, but verify startup)
- [ ] Docker: `curl http://localhost:8501` → Chainlit UI loads
- [ ] Remote: `deploy.sh host deploy` → all 4 services running
- [ ] Remote: `uv run pytest tests/infrastructure` → all pass (including dashboard via dispatcher)
- [ ] Remote: `deploy.sh host teardown --force` → host clean
- [ ] Commit + push + tag
