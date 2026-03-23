# Sandbox Client — Design Specification

> **Date**: 2026-03-22
> **Status**: Approved
> **Approach**: Thin async wrapper over Kernel Gateway REST + WebSocket

**Note**: This spec supersedes the sandbox client references in the parent design spec (`2026-03-21-fc-kernel-provisioner-design.md` sections 7 and 9). The class is named `SandboxSession` (not `SandboxClient`), the main module is `session.py` (not `client.py`), and the `_capture_rich_output()` method is replaced by the `OutputParser` + `ArtifactStore` integration described here.

---

## 1. Overview

A Python client library that lets a chatbot backend execute Python code inside Firecracker microVM sandboxes and get structured results back (stdout, stderr, errors, images, HTML). Built as a thin wrapper over the Kernel Gateway's existing REST and WebSocket API.

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Consumer | Single chatbot backend | Simplifies API surface — no multi-tenant concerns |
| Connection target | Kernel Gateway (HTTP + WebSocket) | Already deployed, handles kernel lifecycle and ZMQ proxying |
| Output delivery | Inline data by default, optional ArtifactStore for URLs | Zero infrastructure for basic use; URLs when file serving is available |
| Execution model | Batch (wait for completion) | Chatbot sends response after execution finishes; streaming adds complexity without benefit |
| Lifecycle | Context manager + explicit create/destroy | Context manager for one-shot use, explicit for long-lived conversations |
| Timeouts | Configurable per-call, 30s default | Prevents runaway code from blocking the chatbot |
| Orchestration | None — not in scope | LLM tool dispatch lives in the chatbot backend, not the sandbox client |
| New dependencies | None | Uses aiohttp (already in project) |

---

## 2. API Surface

### Core Classes

```python
# Context manager usage
async with SandboxSession("http://localhost:8888") as session:
    result = await session.execute("print('hello')")
    # result.stdout == "hello\n"
    # result.success == True

# Rich output
result = await session.execute("""
import matplotlib.pyplot as plt
plt.plot([1, 2, 3])
plt.show()
""")
# result.outputs[0].mime_type == "image/png"
# result.outputs[0].data == b"<png bytes>"
# result.outputs[0].url == "http://..." (if ArtifactStore configured)

# Explicit lifecycle for long-lived conversations
session = SandboxSession("http://localhost:8888")
await session.start()
await session.execute("x = 42")
await session.execute("print(x)")  # stdout: "42\n"
await session.stop()

# Per-call timeout (default 30s)
result = await session.execute("import time; time.sleep(100)", timeout=5)
# result.success == False
# result.error.name == "TimeoutError"

# Optional ArtifactStore for URL-based output
store = LocalArtifactStore(base_dir="/var/lib/fc-artifacts", url_prefix="http://localhost:8080/artifacts")
async with SandboxSession("http://localhost:8888", artifact_store=store) as session:
    result = await session.execute("...")
    # result.outputs[0].url == "http://localhost:8080/artifacts/{session_id}/output_0.png"
```

### Data Classes

```python
@dataclass
class ExecutionResult:
    success: bool
    stdout: str
    stderr: str
    error: ExecutionError | None
    outputs: list[DisplayOutput]
    execution_count: int

@dataclass
class DisplayOutput:
    mime_type: str          # "image/png", "text/html", etc.
    data: bytes | str       # raw content (bytes for binary, str for text)
    url: str | None         # populated when ArtifactStore is configured

@dataclass
class ExecutionError:
    name: str               # e.g., "ZeroDivisionError"
    value: str              # e.g., "division by zero"
    traceback: list[str]    # formatted traceback lines
```

### Constructor

```python
class SandboxSession:
    def __init__(
        self,
        gateway_url: str = "http://localhost:8888",
        kernel_name: str = "python3-firecracker",
        default_timeout: float = 30.0,
        artifact_store: ArtifactStore | None = None,
    ): ...
```

---

## 3. Architecture & Components

### File Structure

```
sandbox_client/
├── __init__.py          # re-exports: SandboxSession, ExecutionResult, DisplayOutput,
│                        #   ExecutionError, ArtifactStore, LocalArtifactStore
├── session.py           # SandboxSession — kernel lifecycle + execute()
├── output.py            # OutputParser, ExecutionResult, DisplayOutput, ExecutionError
└── artifact_store.py    # ArtifactStore protocol, LocalArtifactStore
```

Four files, each under ~150 lines. No circular dependencies — `session.py` imports from `output.py` and `artifact_store.py`. Nothing imports from `session.py`.

The `sandbox_client` package lives in the same repo but imports nothing from `fc_provisioner` or `fc_pool_manager`. It is a pure client library that talks HTTP/WebSocket to the Kernel Gateway.

### Data Flow

For `session.execute("print('hello')")`:

1. `SandboxSession.execute()` sends an `execute_request` message over the WebSocket
2. Collects all response messages until `status: idle` (or timeout)
3. Passes collected messages to `OutputParser.parse()`
4. `OutputParser` extracts stdout, stderr, errors, and display_data into an `ExecutionResult`
5. If an `ArtifactStore` is configured, display outputs are saved and URLs attached
6. Returns the `ExecutionResult`

---

## 4. Component Details

### SandboxSession (`session.py`)

Holds an `aiohttp.ClientSession`, a kernel ID, and a WebSocket connection.

**`start()`**:
1. Create `aiohttp.ClientSession`
2. POST `/api/kernels` with `{"name": kernel_name}` → get `kernel_id`
3. Open WebSocket to `/api/kernels/{kernel_id}/channels`

**`execute(code, timeout=None)`**:
1. Raise `RuntimeError("Session not started")` if called before `start()` or after `stop()`
2. Build Jupyter `execute_request` message with a unique `msg_id` (UUID4). The message must include the `channel: "shell"` field required by the Kernel Gateway's WebSocket API:
   ```python
   {
       "header": {"msg_id": "<uuid>", "msg_type": "execute_request", ...},
       "parent_header": {},
       "metadata": {},
       "content": {"code": code, "silent": False, ...},
       "buffers": [],
       "channel": "shell",
   }
   ```
3. Send over WebSocket as JSON
4. Collect response messages in a loop, filtering by `parent_header.msg_id` to match our request
5. Stop when `status` message with `execution_state: "idle"` is received
6. Wrap collection loop in `asyncio.wait_for(timeout)` — on timeout, interrupt the kernel via `POST /api/kernels/{kernel_id}/interrupt` (the REST interrupt endpoint), then return result with `TimeoutError`
7. Pass collected messages to `OutputParser.parse()`
8. If `ArtifactStore` configured, encode text `DisplayOutput.data` to bytes (UTF-8) before calling `save()`, then attach returned URLs
9. Return `ExecutionResult`

**`stop()`**:
1. Close WebSocket
2. DELETE `/api/kernels/{kernel_id}`
3. Close `aiohttp.ClientSession`
4. Clear internal state

**Context manager**: `__aenter__` calls `start()`. `__aexit__` calls `stop()` — errors from `stop()` are suppressed (logged but not raised), so the original exception from the `async with` body propagates.

**Session ID**: The `kernel_id` obtained from `POST /api/kernels` is used as the `session_id` for artifact storage. No separate ID is generated.

### OutputParser (`output.py`)

Stateless parser. A single function: `parse(messages: list[dict]) → ExecutionResult`.

**Message type handling**:

| Message type | Action |
|---|---|
| `stream` | Append `content.text` to `stdout` or `stderr` based on `content.name` |
| `error` | Create `ExecutionError(name, value, traceback)`, set `success = False` |
| `execute_result` | Extract mime bundle → `DisplayOutput` entries |
| `display_data` | Extract mime bundle → `DisplayOutput` entries |
| `status` | Ignored (used by session for flow control, not passed to parser) |
| `execute_input` | Ignored |
| `execute_reply` | Extract `execution_count` from `content.execution_count`. Fallback error detection if `content.status == "error"` and no `error` message was received |

**Mime bundle handling**: `display_data` and `execute_result` messages contain a `data` dict with multiple mime types (e.g., `{"image/png": "<base64>", "text/plain": "<fallback>"}`). The parser creates one `DisplayOutput` per non-text mime type. Priority order: `image/png` > `image/svg+xml` > `text/html` > `application/json` > `text/plain`. Text-plain is only kept if it is the sole representation.

Binary mime types (`image/png`) are base64-decoded from the Jupyter message into `bytes` in `DisplayOutput.data`. Text mime types (`text/html`, `text/plain`, `application/json`, `image/svg+xml`) are kept as `str`. Note: `image/svg+xml` is XML text — ipykernel sends it as a plain string, not base64-encoded.

### ArtifactStore (`artifact_store.py`)

**Interface** (Python Protocol):

```python
class ArtifactStore(Protocol):
    async def save(self, session_id: str, filename: str, data: bytes, content_type: str) -> str:
        """Save artifact data and return its URL.

        The caller (SandboxSession) is responsible for encoding str data to
        bytes (UTF-8) before calling save().  Data is always bytes here.
        """
        ...
```

**`LocalArtifactStore`**:
- Constructor: `LocalArtifactStore(base_dir: str, url_prefix: str)`
- `save()`: writes to `{base_dir}/{session_id}/{filename}`, creates directories as needed, returns `{url_prefix}/{session_id}/{filename}`
- Filenames are auto-generated by the session: `output_0.png`, `output_1.html`, etc., based on mime type

**Deferred**: TTL cleanup, `get()`, `delete_session()`, S3 implementation. Only `save()` is built now.

---

## 5. Error Handling

**Connection errors** (raised as exceptions):

| Scenario | Exception |
|---|---|
| Kernel Gateway unreachable | `ConnectionError` from `start()` |
| Pool exhausted (KG returns 503) | `RuntimeError("No VMs available")` from `start()` |
| WebSocket drops mid-execution | `ConnectionError` from `execute()` |

**No retry logic in the sandbox client.** The chatbot backend decides whether and how to retry. The sandbox client reports errors faithfully.

**Execution errors** (returned as results, not exceptions):

| Scenario | Result |
|---|---|
| Syntax error, runtime exception | `success=False`, `error` populated with name/value/traceback |
| Code produces output then crashes | `stdout`/`stderr` captured up to failure point |
| Timeout | `success=False`, `error.name="TimeoutError"`, kernel interrupted |

**Kernel death**: If the kernel crashes mid-execution (segfault, OOM), the WebSocket will close or stop sending messages. The timeout mechanism catches this — after timeout expires, the result is returned with whatever was collected plus a `TimeoutError`.

**Session reuse after error**: The session stays usable after an execution error (just like a Jupyter notebook). The session is only unusable if the kernel dies, in which case `stop()` cleans up and the caller creates a new session.

---

## 6. Testing

### Unit Tests (mocked, no KVM needed)

| Test file | Validates |
|---|---|
| `test_output_parser.py` | Parsing all Jupyter message types → `ExecutionResult`. Mime bundle priority. Stdout/stderr accumulation. Error extraction. Edge cases (empty output, multiple display outputs). |
| `test_session.py` | `start()`/`stop()` call correct REST endpoints. `execute()` sends correct WebSocket message format. Timeout triggers interrupt. Context manager cleanup. |
| `test_artifact_store.py` | `LocalArtifactStore.save()` writes files to correct paths. Returns correct URLs. Creates directories as needed. |

### Integration Tests (on KVM host)

| Test | Validates |
|---|---|
| `test_sandbox_hello_world` | `execute("print('hello')")` → `stdout == "hello\n"` |
| `test_sandbox_state_persists` | Two executes share state: `x = 42` then `print(x)` |
| `test_sandbox_error_handling` | `execute("1/0")` → `success=False`, error populated |
| `test_sandbox_rich_output` | matplotlib plot → `outputs[0].mime_type == "image/png"` |
| `test_sandbox_html_output` | pandas DataFrame display → `outputs` contains `text/html` |
| `test_sandbox_timeout` | `execute("...", timeout=3)` with long-running code → timeout error |
| `test_sandbox_artifact_store` | With `LocalArtifactStore`, outputs get URLs and files written |

---

## 7. Project Structure Changes

```
sandbox_client/               # NEW — entire package
├── __init__.py
├── session.py
├── output.py
└── artifact_store.py

tests/
├── test_output_parser.py     # NEW — unit tests for OutputParser
├── test_session.py           # NEW — unit tests for SandboxSession
├── test_artifact_store.py    # NEW — unit tests for LocalArtifactStore
└── test_integration.py       # MODIFY — add sandbox client integration tests
```

No changes to `pyproject.toml` dependencies (uses existing `aiohttp`). The `sandbox_client` package is included in the project's packages list.
