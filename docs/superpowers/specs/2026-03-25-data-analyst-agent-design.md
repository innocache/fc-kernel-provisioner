# Data Analytics Agent — Design Spec

## 1. Summary

A sample data analytics agent that combines Claude's reasoning with sandboxed Python execution. Users upload data files, ask analytical questions in natural language, and get results as inline charts, tables, and interactive Panel dashboards — all through a Chainlit chat interface.

## 2. Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Frontend | Chainlit | Purpose-built for LLM chat+tools: step visualization, file upload, image display, streaming |
| API layer | Execution API REST | Agent is just an HTTP client. Decoupled from infrastructure internals. |
| LLM | **Any LLM with tool/function calling** | Pluggable provider: Anthropic, OpenAI, Gemini, local (Ollama). Default: Claude. |
| Agent pattern | ReAct loop | LLM sees execution output, decides next step. Handles multi-step analysis naturally. |
| Session lifecycle | One sandbox session per chat | Kernel state persists across messages. Session destroyed on chat end. |
| Location | `apps/data_analyst/` in this repo | Sample app alongside infrastructure |

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Browser (:8501)                                                  │
│  Chainlit Chat UI                                                 │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  Chat messages + inline matplotlib images                    │ │
│  │  File upload dropzone (CSV, Excel, Parquet, JSON)            │ │
│  │  Tool step visualization (shows code being executed)         │ │
│  │  Dashboard links (opens Panel app in new tab)                │ │
│  └─────────────────────────────────────────────────────────────┘ │
└──────────────────────┬───────────────────────────────────────────┘
                       │ WebSocket (Chainlit protocol)
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  Chainlit Backend (Python)                                        │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  DataAnalystAgent                                         │    │
│  │                                                           │    │
│  │  @cl.on_chat_start:                                       │    │
│  │    → POST /sessions → get session_id                      │    │
│  │    → Pre-warm imports (pandas, numpy, matplotlib)         │    │
│  │                                                           │    │
│  │  @cl.on_message:                                          │    │
│  │    → Handle file uploads (save to sandbox via /execute)   │    │
│  │    → Claude tool_use loop:                                │    │
│  │      1. Send user message + history to Claude             │    │
│  │      2. Claude returns tool_use blocks                    │    │
│  │      3. Execute each tool:                                │    │
│  │         execute_python_code → POST /sessions/{id}/execute │    │
│  │         launch_dashboard → POST /sessions/{id}/dashboard  │    │
│  │      4. Feed results back to Claude                       │    │
│  │      5. Repeat until Claude returns text (stop_reason=end)│    │
│  │    → Display text + images + dashboard links              │    │
│  │                                                           │    │
│  │  @cl.on_chat_end:                                         │    │
│  │    → DELETE /sessions/{id}                                │    │
│  └──────────────────────────────────────────────────────────┘    │
│                       │ HTTP                                      │
└───────────────────────┼──────────────────────────────────────────┘
                        ▼
              Execution API (:8000)
              (existing infrastructure)
```

## 4. User Experience Flow

### 4.1 Basic Analysis

```
User: [uploads sales.csv] "What are the top revenue drivers?"

Agent (thinking step visible): 
  → execute_python_code: load CSV, df.head(), df.describe()

Agent: "The dataset has 10,000 rows with columns: date, product, region, revenue, units.
        Let me analyze revenue distribution."

Agent (thinking step visible):
  → execute_python_code: groupby product, plot top 10

Agent: "Here are the top 10 products by revenue:
        [matplotlib chart displayed inline]
        Product A leads with $2.3M, followed by..."

User: "Can I explore this interactively?"

Agent (thinking step visible):
  → launch_dashboard: Panel app with filters

Agent: "📊 Dashboard ready: http://localhost:8080/dash/abc123/dash_xyz
        You can filter by region and date range."
```

### 4.2 Multi-Step Analysis

```
User: "Run a cohort analysis on customer retention"

Agent:
  → execute: load data, identify cohorts by signup month
  → execute: build retention matrix
  → execute: plot heatmap
  → text: "Here's the retention analysis. Month 1 retention is 45%..."

User: "What about by acquisition channel?"

Agent:
  → execute: group by channel, rebuild retention matrix
  → execute: plot comparison
  → text: "Organic has 52% M1 retention vs 31% for paid..."
```

## 5. File Structure

```
apps/data_analyst/
├── agent.py              # DataAnalystAgent class — session + tool loop (LLM-agnostic)
├── llm_provider.py       # LLMProvider protocol + Anthropic/OpenAI/Ollama implementations
├── app.py                # Chainlit handlers — on_chat_start, on_message, on_chat_end
├── config.py             # Configuration (API URL, LLM provider, system prompt, tools)
├── .chainlit/
│   └── config.toml       # Chainlit settings (file upload, theme)
└── chainlit.md           # Welcome message displayed on chat start
```

## 6. Component Design

### 6.1 config.py

```python
import os

EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")
CADDY_BASE_URL = os.environ.get("CADDY_BASE_URL", "http://localhost:8080")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

ACCEPTED_FILE_TYPES = [
    "text/csv",
    "application/json",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.apache.parquet",
    "text/plain",
]

MAX_FILE_SIZE_MB = 50

SYSTEM_PROMPT = """You are a data analytics assistant with access to a sandboxed Python environment.

CAPABILITIES:
- execute_python_code: Run Python in an isolated Firecracker microVM
  - Pre-installed: numpy, pandas, matplotlib, scipy, plotly, seaborn, scikit-learn
  - State persists across calls (variables, imports, files)
  - Uploaded files are at /data/<filename>
- launch_dashboard: Create an interactive Panel dashboard (embedded in chat)
- download_file: Read a file from the sandbox and send it to the user for download

WORKFLOW:
1. When data is uploaded, immediately load it and show df.head(), df.shape, df.dtypes
2. For analysis questions, write and execute Python code step by step
3. Show intermediate results — print DataFrames, statistics, value counts
4. For visualizations, use matplotlib (plots appear inline in chat)
5. For interactive exploration, use launch_dashboard with Panel + hvPlot
6. When the user asks to export/download, save the file in the sandbox then use download_file

RULES:
- Always use matplotlib.use('Agg') before importing pyplot
- Print results explicitly — the chat only sees stdout and images
- For large DataFrames, show .head() or .describe(), not the full frame
- Handle errors gracefully — if code fails, explain and retry
- Be concise in explanations, let the data speak
- When saving files for download, use /data/ as the output directory"""

TOOLS = [
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
            "The dashboard can access the same data and variables as execute_python_code. "
            "Use for interactive exploration with widgets, filters, and live charts."
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
            "First use execute_python_code to create the file (e.g., df.to_csv, plt.savefig, "
            "df.to_excel), then call this tool with the file path to deliver it to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file in the sandbox (e.g., /data/report.csv)",
                },
            },
            "required": ["path"],
        },
    },
]
```

### 6.2 llm_provider.py — Pluggable LLM Interface

The agent is LLM-agnostic. All LLM interaction goes through a provider interface:

```python
@dataclass
class ToolCall:
    id: str
    name: str
    input: dict

@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: str  # "end" | "tool_use"

class LLMProvider(Protocol):
    async def chat(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
    ) -> LLMResponse: ...
```

**Built-in providers:**

```python
class AnthropicProvider(LLMProvider):
    """Claude via Anthropic API. Uses tool_use format natively."""
    def __init__(self, model="claude-sonnet-4-20250514"):
        self.client = anthropic.AsyncAnthropic()
        self.model = model

    async def chat(self, messages, system, tools) -> LLMResponse:
        response = await self.client.messages.create(
            model=self.model, max_tokens=4096,
            system=system, tools=tools, messages=messages,
        )
        # Convert Anthropic content blocks → ToolCall / text
        ...

class OpenAIProvider(LLMProvider):
    """GPT-4o, o1, etc. via OpenAI API. Converts tools to function_calling format."""
    def __init__(self, model="gpt-4o"):
        self.client = openai.AsyncOpenAI()
        self.model = model

    async def chat(self, messages, system, tools) -> LLMResponse:
        # Convert tool definitions: input_schema → parameters
        oai_tools = [{"type": "function", "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }} for t in tools]
        # Convert message format: Anthropic → OpenAI
        oai_messages = [{"role": "system", "content": system}]
        for m in messages:
            oai_messages.append(_convert_message(m))
        response = await self.client.chat.completions.create(
            model=self.model, tools=oai_tools, messages=oai_messages,
        )
        # Convert function_call blocks → ToolCall
        ...

class OllamaProvider(LLMProvider):
    """Local models via Ollama. Uses OpenAI-compatible API."""
    def __init__(self, model="llama3.1", base_url="http://localhost:11434/v1"):
        self.client = openai.AsyncOpenAI(base_url=base_url, api_key="ollama")
        self.model = model
    # Same as OpenAIProvider — Ollama uses OpenAI function_calling format
```

**Provider selection via environment variable:**

```python
# config.py
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")  # anthropic | openai | ollama
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514")

def create_provider() -> LLMProvider:
    if LLM_PROVIDER == "anthropic":
        return AnthropicProvider(model=LLM_MODEL)
    elif LLM_PROVIDER == "openai":
        return OpenAIProvider(model=LLM_MODEL)
    elif LLM_PROVIDER == "ollama":
        return OllamaProvider(model=LLM_MODEL)
    else:
        raise ValueError(f"Unknown LLM provider: {LLM_PROVIDER}")
```

**Usage examples:**

```bash
# Claude (default)
ANTHROPIC_API_KEY=sk-ant-... chainlit run app.py

# GPT-4o
LLM_PROVIDER=openai LLM_MODEL=gpt-4o OPENAI_API_KEY=sk-... chainlit run app.py

# Local Llama via Ollama
LLM_PROVIDER=ollama LLM_MODEL=llama3.1 chainlit run app.py
```

**Tool format translation:**

The tools are defined once in Anthropic's `input_schema` format (our canonical format — matches the existing `tool_schemas/claude.json`). Each provider converts to its native format:

| Provider | Tool Format | Message Format |
|----------|------------|----------------|
| Anthropic | `input_schema` (native) | `tool_use` / `tool_result` content blocks |
| OpenAI | `parameters` (renamed) | `function_call` / `tool_calls` in choices |
| Ollama | `parameters` (OpenAI-compatible) | Same as OpenAI |

### 6.3 agent.py

Core responsibilities:
- Manage sandbox session lifecycle (create on chat start, destroy on end)
- Upload files to sandbox via code execution
- Download files from sandbox for user export
- Run the LLM tool_use loop (via LLMProvider)
- Extract images from execution results
- Track dashboard URLs

```python
class DataAnalystAgent:
    def __init__(self, api_url, provider: LLMProvider):
        self.api_url = api_url
        self.provider = provider
        self.session_id = None
        self.messages = []
        self._client = httpx.AsyncClient(...)

    async def start_session(self) -> None:
        # POST /sessions → store session_id
        # Pre-warm: execute "import numpy, pandas, matplotlib; matplotlib.use('Agg')"

    async def upload_file(self, filename: str, content: bytes) -> str:
        # Base64-encode content, execute code to write to /data/{filename}
        # Return confirmation message

    async def chat(self, user_message: str) -> AsyncGenerator[AgentEvent, None]:
        # Yields events: TextDelta, ToolStart, ToolResult, ImageOutput, DashboardLink
        # Runs the provider.chat() loop
        # Each tool call is yielded as ToolStart/ToolResult for step visualization

    async def end_session(self) -> None:
        # DELETE /sessions/{session_id}
```

Event types for streaming to Chainlit:
```python
@dataclass
class TextDelta:
    text: str

@dataclass  
class ToolStart:
    tool_name: str
    code: str

@dataclass
class ToolResult:
    tool_name: str
    output: str
    success: bool

@dataclass
class ImageOutput:
    data: bytes
    mime_type: str

@dataclass
class DashboardLink:
    url: str       # relative path: /dash/{sid}/dash_{app_id}
    full_url: str  # absolute: http://localhost:8080/dash/...

@dataclass
class FileDownload:
    filename: str
    data: bytes
    mime_type: str
```

### 6.3 app.py (Chainlit handlers)

```python
import chainlit as cl

@cl.on_chat_start
async def on_start():
    agent = DataAnalystAgent(api_url=..., model=...)
    await agent.start_session()
    cl.user_session.set("agent", agent)
    await cl.Message(content="Ready! Upload a data file or ask a question.").send()

@cl.on_message
async def on_message(message: cl.Message):
    agent = cl.user_session.get("agent")

    # Handle file uploads
    for file in message.elements or []:
        content = file.content if hasattr(file, 'content') else open(file.path, 'rb').read()
        result = await agent.upload_file(file.name, content)
        await cl.Message(content=f"📁 Uploaded `{file.name}` — {result}").send()

    # Run agent
    response_msg = cl.Message(content="")
    await response_msg.send()

    async for event in agent.chat(message.content):
        if isinstance(event, TextDelta):
            await response_msg.stream_token(event.text)
        elif isinstance(event, ToolStart):
            async with cl.Step(name=event.tool_name, type="tool") as step:
                step.input = event.code
        elif isinstance(event, ToolResult):
            step.output = event.output
        elif isinstance(event, ImageOutput):
            image = cl.Image(content=event.data, name="plot.png", display="inline")
            response_msg.elements.append(image)
        elif isinstance(event, DashboardLink):
            iframe = (
                f'<iframe src="{event.full_url}" '
                f'width="100%" height="600" frameborder="0" '
                f'style="border-radius: 8px; border: 1px solid #e0e0e0;"></iframe>'
            )
            elements = [cl.Text(name="dashboard", content=iframe, display="inline")]
            await cl.Message(
                content=f"📊 Interactive dashboard ([open full screen]({event.full_url}))",
                elements=elements,
            ).send()
        elif isinstance(event, FileDownload):
            file_el = cl.File(name=event.filename, content=event.data, display="inline")
            await cl.Message(
                content=f"📎 **{event.filename}** ready for download",
                elements=[file_el],
            ).send()

    await response_msg.update()

@cl.on_chat_end
async def on_end():
    agent = cl.user_session.get("agent")
    if agent:
        await agent.end_session()
```

### 6.4 Chainlit config

`.chainlit/config.toml`:
```toml
[project]
name = "Data Analyst"

[features.spontaneous_file_upload]
enabled = true
accept = ["text/csv", "application/json", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/plain"]
max_files = 5
max_size_mb = 50

[UI]
name = "Data Analyst"
description = "Upload data, ask questions, get analysis + interactive dashboards"
```

`chainlit.md` (welcome screen):
```markdown
# 📊 Data Analyst

Upload a CSV, Excel, or JSON file and ask questions about your data.

**Examples:**
- "Summarize this dataset"
- "What are the top 10 products by revenue?"
- "Show me the correlation matrix"
- "Create an interactive dashboard for exploration"
- "Export the cleaned data as CSV"
- "Save the charts as a PDF report"

**Capabilities:**
- Python data analysis (pandas, numpy, scipy)
- Static visualizations (matplotlib, seaborn, plotly)
- Interactive dashboards (Panel + hvPlot)
- File download (CSV, Excel, PDF, images)
```

## 7. Data Upload Flow

```
User drops sales.csv in chat
  ↓
Chainlit: reads file content as bytes
  ↓
agent.upload_file("sales.csv", bytes):
  1. base64-encode the content
  2. POST /sessions/{id}/execute with code:
     import base64, os
     os.makedirs("/data", exist_ok=True)
     data = base64.b64decode("...")
     with open("/data/sales.csv", "wb") as f:
         f.write(data)
     print(f"Saved /data/sales.csv ({len(data)} bytes)")
  3. Return "Saved /data/sales.csv (1.2 MB)"
  ↓
Agent automatically runs:
  import pandas as pd
  df = pd.read_csv("/data/sales.csv")
  print(f"Shape: {df.shape}")
  print(df.head())
  print(df.dtypes)
```

For large files (>5MB), base64 encoding doubles the payload. Alternative for future optimization: upload via artifact store and mount in the VM. For v1, base64 is simple and works.

## 8. File Download Flow

```
User: "Export the cleaned data as CSV"

Agent (LLM):
  → execute_python_code:
      df_cleaned.to_csv("/data/cleaned_data.csv", index=False)
      print("Saved /data/cleaned_data.csv")
  → download_file: {"path": "/data/cleaned_data.csv"}

agent._download_file("/data/cleaned_data.csv"):
  1. POST /sessions/{id}/execute with code:
       import base64
       with open("/data/cleaned_data.csv", "rb") as f:
           data = f.read()
       print(base64.b64encode(data).decode())
  2. Decode base64 from stdout
  3. Infer MIME type from extension
  4. Yield FileDownload(filename="cleaned_data.csv", data=bytes, mime_type="text/csv")

Chainlit:
  → cl.File(name="cleaned_data.csv", content=bytes, display="inline")
  → User sees download button in chat
```

### Download method on agent:

```python
async def _download_file(self, path: str) -> tuple[str, bytes]:
    filename = path.rsplit("/", 1)[-1]
    code = (
        "import base64\n"
        f"with open('{path}', 'rb') as f:\n"
        "    data = f.read()\n"
        "print(base64.b64encode(data).decode())"
    )
    resp = await self._client.post(
        f"/sessions/{self.session_id}/execute",
        json={"code": code},
    )
    result = resp.json()
    if not result.get("success"):
        raise RuntimeError(f"Failed to read {path}: {result.get('error', {}).get('value', 'unknown')}")
    b64_data = result["stdout"].strip()
    return filename, base64.b64decode(b64_data)
```

### Supported download types:

| Extension | MIME Type | Typical Use |
|-----------|----------|-------------|
| `.csv` | `text/csv` | Cleaned data, aggregations |
| `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | Formatted reports |
| `.json` | `application/json` | API-ready data |
| `.parquet` | `application/vnd.apache.parquet` | Efficient data exchange |
| `.png` | `image/png` | Charts, plots |
| `.pdf` | `application/pdf` | Multi-page reports |
| `.html` | `text/html` | Styled tables, plotly charts |

### Size limits:

Base64 encoding via stdout has a practical limit of ~10MB (stdout buffer). For larger exports, the artifact store provides a URL-based download path (future enhancement).

## 9. Image Display Flow

```
Claude generates: plt.savefig("/tmp/plot.png"); plt.show()
  ↓
Execution API returns:
  {"outputs": [{"mime_type": "image/png", "data_b64": "iVBOR..."}]}
  ↓
agent._execute() decodes base64 → ImageOutput(data=bytes)
  ↓
app.py creates cl.Image(content=bytes, display="inline")
  ↓
Chart appears inline in the chat message
```

## 9b. Dashboard Flow (Embedded)

```
Claude generates Panel code
  ↓
agent._launch_dashboard(code)
  → POST /sessions/{id}/dashboard {"code": "..."}
  → Returns {"url": "/dash/{sid}/dash_abc"}
  ↓
DashboardLink(url="/dash/...", full_url="http://localhost:8080/dash/...")
  ↓
Chainlit embeds as <iframe> inline in chat message
  + "open full screen" link for pop-out
  ↓
Browser loads iframe → Caddy → Panel in VM
  ↓
WebSocket for live updates flows through iframe
```

### Embedding requirements

1. **Caddy must allow framing** — add to Caddyfile:
   ```
   header X-Frame-Options "ALLOWALL"
   header Content-Security-Policy "frame-ancestors *"
   ```

2. **Panel WebSocket origin** — the `allowed_origins` passed to the guest agent must include the Chainlit host:
   ```
   DASHBOARD_ALLOWED_ORIGINS=localhost:8080,localhost:8501,127.0.0.1:8501
   ```

3. **User can pop out** — the "open full screen" link opens the same URL in a new tab for undocked exploration.

## 10. Context Window Management

Long analysis sessions generate many tool_use + tool_result pairs. Each pair adds ~2-5KB. After 50 exchanges, the context window fills and LLM quality degrades.

**Strategy: truncate old tool results, keep recent ones in full.**

```python
MAX_HISTORY_TOKENS = 80_000  # leave headroom for system prompt + response
RECENT_KEEP_FULL = 10        # last N tool interactions kept verbatim
TRUNCATED_OUTPUT_CHARS = 200  # older tool results truncated to this length

def compact_messages(messages: list[dict]) -> list[dict]:
    tool_indices = [i for i, m in enumerate(messages)
                    if m["role"] == "user" and isinstance(m.get("content"), list)
                    and any(c.get("type") == "tool_result" for c in m["content"])]
    
    if len(tool_indices) <= RECENT_KEEP_FULL:
        return messages
    
    old_indices = set(tool_indices[:-RECENT_KEEP_FULL])
    compacted = []
    for i, m in enumerate(messages):
        if i in old_indices:
            compacted.append(_truncate_tool_results(m))
        else:
            compacted.append(m)
    return compacted

def _truncate_tool_results(message: dict) -> dict:
    content = []
    for block in message["content"]:
        if block.get("type") == "tool_result":
            text = block.get("content", "")
            if len(text) > TRUNCATED_OUTPUT_CHARS:
                text = text[:TRUNCATED_OUTPUT_CHARS] + "... [truncated]"
            content.append({**block, "content": text})
        else:
            content.append(block)
    return {**message, "content": content}
```

The agent calls `compact_messages()` before each LLM request. Recent tool results are preserved verbatim (the LLM needs them for reasoning). Older results are truncated to 200 chars (enough to know what happened, not enough to fill the context).

## 11. File Size Limits

### Upload limits

| Size | Method | Latency |
|------|--------|---------|
| < 5MB | Base64 in execute (default) | ~500ms |
| 5-50MB | Chunked upload (4MB chunks via multiple execute calls) | ~5s |
| > 50MB | Not supported in v1 | — |

```python
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB per chunk

async def upload_file(self, filename: str, content: bytes) -> str:
    if len(content) > 50 * 1024 * 1024:
        raise ValueError(f"File too large ({len(content)} bytes). Max 50MB.")
    
    if len(content) <= 5 * 1024 * 1024:
        return await self._upload_single(filename, content)
    return await self._upload_chunked(filename, content)

async def _upload_chunked(self, filename: str, content: bytes) -> str:
    total = len(content)
    path = f"/data/{filename}"
    for offset in range(0, total, UPLOAD_CHUNK_SIZE):
        chunk = content[offset:offset + UPLOAD_CHUNK_SIZE]
        b64 = base64.b64encode(chunk).decode()
        mode = "wb" if offset == 0 else "ab"
        code = (
            f"import base64\n"
            f"with open('{path}', '{mode}') as f:\n"
            f"    f.write(base64.b64decode('{b64}'))\n"
            f"print(f'Chunk {offset // (4*1024*1024) + 1}: {{offset + len(chunk)}}/{total} bytes')"
        )
        await self._client.post(
            f"/sessions/{self.session_id}/execute",
            json={"code": code},
        )
    return f"Saved {path} ({total} bytes, chunked)"
```

### Download limits

| Size | Method | Notes |
|------|--------|-------|
| < 10MB | Base64 via stdout (default) | Returned as Chainlit File element |
| > 10MB | Not supported in v1 | Future: artifact store URL |

The agent checks file size before downloading:
```python
async def _download_file(self, path: str) -> tuple[str, bytes]:
    # Check size first
    size_resp = await self._client.post(
        f"/sessions/{self.session_id}/execute",
        json={"code": f"import os; print(os.path.getsize('{path}'))"},
    )
    size = int(size_resp.json()["stdout"].strip())
    if size > 10 * 1024 * 1024:
        raise ValueError(f"File too large for download ({size} bytes). Max 10MB.")
    # Then read via base64
    ...
```

## 12. Error Handling

| Scenario | Detection | User Experience |
|----------|-----------|----------------|
| Code execution error | `result["success"] == False` | LLM sees the error, explains it, and retries with fixed code |
| Sandbox session dies | Execute returns connection error | Agent creates a new session, tells user "Session restarted — previous variables are lost" |
| LLM provider rate limited | 429 response from API | Retry with exponential backoff (3 attempts). Show "Thinking... (retry)" |
| LLM provider down | Connection error | Show "LLM service unavailable. Please try again." |
| File upload too large | Size check before upload | Show "File too large (X MB). Maximum is 50MB." |
| File download too large | Size check before download | Show "File too large for download (X MB). Maximum is 10MB." |
| download_file on missing path | Execute returns FileNotFoundError | LLM sees error: "File not found: /data/report.csv — save it first with execute_python_code" |
| Browser refresh | Chainlit on_chat_end fires | Old session cleaned up. New session created on reconnect. |

```python
async def _execute_with_recovery(self, code: str) -> dict:
    try:
        resp = await self._client.post(
            f"/sessions/{self.session_id}/execute",
            json={"code": code},
        )
        return resp.json()
    except (httpx.ConnectError, httpx.ReadError):
        # Session died — recreate
        await self.start_session()
        return {"success": False, "stdout": "", "stderr": "",
                "error": {"name": "SessionRestarted",
                          "value": "Sandbox session was restarted. Previous variables are lost.",
                          "traceback": []},
                "outputs": [], "execution_count": 0}
```

## 13. Iframe Security

Use restrictive Content-Security-Policy instead of blanket `X-Frame-Options ALLOWALL`:

```
# In config/Caddyfile, update the dashboard route:
header Content-Security-Policy "frame-ancestors 'self' localhost:8501 127.0.0.1:8501"
```

This restricts iframe embedding to only the Chainlit host. External sites cannot embed dashboards.

## 14. Dependencies


```toml
# New optional dependency group in pyproject.toml
[dependency-groups]
apps = [
    "chainlit>=2.0",
    "anthropic>=0.40",
    "openai>=1.0",
]
```

`anthropic` is the default provider. `openai` is needed for OpenAI and Ollama providers (Ollama uses OpenAI-compatible API). No dependency needed for Ollama itself — it's a local server.

Chainlit brings its own FastAPI server — runs on port 8501 by default. Does NOT conflict with the Execution API on port 8000.

## 15. Running the App

```bash
# Prerequisites: Execution API + Kernel Gateway + Pool Manager running
# Set Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...

# Run the chatbot
cd apps/data_analyst
chainlit run app.py --port 8501

# Or with custom API URL
EXECUTION_API_URL=http://my-host:8000 chainlit run app.py
```

## 16. Testing Plan

| Test | Type | What it verifies |
|------|------|-----------------|
| Agent creates session on start | Unit | POST /sessions called, session_id stored |
| Agent destroys session on end | Unit | DELETE /sessions/{id} called |
| File upload small (< 5MB) | Unit | Single base64 encode + execute |
| File upload chunked (> 5MB) | Unit | Multiple execute calls with append mode |
| File upload rejects > 50MB | Unit | ValueError raised |
| File download reads via base64 | Unit | Execute + decode, FileDownload yielded |
| File download rejects > 10MB | Unit | Size check, ValueError raised |
| File download missing path | Unit | Friendly error returned to LLM |
| Tool loop runs to completion | Unit | Multiple tool_use → text response cycle |
| Image extraction from outputs | Unit | data_b64 decoded, ImageOutput yielded |
| Dashboard URL returned | Unit | POST /dashboard, DashboardLink yielded |
| Error handling in tool execution | Unit | Failed execution → error message, agent continues |
| Session recovery on sandbox death | Unit | New session created, user notified |
| Context window compaction | Unit | Old tool results truncated, recent kept in full |
| LLM provider Anthropic | Unit | Tool format correct, message format correct |
| LLM provider OpenAI | Unit | input_schema → parameters, message format converted |
| LLM provider Ollama | Unit | Same as OpenAI with custom base_url |
| Full conversation E2E | Integration | Upload CSV → ask question → get chart → download → dashboard |

## 17. File Inventory

| File | New/Modify | Responsibility |
|------|-----------|---------------|
| `apps/data_analyst/agent.py` | New | DataAnalystAgent class — session, upload, download, chat loop |
| `apps/data_analyst/llm_provider.py` | New | LLMProvider protocol + Anthropic/OpenAI/Ollama implementations |
| `apps/data_analyst/app.py` | New | Chainlit handlers — on_start, on_message, on_end |
| `apps/data_analyst/config.py` | New | Configuration, system prompt, tool definitions, size limits |
| `apps/data_analyst/.chainlit/config.toml` | New | Chainlit UI settings |
| `apps/data_analyst/chainlit.md` | New | Welcome screen content |
| `pyproject.toml` | Modify | Add `apps` dependency group |
| `tests/test_data_analyst.py` | New | Unit tests for agent |
