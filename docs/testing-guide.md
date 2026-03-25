# Testing Guide

## Quick Reference

```bash
# Unit tests only (no services needed, ~4s)
uv run pytest tests/ -m "not integration and not slow"

# All tests except real-LLM (needs services running)
uv run pytest tests/ -m "not slow"

# Everything including real-LLM (needs services + API key)
ANTHROPIC_API_KEY=sk-ant-... uv run pytest tests/

# Remote full suite (starts all services automatically)
./scripts/remote-test.sh user@host
```

## Test Pyramid

```
                    ┌───────────┐
                    │  Manual   │  43 test cases
                    │  (human)  │  docs/test-plan-data-analyst.md
                  ┌─┴───────────┴─┐
                  │  Real LLM     │  5 tests (@slow)
                  │  (API key)    │  tests/test_data_analyst_llm.py
                ┌─┴───────────────┴─┐
                │  Integration      │  38 tests (@integration)
                │  (services)       │  tests/test_integration.py
                │                   │  tests/test_data_analyst_integration.py
              ┌─┴───────────────────┴─┐
              │  Unit                  │  526 tests
              │  (no deps)             │  36 test files
              └────────────────────────┘
```

| Layer | Tests | Run Time | Dependencies | When to Run |
|-------|-------|----------|-------------|-------------|
| Unit | 526 | ~4s | None | Every commit |
| Integration | 38 | ~5min | Pool Manager + KG + Execution API + Caddy | PR merge |
| Real LLM | 5 | ~2min | Above + `ANTHROPIC_API_KEY` | Manual / weekly |
| Manual | 43 | ~1hr | Above + browser | Pre-release |

## Unit Tests (526)

No services needed. Run anywhere.

```bash
uv run pytest tests/ -m "not integration and not slow"
```

### Coverage by Module

| Module | Test File | Tests | What's Covered |
|--------|-----------|-------|---------------|
| Pool Manager | `test_pool_manager.py` | 5 | Acquire/release lifecycle |
| Pool Manager edges | `test_pool_manager_edge_cases.py` | 26 | Exhaustion, concurrent acquire, shutdown |
| VM state | `test_vm.py`, `test_vm_edge_cases.py` | 43 | State transitions, CID allocation, timestamps |
| Network | `test_network.py`, `test_network_edge_cases.py` | 22 | TAP create/delete, IP allocation, bridge |
| Network hardening | `test_network_hardening.py` | 15 | tc rate limit, iptables whitelist, conntrack |
| Config | `test_config.py`, `test_config_edge_cases.py` | 16 | YAML parsing, defaults, edge cases |
| Firecracker API | `test_firecracker_api.py` | 6 | Machine/boot/drive/network/vsock configure |
| Snapshot | `test_snapshot.py` | 11 | Metadata validation, invalidation, file hashing |
| Snapshot reconfig | `test_snapshot_reconfig.py` | 9 | TAP detach/attach, network reconfig, fail-closed |
| Golden snapshot | `test_golden_snapshot.py` | 8 | Create, failure cleanup, ephemeral VM, ensure checks |
| Auto-cull | `test_auto_cull.py` | 9 | Stale VM cull, preserve active, disabled mode |
| Metrics | `test_metrics.py` | 12 | Gauges, counters, histograms, acquire/release |
| Pre-warm | `test_prewarm.py` | 9 | Kernel pre-warm, key/ports stored, ephemeral |
| Warm pool provisioner | `test_warm_pool_provisioner.py` | 10 | Queue pop, fallback, replenish, cleanup |
| Pool server | `test_pool_server.py`, `test_server.py`, `test_server_edge_cases.py` | 22 | HTTP handlers, bind/lookup, metrics endpoint |
| Pool client | `test_pool_client.py` | 2 | Client init, base URL |
| Provisioner | `test_provisioner.py`, `test_provisioner_edge_cases.py` | 30 | Pre-launch, launch, cleanup, connection info |
| Vsock | `test_vsock_client.py`, `test_vsock_client_edge_cases.py` | 18 | Protocol, timeout, error handling |
| Sandbox client | `test_session.py` | 28 | Start/stop, execute, WebSocket, output parsing |
| Output parser | `test_output_parser.py` | 35 | Stdout, stderr, errors, display outputs, edge cases |
| Artifact store | `test_artifact_store.py` | 7 | Save, URL generation, directory creation |
| Execution API | `test_execution_api.py` | 75 | Models, session manager, endpoints, dashboard |
| Caddy client | `test_caddy_client.py` | 8 | Route add/remove, server key discovery |
| Guest agent | `test_guest_agent.py`, `test_guest_agent_edge_cases.py` | 54 | Start/stop kernel, dashboard, reconfigure, pre-warm |
| Data analyst agent | `test_data_analyst.py` | 40 | Providers, session, upload/download, chat loop, compaction |

### 100% Public Method Coverage

Every public method (73/73) across all source packages has at least one unit test. Verified by AST-based coverage scan.

## Integration Tests (38)

Requires running services. Auto-skip when services aren't reachable.

```bash
# Start services (automated)
./scripts/remote-test.sh user@host --keep-services

# Or manually:
sudo uv run python -m fc_pool_manager.server --config config/fc-pool.yaml --socket /var/run/fc-pool.sock -v &
sudo uv run jupyter kernelgateway --KernelGatewayApp.default_kernel_name=python3-firecracker --KernelGatewayApp.port=8888 &
uv run python -m execution_api.server &
caddy run --config config/Caddyfile &

# Run integration tests
EXECUTION_API_URL=http://localhost:8000 uv run pytest tests/ -m integration -m "not slow"
```

### Coverage by Feature

| Test Class | File | Tests | What's Verified |
|-----------|------|-------|----------------|
| TestSandboxClient | `test_integration.py` | 12 | SandboxSession end-to-end: hello, state, errors, matplotlib, pandas HTML, timeout, artifacts, lifecycle, recovery, exec_count, stderr |
| TestExecutionAPI | `test_integration.py` | 5 | REST API: hello, session CRUD, errors, rich output, 404 |
| TestExecutionAPIExtended | `test_integration.py` | 6 | One-shot errors/images, custom timeout, dashboard stop, large output, concurrent sessions |
| TestPoolMetrics | `test_integration.py` | 1 | Prometheus /api/metrics endpoint |
| TestPoolStatus | `test_integration.py` | 2 | Pool status endpoint, session create latency <500ms |
| TestDashboardIntegration | `test_integration.py` | 4 | Dashboard launch+access, data from kernel, replace, cleanup |
| TestAgentAnalysisFlow | `test_data_analyst_integration.py` | 4 | Upload+execute, chart generation, multi-turn state, error reporting |
| TestAgentFileRoundTrip | `test_data_analyst_integration.py` | 2 | Upload→process→download, download nonexistent file |
| TestAgentSessionLifecycle | `test_data_analyst_integration.py` | 2 | Session create/destroy, pre-warmed imports available |

### Auto-Skip Behavior

Integration tests check TCP reachability at module load time. If services aren't running, all tests in the module are skipped (not failed):

```
$ uv run pytest tests/ -m "not slow" -q
526 passed, 38 skipped, 5 deselected
```

## Real LLM Tests (5)

Uses actual Claude API calls + real sandbox. Costs money per run.

```bash
ANTHROPIC_API_KEY=sk-ant-... uv run pytest tests/test_data_analyst_llm.py -v -m slow
```

| Test | What It Proves |
|------|---------------|
| `test_llm_summarizes_uploaded_data` | Claude reads CSV, reports row count |
| `test_llm_answers_analytical_question` | Claude runs groupby, names a product |
| `test_llm_generates_working_chart` | Claude generates matplotlib code that produces images |
| `test_llm_exports_file` | Claude creates + downloads a CSV file |
| `test_llm_state_persists_across_turns` | Variables survive across chat turns |

## Manual Tests (43)

Full test plan at `docs/test-plan-data-analyst.md`. Requires browser + running services.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
cd apps/data_analyst
uv run --group apps chainlit run app.py --port 8501
# Open http://localhost:8501
```

### Test Suites

| Suite | Tests | Focus |
|-------|-------|-------|
| LLM-in-the-loop (TC-L01 to TC-L12) | 12 | Real LLM analysis flows with uploaded data |
| UI/UX (TC-U01 to TC-U15) | 15 | Chainlit frontend: drag-drop, images, iframes, streaming |
| Error/Edge cases (TC-E01 to TC-E10) | 10 | Oversized files, malicious code, infinite loops, path traversal |
| Multi-LLM (TC-M01 to TC-M06) | 6 | Claude, GPT-4o, Ollama, invalid keys, rate limiting |

## Remote Test Runner

`scripts/remote-test.sh` automates the full integration test flow on a KVM host:

```bash
# First time: sets up host (Firecracker, rootfs, network, Caddy, XFS)
./scripts/remote-test.sh user@host

# Subsequent: skip setup, just run tests
./scripts/remote-test.sh user@host --skip-setup

# Keep services running after tests (for manual testing or benchmarks)
./scripts/remote-test.sh user@host --skip-setup --keep-services
```

The script:
1. Syncs code to remote host
2. Ensures XFS mount for reflink support
3. Installs kernelspec
4. Starts pool manager, Kernel Gateway, Execution API, Caddy
5. Polls until all services are ready (120s timeout)
6. Runs unit tests + integration tests
7. Teardown: kills all services, cleans up VMs

## Benchmark Scripts

```bash
# API performance profiler (all code tiers + concurrent)
uv run python scripts/benchmark_api.py --url http://localhost:8000 --iterations 5

# Snapshot restore vs full boot timing
sudo uv run python scripts/benchmark_snapshot.py --config config/fc-pool.yaml

# Generate sample data for manual testing
uv run python scripts/generate_test_data.py
```

## Troubleshooting

### Integration tests fail with "500 Internal Server Error"
- Check pool status: `sudo curl -sf --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status`
- If pool has 0 idle VMs, wait for replenishment or restart pool manager
- Check KG log: `tail /tmp/fc-kernel-gateway.log`

### Tests hang on remote host
- Kill zombie pytest processes: `ssh host "pkill -9 -f pytest"`
- Check for orphan Firecracker processes: `ssh host "pgrep -f firecracker | wc -l"`

### "Invalid Signature" in KG log
- HMAC key mismatch between provisioner and kernel
- Restart the Kernel Gateway: `sudo pkill -f kernelgateway; sleep 2; sudo .venv/bin/jupyter-kernelgateway ...`

### Integration tests skip locally
- Expected behavior — tests auto-skip when services aren't running
- Run `uv run pytest tests/ -m "not slow"` to see: `526 passed, 38 skipped`
