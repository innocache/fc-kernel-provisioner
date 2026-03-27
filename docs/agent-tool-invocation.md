# Data Analyst Agent — Tool Invocation Design Spec

## 1. Current State

```
Chainlit on_chat_start
  +-- agent.start_session()              <- EAGER: creates session before user speaks
       +-- httpx.AsyncClient(base_url)
       +-- POST /sessions -> session_id
       +-- execute("import numpy, pandas...")  <- warm-up

User message
  +-- agent.chat(text)
       +-- append to messages[]
       +-- LLM call (system_prompt + tools + messages)
       +-- WHILE stop_reason == "tool_use":
            +-- for each tool_call:
            |    +-- execute_python_code -> POST /sessions/{id}/execute
            |    +-- launch_dashboard   -> POST /sessions/{id}/dashboard
            |    +-- download_file      -> execute(base64-encode) -> decode client-side
            +-- append tool_results to messages[]
            +-- compact old tool results (truncate by char count)
            +-- LLM call again

Chainlit on_chat_end
  +-- agent.end_session() -> DELETE /sessions/{id}
```

## 2. Problems

| # | Problem | Impact |
|---|---------|--------|
| P1 | **Session created before user speaks** | Wastes a VM if user never sends a message. VM sits idle for entire TTL. |
| P2 | **Tool defs duplicated in 5 places** | `config.py`, `tool_schemas/claude.json`, `tool_schemas/openai.json`, `examples/*.py`, and `README.md` — already diverged (`framework` field exists only in tool_schemas, README documents `download_file(path=...)` differently). Single change requires 5 edits. |
| P3 | **download_file is not a real API call** | Executes Python inside VM to `base64.b64encode(open(path).read())`, then decodes stdout. Breaks on binary files with newlines, limited to 10MB, no streaming. The Execution API has no `GET /files/{name}` endpoint. |
| P4 | **Session recovery loses all state** | `_execute_with_recovery` creates a fresh session on ConnectError. All variables, imports, uploaded files are gone. Agent tells LLM "variables lost" — LLM has no way to recover. |
| P5 | **No LLM response streaming** | `provider.chat()` waits for complete response. User sees nothing until full LLM response + all tool executions finish. For multi-tool chains this can be 10+ seconds of silence. Deferred to future work (see issue #38). |
| P6 | **Context compaction is blunt** | Truncates old tool results to 200 chars regardless of importance. A critical error message gets same truncation as a verbose DataFrame print. Additionally, `_format_result()` drops `error.traceback` entirely — only `name` and `value` are kept. |
| P7 | **LLM provider stores raw Anthropic objects** | `raw_content` in messages is Anthropic `ContentBlock` objects. Works because OpenAI provider wraps `choice.message` in a list, but breaks if you try to serialize/persist conversations. |
| P8 | **No idempotent tool dispatch** | If agent crashes between executing code and appending result to messages, the tool call replays on restart. For side-effecting code (file writes, data mutations) this is dangerous. Deferred to future work — requires conversation persistence (see issue #37). |
| P9 | **System prompt is static** | Doesn't include uploaded filenames, session state, or available packages. LLM has to guess or ask what files are available. |

## 3. Target Design

### 3.1 Session Lifecycle: Lazy Creation

```
                           +-----------------------------+
                           |     DataAnalystAgent        |
                           |                             |
  on_chat_start ---------> |  provider = create_provider()|
                           |  session_id = None          | <- NO session yet
                           |  _client = None             |
                           +-------------+---------------+
                                         |
  user sends message ---------------------+
  (or uploads file)                      |
                                         v
                           +------------------------------+
                           |  _ensure_session()            |
                           |  if session_id is None:       |
                           |    POST /sessions             |
                           |    execute(warm-up imports)    |
                           +------------------------------+
```

Rules:

- Session is created on first actual need: `chat()` and `upload_file()` both call `_ensure_session()` internally.
- No pending uploads queue. Since Chainlit's `on_message` calls `upload_file()` before `chat()`, the first `upload_file()` call triggers session creation. Subsequent uploads in the same message go to the already-created session.
- Session is not created eagerly on chat start — saves VM resources.

### 3.2 Tool Definitions: Single Source of Truth

```
execution_api/
  tool_schemas/
    tools.py          <- THE canonical definitions (provider-neutral Python dicts)
      TOOLS: list[dict]
    claude.json       <- GENERATED from tools.py
    openai.json       <- GENERATED from tools.py
```

`tools.py` exports provider-neutral tool definitions. Each LLM provider is responsible for converting to its own format (Anthropic passes through as-is; OpenAI wraps in `{"type": "function", "function": {...}}`). The JSON files and README tool examples are generated from the canonical definitions, not hand-maintained.

The agent imports from `execution_api.tool_schemas.tools` directly. No more copy in `config.py`.

Tool inventory:

| Tool | API Endpoint | When LLM uses it |
|------|-------------|-------------------|
| `execute_python_code` | `POST /sessions/{id}/execute` | Any computation, data loading, visualization, file creation |
| `launch_dashboard` | `POST /sessions/{id}/dashboard` | Interactive exploration with widgets (Panel) |
| `download_file` | `GET /sessions/{id}/files/{name}` (NEW) | Deliver a file the sandbox created to the user |

The `download_file` tool schema keeps `path` as its input parameter (e.g., `/data/report.csv`). The agent strips the `/data/` prefix internally when calling the API endpoint. No change to the LLM-facing schema.

### 3.3 Tool Invocation Flow

```
User: "Analyze sales.csv and show me trends"
  |
  +-- files in message? --> upload_file() for each (triggers _ensure_session)
  |
  v
agent.chat(message)
  |
  +-- _ensure_session()           <- lazy create (no-op if already created by upload)
  |    +-- POST /sessions
  |    +-- execute(warm-up)
  |
  +-- _build_system_prompt()      <- DYNAMIC: includes uploaded filenames
  |
  +-- LLM.chat(messages, system, tools)
  |
  +-- WHILE stop_reason == "tool_use":
       |
       +-- for each tool_call:
       |    |
       |    +-- execute_python_code
       |    |    POST /sessions/{id}/execute
       |    |    {code: "..."}
       |    |    <- {success, stdout, stderr, outputs[], error}
       |    |    |
       |    |    +-- yield ToolStart(name, code)
       |    |    +-- yield ToolResult(output, success)
       |    |    +-- yield ImageOutput(data, mime) for each
       |    |
       |    +-- launch_dashboard
       |    |    POST /sessions/{id}/dashboard
       |    |    {code: "..."}
       |    |    <- {url, session_id, app_id}
       |    |    |
       |    |    +-- yield DashboardLink(url, full_url)
       |    |
       |    +-- download_file
       |         GET /sessions/{id}/files/{name}
       |         <- raw bytes
       |         |
       |         +-- yield FileDownload(name, bytes, mime)
       |
       +-- append tool_results to messages[]
       +-- _compact_messages()
       +-- LLM.chat(messages, system, tools)

  Final text response
       +-- yield TextDelta(text)
```

### 3.4 Dynamic System Prompt

Current: static string in `config.py`.
Target: built per-turn from session state.

```python
def _build_system_prompt(self) -> str:
    parts = [BASE_SYSTEM_PROMPT]

    # Inject user-uploaded files so LLM knows what's available
    if self._uploaded_files:
        file_list = "\n".join(f"  - /data/{f}" for f in self._uploaded_files)
        parts.append(f"\nUPLOADED FILES:\n{file_list}")

    # Inject session context (e.g. after recovery)
    if self._session_context:
        parts.append(f"\nSESSION STATE:\n{self._session_context}")

    return "\n".join(parts)
```

`_uploaded_files` tracks only user-uploaded filenames, not files created by sandbox execution. The LLM can run `os.listdir('/data/')` if it needs the full picture.

### 3.5 Session Recovery

Current: create new session, tell LLM "variables lost".
Target: minimal recovery with agent-owned setup replay.

```
Session dies (ConnectError / ReadError / 404)
  |
  +-- Create new session (POST /sessions)
  |
  +-- Re-upload files from _upload_cache
  |    (agent caches filename -> bytes up to RECOVERY_CACHE_MAX, default 20MB)
  |    Files exceeding the cache budget are tracked by name but not cached.
  |
  +-- Re-execute agent-owned warm-up only
  |    (the same "import numpy, pandas; matplotlib.use('Agg')" from _ensure_session)
  |    No heuristic replay of user code. No attempt to detect imports or data loads.
  |
  +-- Inject system note into _session_context:
       "Session was recovered. Files re-uploaded: [list].
        Files too large to cache (user must re-upload): [list].
        All variables and computation state from previous session are lost.
        Reload data from /data/ paths as needed."
```

Recovery is deliberately minimal. The agent does not attempt to replay user code — heuristic detection of imports and data loads from arbitrary code strings is brittle and would produce unreliable results. Instead, the LLM is given full context about what files are available and is expected to re-issue its own data loading commands.

Detection scope: `_execute_with_recovery` currently catches `ConnectError` and `ReadError`. It should also handle HTTP 404 (session expired) from any session-bound endpoint (`execute`, `upload_file`, `launch_dashboard`).

### 3.6 Download Endpoint

Current: `download_file` tool executes Python to base64-read a file through the kernel.

Target: `GET /sessions/{id}/files/{filename}` endpoint on the Execution API.

```
GET /sessions/{id}/files/{filename}
  -> 200 with raw bytes, Content-Disposition: attachment
  -> 404 if file doesn't exist
  -> 413 if file exceeds limit
```

Implementation note: the Execution API can only reach the VM filesystem through `SandboxSession.execute()`. The new endpoint is a server-side proxy that internally uses kernel execution to base64-read the file, same as upload/list/delete. The difference is that the agent gets a clean HTTP interface instead of constructing Python code itself.

Agent implementation:

```python
async def download_file(self, path: str) -> FileDownload:
    if not path.startswith("/data/"):
        raise ValueError(f"Downloads restricted to /data/. Got: {path}")
    filename = path.rsplit("/", 1)[-1]
    client = self._require_client()
    session_id = self._require_session_id()
    resp = await client.get(f"/sessions/{session_id}/files/{filename}")
    if resp.status_code == 404:
        raise FileNotFoundError(f"{path} not found")
    resp.raise_for_status()
    mime = resp.headers.get("content-type", "application/octet-stream")
    return FileDownload(filename=filename, data=resp.content, mime_type=mime)
```

### 3.7 Context Window Management

Current: truncate old `tool_result` blocks to 200 chars.

Target: tiered compaction with awareness of content type, triggered by age (number of tool exchanges).

Prerequisite: `_format_result()` must be updated to include `error.traceback` text. Currently it only keeps `error.name` and `error.value`, which makes error-aware compaction meaningless.

```python
def _compact_messages(self, messages: list[dict]) -> list[dict]:
    """
    Compaction tiers (most recent -> oldest):

    1. Last N tool exchanges: FULL (keep everything including images)
    2. Older exchanges: SUMMARIZED
       - stdout > 500 chars -> first 200 + "..." + last 100
       - stderr -> keep first 200 only
       - images -> replace with "[image: matplotlib plot]"
       - errors -> keep FULL (never truncate errors or tracebacks)
    3. Very old exchanges: COLLAPSED
       - Entire tool_result -> "[executed: <first line of code>] -> success/error"
    """
```

Key rules:

- Errors (including tracebacks) are never truncated. The LLM needs full tracebacks to self-correct.
- Compaction is triggered by the number of tool exchanges, not a strict token budget. A rough char-to-token estimate (1 token ~ 4 chars) is used to warn in logs if the conversation approaches model context limits.
- `RECENT_KEEP_FULL` (currently 10) controls the boundary between tier 1 and tier 2.

### 3.8 Provider Abstraction

Current: `raw_content` stores provider-specific objects (Anthropic ContentBlocks, OpenAI Message).

Target: normalize to serializable dicts using a provider-neutral block schema.

```python
# Provider-neutral assistant content blocks (always dicts)
{"type": "text", "text": "..."}
{"type": "tool_use", "id": "toolu_...", "name": "execute_python_code", "input": {"code": "..."}}
```

```python
@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: str            # "tool_use" | "end"
    raw_content: list[dict]     # Always serializable dicts, never provider objects
```

Each provider is responsible for:
- **Output**: converting its native response objects to the neutral block schema.
- **Input**: converting the neutral blocks back to its native format when building the next request.

Anthropic: `ContentBlock` objects are converted to dicts via `{"type": block.type, "text": block.text}` or `{"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}`.

OpenAI/Ollama: `_convert_messages()` must be updated to consume dict blocks instead of using `hasattr(block, "type")` on objects.

This enables conversation persistence, debugging, and replay.

## 4. Component Boundaries

```
+-----------------------------------------------------+
|  app.py (Chainlit)                                  |
|  - UI rendering, wiring                             |
|  - Creates provider, passes to agent                |
|  - Maps AgentEvents -> Chainlit widgets             |
|  - Does not call LLM APIs or manage tool dispatch   |
+-----------------------------------------------------+
|  agent.py (DataAnalystAgent)                        |
|  - Session lifecycle (lazy create, recovery)        |
|  - Tool dispatch (tool_call -> API call)            |
|  - Context management (compaction, system prompt)   |
|  - Upload cache for recovery                        |
|  - Emits AgentEvent stream                          |
+-----------------------------------------------------+
|  llm_provider.py (LLMProvider protocol)             |
|  - LLM API abstraction                              |
|  - Normalizes responses to neutral block schema     |
|  - Converts neutral tool defs to provider format    |
+-----------------------------------------------------+
|  config.py                                          |
|  - Environment variables, limits                    |
|  - BASE system prompt (static part only)            |
|  - NO tool definitions (moved to tool_schemas/)     |
+-----------------------------------------------------+
|  execution_api/tool_schemas/tools.py                |
|  - SINGLE SOURCE for provider-neutral tool defs     |
|  - JSON files generated for external consumers      |
+-----------------------------------------------------+
```

## 5. Change Summary

| Change | Files | Effort |
|--------|-------|--------|
| Lazy session creation | `agent.py` | Small — move `start_session` into `_ensure_session`, called from `chat()` and `upload_file()` |
| Single tool source of truth | new `tool_schemas/tools.py`, update `config.py`, `llm_provider.py`, regenerate JSONs, update README and examples | Small |
| File download endpoint | `server.py`, `agent.py` | Medium — new GET endpoint (server-side proxy via kernel execution), update agent `download_file()` |
| Dynamic system prompt | `agent.py`, `config.py` | Small |
| Session recovery | `agent.py` | Small — upload cache, agent-owned warm-up replay, system note injection |
| Smarter compaction | `agent.py` | Small — prerequisite: add traceback to `_format_result()` |
| Normalize raw_content | `llm_provider.py` | Medium — define neutral block schema, update Anthropic + OpenAI + Ollama serializers |

## 6. Out of Scope

- Multi-agent orchestration (multiple agents sharing sessions)
- Conversation persistence (saving/loading conversations across restarts)
- Idempotent tool dispatch — requires conversation persistence (tracked: issue #37)
- LLM response streaming — cross-provider complexity, Ollama support unreliable (tracked: issue #38)
- Authentication (API keys, user auth)
- Rate limiting on tool calls
- Custom package installation in sandbox
