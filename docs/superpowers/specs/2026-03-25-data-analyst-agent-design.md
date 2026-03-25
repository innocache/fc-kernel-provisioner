# Data Analytics Agent — Design Spec

## 1. Summary

A sample data analytics agent that combines Claude's reasoning with sandboxed Python execution. Users upload data files, ask analytical questions in natural language, and get results as inline charts, tables, and interactive Panel dashboards — all through a Chainlit chat interface.

## 2. Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Frontend | Chainlit | Purpose-built for LLM chat+tools: step visualization, file upload, image display, streaming |
| API layer | Execution API REST | Agent is just an HTTP client. Decoupled from infrastructure internals. |
| LLM | Claude (Anthropic) | tool_use for structured code generation, strong at data analysis |
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
├── agent.py              # DataAnalystAgent class — Claude + tool execution loop
├── app.py                # Chainlit handlers — on_chat_start, on_message, on_chat_end
├── config.py             # Configuration (API URL, model, system prompt)
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
- launch_dashboard: Create an interactive Panel dashboard (opens in browser tab)

WORKFLOW:
1. When data is uploaded, immediately load it and show df.head(), df.shape, df.dtypes
2. For analysis questions, write and execute Python code step by step
3. Show intermediate results — print DataFrames, statistics, value counts
4. For visualizations, use matplotlib (plots appear inline in chat)
5. For interactive exploration, use launch_dashboard with Panel + hvPlot

RULES:
- Always use matplotlib.use('Agg') before importing pyplot
- Print results explicitly — the chat only sees stdout and images
- For large DataFrames, show .head() or .describe(), not the full frame
- Handle errors gracefully — if code fails, explain and retry
- Be concise in explanations, let the data speak"""

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
]
```

### 6.2 agent.py

Core responsibilities:
- Manage sandbox session lifecycle (create on chat start, destroy on end)
- Upload files to sandbox via code execution
- Run the Claude tool_use loop
- Extract images from execution results
- Track dashboard URLs

```python
class DataAnalystAgent:
    def __init__(self, api_url, model):
        self.api_url = api_url
        self.model = model
        self.session_id = None
        self.messages = []
        self._client = httpx.AsyncClient(...)
        self._anthropic = anthropic.AsyncAnthropic()

    async def start_session(self) -> None:
        # POST /sessions → store session_id
        # Pre-warm: execute "import numpy, pandas, matplotlib; matplotlib.use('Agg')"

    async def upload_file(self, filename: str, content: bytes) -> str:
        # Base64-encode content, execute code to write to /data/{filename}
        # Return confirmation message

    async def chat(self, user_message: str) -> AsyncGenerator[AgentEvent, None]:
        # Yields events: TextDelta, ToolStart, ToolResult, ImageOutput, DashboardLink
        # Runs the Claude tool_use loop
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

**Capabilities:**
- Python data analysis (pandas, numpy, scipy)
- Static visualizations (matplotlib, seaborn, plotly)
- Interactive dashboards (Panel + hvPlot)
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

## 8. Image Display Flow

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

## 9. Dashboard Flow (Embedded)

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

## 10. Dependencies

```toml
# New optional dependency group in pyproject.toml
[dependency-groups]
apps = [
    "chainlit>=2.0",
    "anthropic>=0.40",
]
```

Chainlit brings its own FastAPI server — runs on port 8501 by default. Does NOT conflict with the Execution API on port 8000.

## 11. Running the App

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

## 12. Testing Plan

| Test | Type | What it verifies |
|------|------|-----------------|
| Agent creates session on start | Unit | POST /sessions called, session_id stored |
| Agent destroys session on end | Unit | DELETE /sessions/{id} called |
| File upload encodes + executes | Unit | base64 encoding, execute called with write code |
| Tool loop runs to completion | Unit | Multiple tool_use → text response cycle |
| Image extraction from outputs | Unit | data_b64 decoded, ImageOutput yielded |
| Dashboard URL returned | Unit | POST /dashboard, DashboardLink yielded |
| Error handling in tool execution | Unit | Failed execution → error message, agent continues |
| Full conversation E2E | Integration | Upload CSV → ask question → get chart → launch dashboard |

## 13. File Inventory

| File | New/Modify | Responsibility |
|------|-----------|---------------|
| `apps/data_analyst/agent.py` | New | DataAnalystAgent class — session, upload, chat loop |
| `apps/data_analyst/app.py` | New | Chainlit handlers — on_start, on_message, on_end |
| `apps/data_analyst/config.py` | New | Configuration, system prompt, tool definitions |
| `apps/data_analyst/.chainlit/config.toml` | New | Chainlit UI settings |
| `apps/data_analyst/chainlit.md` | New | Welcome screen content |
| `pyproject.toml` | Modify | Add `apps` dependency group |
| `tests/test_data_analyst.py` | New | Unit tests for agent |
