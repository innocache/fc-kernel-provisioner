# Chatbot Integration — Design Specification

> **Date**: 2026-03-23
> **Status**: Approved
> **Approach**: REST Execution API + Tool Schemas + Python Examples

---

## 1. Overview

Enable any chatbot — Python or otherwise — to execute Python code in Firecracker sandboxes with minimal integration effort. Three deliverables:

1. **Execution API** — A FastAPI server wrapping `SandboxSession` with server-managed sessions, TTL cleanup, and a one-shot convenience endpoint.
2. **Tool schemas** — JSON tool definitions for Claude (`tool_use`) and OpenAI (`function_calling`) that chatbot backends feed to their LLM so it knows the sandbox exists.
3. **Python examples** — Complete working scripts showing Claude API + `SandboxSession` for one-shot and conversation-level usage.

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Protocol | REST (FastAPI) | Universal — any language, any framework, testable with curl |
| Session management | Server-managed with TTL | Matches conversation pattern; prevents resource leaks |
| Tool count | Single `execute_python_code` tool | LLMs work best with fewer tools; session management is backend logic, not LLM logic |
| MCP | Deferred | Thin adapter (~50 lines) over REST; add when an MCP client needs it |
| Authentication | None | Single-tenant, same as rest of the project |
| Streaming | Not included | Batch execution model matches project design |

### What This Does NOT Include

- A complete chatbot application (that's the consumer's product)
- MCP server (add later if needed)
- Frontend/UI
- Authentication or multi-tenancy

---

## 2. REST Execution API

### Package Structure

```
execution_api/
├── __init__.py
├── server.py          # FastAPI app, endpoints, session manager
├── models.py          # Pydantic request/response models
└── tool_schemas/
    ├── claude.json    # Claude tool_use format
    └── openai.json    # OpenAI function_calling format
```

Four files, each under ~150 lines. `server.py` imports from `sandbox_client` — no circular dependencies.

### Endpoints

| Endpoint | Method | Request | Response | Purpose |
|----------|--------|---------|----------|---------|
| `POST /sessions` | POST | `{"execution_timeout": 30}` (optional) | `{"session_id": "...", "created_at": "2026-03-23T12:00:00Z"}` | Create a sandbox session |
| `POST /sessions/{id}/execute` | POST | `{"code": "..."}` | `ExecuteResponse` (see below) | Execute code in session |
| `DELETE /sessions/{id}` | DELETE | — | `{"ok": true}` | Destroy session |
| `GET /sessions` | GET | — | `[{"session_id": "...", "created_at": "2026-...", "last_active": "2026-..."}]` | List active sessions |
| `POST /execute` | POST | `{"code": "...", "timeout": 30}` | `ExecuteResponse` | One-shot: create + execute + destroy |

`POST /sessions` accepts an optional `execution_timeout` (seconds) that overrides the server's `DEFAULT_TIMEOUT` for all `execute()` calls within this session. The server-wide `SESSION_TTL` (idle timeout for cleanup) is not configurable per-session.

`POST /execute` accepts `timeout` which is passed directly to `SandboxSession.execute()` as the execution timeout. It does not limit session creation/teardown time.

All datetime fields in API responses use ISO 8601 format (e.g., `"2026-03-23T12:00:00Z"`). Internally stored as `float` (Unix timestamp), converted by Pydantic serialization.

### Execute Response Format

```json
{
  "success": true,
  "stdout": "hello\n",
  "stderr": "",
  "error": null,
  "outputs": [
    {
      "mime_type": "image/png",
      "data_b64": "iVBORw0KGgo...",
      "url": "http://localhost:8080/artifacts/session-id/output_0.png"
    },
    {
      "mime_type": "text/html",
      "data": "<table>...</table>",
      "url": null
    }
  ],
  "execution_count": 1
}
```

Binary outputs (PNG) are base64-encoded as `data_b64`. Text outputs are inline as `data`. If an artifact store is configured, `url` is also populated.

### Output Item Pydantic Model

```python
class OutputItem(BaseModel):
    mime_type: str
    data: str | None = None       # text outputs (HTML, SVG, JSON, plain text)
    data_b64: str | None = None   # binary outputs (PNG), base64-encoded
    url: str | None = None        # populated when ArtifactStore is configured
```

Exactly one of `data` or `data_b64` is populated per output item. The server checks `isinstance(display_output.data, bytes)` to decide which field to use.

### Error Response

```json
{
  "success": false,
  "stdout": "partial output before crash\n",
  "stderr": "",
  "error": {
    "name": "ZeroDivisionError",
    "value": "division by zero",
    "traceback": ["Traceback ...", "  File ...", "ZeroDivisionError: division by zero"]
  },
  "outputs": [],
  "execution_count": 3
}
```

### Session Manager

The server holds `dict[str, SessionEntry]` internally:

```python
@dataclass
class SessionEntry:
    session: SandboxSession
    session_id: str
    created_at: float
    last_active: float
```

- Session IDs are UUID4
- Background task runs every 60 seconds, destroys sessions idle longer than TTL. Cleanup calls `session.stop()` with errors suppressed — the entry is removed regardless of whether `stop()` succeeds (same pattern as `SandboxSession.__aexit__`).
- `POST /sessions` returns 503 if max sessions reached
- `DELETE /sessions/{id}` calls `session.stop()` (errors suppressed) and removes the entry
- Server shutdown (`lifespan`) destroys all active sessions (errors suppressed)

### One-Shot Endpoint

`POST /execute` is sugar over create + execute + destroy:

1. Create a temporary `SandboxSession`
2. `start()` + `execute(code)` + `stop()`
3. Return the result
4. No session entry stored — fully stateless from the server's perspective

---

## 3. Tool Schemas

### Design: One Tool, Not Three

The LLM sees a single `execute_python_code` tool. Session management (create, reuse, destroy) is handled by the chatbot backend — the LLM just says "run this code."

**Where session decisions live:**
- **One-shot chatbot:** Backend calls `POST /execute` per tool invocation
- **Conversational chatbot:** Backend creates a session at conversation start, calls `POST /sessions/{id}/execute` for each tool invocation, destroys the session when the conversation ends
- Either way, the LLM calls the same tool with the same schema

### Claude Format (`tool_schemas/claude.json`)

```json
[
  {
    "name": "execute_python_code",
    "description": "Execute Python code in an isolated Firecracker microVM sandbox. Use this when the user asks you to run code, analyze data, create visualizations, or perform calculations. The sandbox has numpy, pandas, matplotlib, scipy, plotly, and seaborn pre-installed. State persists across calls within the same conversation.",
    "input_schema": {
      "type": "object",
      "properties": {
        "code": {
          "type": "string",
          "description": "Python code to execute"
        }
      },
      "required": ["code"]
    }
  }
]
```

### OpenAI Format (`tool_schemas/openai.json`)

```json
[
  {
    "type": "function",
    "function": {
      "name": "execute_python_code",
      "description": "Execute Python code in an isolated Firecracker microVM sandbox. Use this when the user asks you to run code, analyze data, create visualizations, or perform calculations. The sandbox has numpy, pandas, matplotlib, scipy, plotly, and seaborn pre-installed. State persists across calls within the same conversation.",
      "parameters": {
        "type": "object",
        "properties": {
          "code": {
            "type": "string",
            "description": "Python code to execute"
          }
        },
        "required": ["code"]
      }
    }
  }
]
```

---

## 4. Python Examples

Two complete, runnable example scripts demonstrating `SandboxSession` used directly as a library (no REST API needed for Python chatbots).

### `examples/oneshot_example.py`

Single-turn flow: user asks a question → LLM writes code → sandbox executes → LLM responds with result.

```python
import asyncio
import anthropic
from sandbox_client import SandboxSession

TOOL_DEFINITION = {
    "name": "execute_python_code",
    "description": "Execute Python code in an isolated sandbox...",
    "input_schema": {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Python code to execute"}},
        "required": ["code"],
    },
}

def format_result(result):
    """Convert ExecutionResult to a string for the LLM."""
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]: {result.stderr}")
    if result.error:
        parts.append(f"[error]: {result.error.name}: {result.error.value}")
    for i, output in enumerate(result.outputs):
        if output.url:
            parts.append(f"[output {i}]: {output.mime_type} at {output.url}")
        elif isinstance(output.data, str):
            # Text outputs (HTML, SVG, JSON) — inline for the LLM
            parts.append(f"[output {i}]: {output.mime_type}\n{output.data}")
        else:
            # Binary outputs (PNG) — just report the size
            parts.append(f"[output {i}]: {output.mime_type} ({len(output.data)} bytes)")
    return "\n".join(parts) or "(no output)"

async def main():
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        tools=[TOOL_DEFINITION],
        messages=[{"role": "user", "content": "What's the 100th Fibonacci number?"}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "execute_python_code":
            async with SandboxSession("http://localhost:8888") as session:
                result = await session.execute(block.input["code"])

            tool_result = format_result(result)

            final = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                tools=[TOOL_DEFINITION],
                messages=[
                    {"role": "user", "content": "What's the 100th Fibonacci number?"},
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": block.id, "content": tool_result},
                    ]},
                ],
            )
            print(final.content[0].text)

asyncio.run(main())
```

### `examples/conversation_example.py`

Multi-turn flow: session persists across the conversation, variables and imports carry over between turns.

```python
import asyncio
import anthropic
from sandbox_client import SandboxSession

# Same TOOL_DEFINITION and format_result() as above

async def main():
    client = anthropic.Anthropic()
    messages = []

    session = SandboxSession("http://localhost:8888")
    await session.start()

    try:
        while True:
            user_input = input("> ")
            if user_input.lower() in ("exit", "quit"):
                break

            messages.append({"role": "user", "content": user_input})

            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                tools=[TOOL_DEFINITION],
                messages=messages,
            )

            # Handle tool use loop (LLM may call tool multiple times)
            while response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        result = await session.execute(block.input["code"])
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": format_result(result),
                        })

                messages.append({"role": "user", "content": tool_results})
                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=4096,
                    tools=[TOOL_DEFINITION],
                    messages=messages,
                )

            # Print final text response
            messages.append({"role": "assistant", "content": response.content})
            for block in response.content:
                if hasattr(block, "text"):
                    print(block.text)
    finally:
        await session.stop()

asyncio.run(main())
```

### What Both Examples Include

- Full `format_result()` helper: stdout, stderr, errors with name/value, text outputs inlined, binary output size reported
- Claude tool definition inline
- Proper error handling (connection errors raised, execution errors in result)
- Complete and runnable

### What Examples Do NOT Include

- OpenAI example (schema file provided; wiring is analogous)
- Streaming
- Production hardening (auth, rate limiting)

---

## 5. Error Handling

### Execution API HTTP Status Codes

| Scenario | HTTP Status | Body |
|----------|-------------|------|
| Code execution error (exception, syntax) | **200** | `{"success": false, "error": {...}}` |
| Code timeout | **200** | `{"success": false, "error": {"name": "TimeoutError", ...}}` |
| Session not found | 404 | `{"error": "session not found"}` |
| Max sessions reached | 503 | `{"error": "max sessions reached"}` |
| No VMs available (pool exhausted) | 503 | `{"error": "no VMs available"}` |
| Invalid request body | 422 | FastAPI validation error |

**Key distinction:** Execution errors are 200s — the API call succeeded, the code failed. Infrastructure errors are 4xx/5xx.

### Python Library Error Handling

Same as existing `SandboxSession` behavior:
- Connection errors (unreachable KG, pool exhausted, WebSocket drop) → raised as `RuntimeError` / `ConnectionError`
- Execution errors (syntax, runtime exception, timeout) → returned in `result.error`, `result.success == False`
- Session survives execution errors; only unusable if kernel dies

---

## 6. Configuration

Environment variables for the Execution API server:

| Variable | Default | Purpose |
|----------|---------|---------|
| `GATEWAY_URL` | `http://localhost:8888` | Kernel Gateway URL |
| `SESSION_TTL` | `600` | Idle session timeout in seconds |
| `MAX_SESSIONS` | `20` | Max concurrent sessions |
| `DEFAULT_TIMEOUT` | `30` | Default code execution timeout in seconds |
| `ARTIFACT_BASE_DIR` | None (disabled) | Path to artifact storage directory |
| `ARTIFACT_URL_PREFIX` | None | URL prefix for artifact URLs |
| `SERVE_ARTIFACTS` | `true` | Serve artifact files via `/artifacts/` endpoint (when artifact store is configured) |
| `PORT` | `8000` | API server port |

**Running:**
```bash
uv run python -m execution_api.server
# or with config:
GATEWAY_URL=http://kg:8888 MAX_SESSIONS=50 uv run python -m execution_api.server
```

### Artifact Serving

When `ARTIFACT_BASE_DIR` is set, the Execution API mounts a static file route at `/artifacts/` using FastAPI's `StaticFiles`. Artifact URLs in responses use `ARTIFACT_URL_PREFIX` which defaults to `http://localhost:{PORT}/artifacts` when not explicitly set. This means the API server serves artifact files directly — no separate file server needed.

Example: with `ARTIFACT_BASE_DIR=/var/lib/artifacts` and `PORT=8000`, a PNG output gets:
- Saved to `/var/lib/artifacts/{session_id}/output_0.png`
- URL: `http://localhost:8000/artifacts/{session_id}/output_0.png`

For production deployments behind a CDN or reverse proxy, set `ARTIFACT_URL_PREFIX` to the external URL.

---

## 7. Testing

### Unit Tests (mocked, no KVM needed)

| Test file | Validates |
|-----------|-----------|
| `test_execution_api.py` | All endpoints: create/list/delete sessions, execute in session, one-shot execute. Session TTL cleanup. Max sessions limit. Error responses (404, 503). Pydantic model validation. |

Tests use a mock `SandboxSession` — no real Kernel Gateway needed. Same pattern as `test_session.py`.

### Integration Tests (on KVM host)

| Test | Validates |
|------|-----------|
| `test_api_hello_world` | `POST /execute` with `print('hello')` → `stdout == "hello\n"` |
| `test_api_session_lifecycle` | Create session → execute twice (state persists) → delete |
| `test_api_session_ttl` | Session auto-destroyed after TTL |
| `test_api_rich_output` | Execute matplotlib plot → response contains `data_b64` PNG |
| `test_api_error_result` | Execute `1/0` → 200 with `success: false` |

### Example Validation

Examples are not auto-tested (they require `ANTHROPIC_API_KEY`), but they are syntactically valid Python verified by import check.

---

## 8. Dependencies

**New in `pyproject.toml`:**
```
fastapi>=0.115
uvicorn>=0.34
```

Added to main dependencies (not dev-only) — the API server is a deployable service.

No other new dependencies. `anthropic` is NOT a project dependency — it's only used in the examples, which users install separately.

---

## 9. Project Structure Changes

```
execution_api/               # NEW — entire package
├── __init__.py
├── server.py                # FastAPI app, session manager, endpoints
├── models.py                # Pydantic request/response models
└── tool_schemas/
    ├── claude.json           # Claude tool_use schema
    └── openai.json           # OpenAI function_calling schema

examples/                    # NEW — example scripts
├── oneshot_example.py       # Single-turn: Claude + SandboxSession
└── conversation_example.py  # Multi-turn: persistent session

tests/
├── test_execution_api.py    # NEW — unit tests for Execution API
└── test_integration.py      # MODIFY — add API integration tests
```
