# Testing Guide

## Quick Reference

```bash
# Unit + service tests only (no services needed, ~3s)
uv run pytest tests/unit tests/service -q

# E2e mock-LLM (needs Execution API + remote KG)
uv run pytest tests/e2e/test_agent_mock_llm.py -v

# E2e real-LLM (needs above + API key)
ANTHROPIC_API_KEY=sk-ant-... uv run pytest tests/e2e/test_agent_real_llm.py -v

# Infrastructure tests (runs on Linux host only)
uv run pytest tests/infrastructure -v

# Everything
ANTHROPIC_API_KEY=sk-ant-... uv run pytest tests/ -v
```

## Test Pyramid

```
                    ┌───────────┐
                    │  Manual   │  43 test cases
                    │  (human)  │  docs/test-plan-data-analyst.md
                  ┌─┴───────────┴─┐
                  │  Real LLM     │  5 tests (@slow)
                  │  (API key)    │  tests/e2e/test_agent_real_llm.py
                ┌─┴───────────────┴─┐
                │  E2e Mock LLM     │  8 tests
                │  (Execution API)  │  tests/e2e/test_agent_mock_llm.py
              ┌─┴───────────────────┴─┐
              │  Infrastructure       │  38 tests
              │  (all services)       │  tests/infrastructure/
            ┌─┴───────────────────────┴─┐
            │  Service                    │  25 tests
            │  (fake KG)                  │  tests/service/
          ┌─┴─────────────────────────────┴─┐
          │  Unit                              │  537 tests
          │  (no deps)                         │  tests/unit/
          └────────────────────────────────────┘
```

| Layer | Tests | Run Time | Dependencies | When to Run |
|-------|-------|----------|-------------|-------------|
| Unit | 537 | ~3s | None | Every commit |
| Service | 25 | ~0.3s | None (fake KG) | Every commit |
| Infrastructure | 38 | ~5min | All services on Linux host | After deploy |
| E2e Mock LLM | 8 | ~30s | Execution API (local) + KG (remote) | After agent changes |
| E2e Real LLM | 5 | ~90s | Above + `ANTHROPIC_API_KEY` | Manual / weekly |
| Manual | 43 | ~1hr | Above + Chainlit + browser | Pre-release |

## Architecture: Split-Host Testing

Infrastructure services run on a Linux host (KVM required for Firecracker).
Execution API, agent, and tests run on your dev machine (macOS or Linux).

```
Dev machine (macOS)                        Linux host (192.168.1.53)
┌────────────────────────────────┐         ┌───────────────────────────┐
│  pytest / Chainlit             │         │  Pool Manager (unix sock) │
│      ↓                         │         │  Kernel Gateway (:8888)   │
│  Execution API (:8000)         │         │  Caddy (:8080)            │
│      ↓ GATEWAY_URL             │ network │  Firecracker VMs          │
│      └─────────────────────────+────────>│                           │
└────────────────────────────────┘         └───────────────────────────┘
```

### Prerequisites on Linux Host

Services managed via systemd. Deploy with `scripts/deploy.sh`:

```bash
./scripts/deploy.sh xuwang@192.168.1.53
```

Verify services are running:

```bash
ssh xuwang@192.168.1.53 "sudo systemctl status fc-pool-manager fc-kernel-gateway fc-caddy --no-pager"
```

KG must be bound to `0.0.0.0:8888` (set via `--KernelGatewayApp.ip=0.0.0.0` in the service file).

### Starting the Execution API Locally

```bash
GATEWAY_URL=http://192.168.1.53:8888 \
CADDY_BASE_URL=http://192.168.1.53:8080 \
  uv run python -m execution_api.server
```

Verify: `curl http://localhost:8000/sessions` should return `[]`.

## Unit Tests (537)

No services needed. Run anywhere.

```bash
uv run pytest tests/unit -q
```

## Service Tests (25)

Uses a fake Kernel Gateway (in-process). No external services needed.

```bash
uv run pytest tests/service -q
```

5 tests skip because the fake KG can't write to `/data/` inside a real VM.

## Infrastructure Tests (38)

Run on the Linux host with all services. Auto-skip when services aren't reachable.

```bash
# On the Linux host directly:
uv run pytest tests/infrastructure -v

# Or from dev machine (requires services on remote host + Execution API locally):
EXECUTION_API_URL=http://localhost:8000 uv run pytest tests/infrastructure -v
```

## E2e Tests — Mock LLM (8)

Uses scripted LLM responses (MockProvider) but hits the real Execution API + sandbox.
Tests agent-to-sandbox wiring: file upload, code execution, images, downloads, session lifecycle.

```bash
# Start Execution API first (see above), then:
uv run pytest tests/e2e/test_agent_mock_llm.py -v
```

## E2e Tests — Real LLM (5)

Uses actual Anthropic API calls + real sandbox. Costs money per run (~$0.05).

```bash
ANTHROPIC_API_KEY=sk-ant-... uv run pytest tests/e2e/test_agent_real_llm.py -v
```

| Test | What It Proves |
|------|---------------|
| `test_llm_summarizes_uploaded_data` | Claude reads CSV, reports shape |
| `test_llm_answers_analytical_question` | Claude runs groupby, names a product |
| `test_llm_generates_working_chart` | Claude generates matplotlib code that produces images |
| `test_llm_exports_file` | Claude creates + downloads a CSV file |
| `test_llm_state_persists_across_turns` | Variables survive across chat turns |

## Manual Testing — Chainlit UI

### Setup

Three processes on the dev machine:

**Terminal 1 — Execution API:**

```bash
GATEWAY_URL=http://192.168.1.53:8888 \
CADDY_BASE_URL=http://192.168.1.53:8080 \
  uv run python -m execution_api.server
```

**Terminal 2 — Chainlit app:**

```bash
EXECUTION_API_URL=http://localhost:8000 \
CADDY_BASE_URL=http://192.168.1.53:8080 \
ANTHROPIC_API_KEY=sk-ant-... \
  uv run chainlit run apps/data_analyst/app.py --port 8501
```

**Browser:** http://localhost:8501

### Test Scenarios

| # | Scenario | Steps | What to verify |
|---|----------|-------|----------------|
| 1 | Basic chat | Type "What can you do?" | Text response, no tool calls. Session created lazily. |
| 2 | Upload + analyze | Drag CSV, ask "Summarize this data" | File uploaded, LLM reads with pandas, prints shape/dtypes/head |
| 3 | Visualization | "Create a bar chart of revenue by product" | Matplotlib chart inline in chat |
| 4 | Dashboard | "Create an interactive dashboard to explore the data" | Panel dashboard in iframe, link opens at 192.168.1.53:8080 |
| 5 | File download | "Export a summary to CSV and send it to me" | Download button in chat, file saves correctly |
| 6 | Multi-turn | "Set x = 42" then "What is x?" | Variables persist across turns |
| 7 | Error handling | "Run 1/0" | Error with traceback, LLM explains gracefully |
| 8 | Large output | "Print a 1000-row DataFrame" | Truncated in chat, no crash |

Full 43-case plan at `docs/test-plan-data-analyst.md`.

### Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_URL` | `http://localhost:8888` | Kernel Gateway WebSocket endpoint |
| `EXECUTION_API_URL` | `http://localhost:8000` | Execution API HTTP endpoint |
| `CADDY_BASE_URL` | `http://localhost:8080` | Dashboard proxy base URL |
| `ANTHROPIC_API_KEY` | (none) | Required for real LLM tests and Chainlit |
| `LLM_PROVIDER` | `anthropic` | LLM provider: `anthropic`, `openai`, `ollama` |
| `LLM_MODEL` | `claude-sonnet-4-20250514` | Model name for the selected provider |

## Troubleshooting

### E2e tests skip with "Execution API not reachable"

The Execution API isn't running locally. Start it:

```bash
GATEWAY_URL=http://192.168.1.53:8888 uv run python -m execution_api.server
```

### Session creation hangs or times out

KG's warm pool may be filling. Check pool status:

```bash
ssh xuwang@192.168.1.53 "sudo curl -sf --unix-socket /var/run/fc-pool.sock http://localhost/api/pool/status"
```

Wait for `idle > 0`, or restart the pool manager.

### "Connection refused" to 192.168.1.53:8888

KG is bound to `127.0.0.1`. Update the service file:

```bash
# In config/fc-kernel-gateway.service, ExecStart line must include:
--KernelGatewayApp.ip=0.0.0.0
```

Then redeploy: `sudo systemctl daemon-reload && sudo systemctl restart fc-kernel-gateway`

### Dashboard iframe shows "refused to connect"

Browser blocks mixed content or the Caddy port isn't reachable.
Verify: `curl http://192.168.1.53:8080` from your dev machine.

### Infrastructure tests fail with "500 Internal Server Error"

- Check pool status (see above)
- If pool has 0 idle VMs, wait for replenishment or restart pool manager
- Check KG log: `journalctl -u fc-kernel-gateway --no-pager -n 50`

### Real LLM tests skip

`ANTHROPIC_API_KEY` not set. Export it before running.

## Benchmark Scripts

```bash
# API performance profiler (all code tiers + concurrent)
GATEWAY_URL=http://192.168.1.53:8888 \
  uv run python scripts/benchmark_api.py --url http://localhost:8000 --iterations 5

# Snapshot restore vs full boot timing (run on Linux host)
sudo uv run python scripts/benchmark_snapshot.py --config config/fc-pool.yaml
```
