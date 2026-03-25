# Data Analytics Agent Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Chainlit-based data analytics chatbot that uses any LLM with tool calling (Claude, GPT, Ollama) to execute Python code in Firecracker sandboxes, with file upload/download, inline chart display, and embedded interactive Panel dashboards.

**Architecture:** Five-file app in `apps/data_analyst/`. `llm_provider.py` defines the pluggable LLM interface (Protocol + 3 implementations). `agent.py` manages sandbox sessions and runs the ReAct tool loop. `app.py` wires Chainlit events to the agent. `config.py` holds all configuration, tools, and system prompt. Chainlit config and welcome screen are static files.

**Tech Stack:** Python 3.11+, Chainlit >=2.0, anthropic >=0.40, openai >=1.0, httpx, Execution API (existing)

**Spec:** `docs/superpowers/specs/2026-03-25-data-analyst-agent-design.md`

**Status:** Not started.

| Chunk | Status |
|-------|--------|
| 1: Dependencies + Config + Chainlit Setup (Task 1) | Not started |
| 2: LLM Provider Abstraction (Tasks 2-3) | Not started |
| 3: Agent Core (Tasks 4-5) | Not started |
| 4: Chainlit App + Integration (Tasks 6-7) | Not started |
| 5: Caddyfile Update + E2E Test (Task 8) | Not started |

---

## File Map

| File | Responsibility | Dependencies |
|------|---------------|--------------|
| `apps/data_analyst/config.py` | Configuration, system prompt, tools, size limits | None |
| `apps/data_analyst/llm_provider.py` | LLMProvider protocol + Anthropic/OpenAI/Ollama | anthropic, openai |
| `apps/data_analyst/agent.py` | DataAnalystAgent — session, upload, download, chat loop | httpx, llm_provider |
| `apps/data_analyst/app.py` | Chainlit handlers — on_start, on_message, on_end | chainlit, agent |
| `apps/data_analyst/.chainlit/config.toml` | Chainlit UI settings (file upload, theme) | None |
| `apps/data_analyst/chainlit.md` | Welcome screen content | None |
| `config/Caddyfile` | Update iframe CSP headers for dashboard embedding | None |
| `pyproject.toml` | Add `apps` dependency group | None |
| `tests/test_data_analyst.py` | Unit tests for agent + llm_provider | httpx, pytest |

---

## Chunk 1: Dependencies + Config + Chainlit Setup

### Task 1: Project setup, config, and Chainlit static files

**Files:**
- Modify: `pyproject.toml`
- Create: `apps/data_analyst/config.py`
- Create: `apps/data_analyst/.chainlit/config.toml`
- Create: `apps/data_analyst/chainlit.md`

- [ ] **Step 1: Add dependencies to pyproject.toml**

```toml
[dependency-groups]
apps = [
    "chainlit>=2.0",
    "anthropic>=0.40",
    "openai>=1.0",
]
```

Run: `uv sync --group apps`

- [ ] **Step 2: Create config.py**

```python
import os

EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")
CADDY_BASE_URL = os.environ.get("CADDY_BASE_URL", "http://localhost:8080")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514")

UPLOAD_MAX_BYTES = 50 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
DOWNLOAD_MAX_BYTES = 10 * 1024 * 1024

MAX_HISTORY_TOKENS = 80_000
RECENT_KEEP_FULL = 10
TRUNCATED_OUTPUT_CHARS = 200

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
            "First use execute_python_code to create the file (e.g., df.to_csv, plt.savefig), "
            "then call this tool with the file path to deliver it to the user."
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

- [ ] **Step 3: Create Chainlit config**

`apps/data_analyst/.chainlit/config.toml`:
```toml
[project]
name = "Data Analyst"

[features.spontaneous_file_upload]
enabled = true
accept = ["text/csv", "application/json", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/plain", "application/vnd.apache.parquet"]
max_files = 5
max_size_mb = 50

[UI]
name = "Data Analyst"
description = "Upload data, ask questions, get analysis + interactive dashboards"
```

`apps/data_analyst/chainlit.md`:
```markdown
# 📊 Data Analyst

Upload a CSV, Excel, or JSON file and ask questions about your data.

**Examples:**
- "Summarize this dataset"
- "What are the top 10 products by revenue?"
- "Show me the correlation matrix"
- "Create an interactive dashboard for exploration"
- "Export the cleaned data as CSV"

**Capabilities:**
- Python data analysis (pandas, numpy, scipy)
- Static visualizations (matplotlib, seaborn, plotly)
- Interactive dashboards (Panel + hvPlot)
- File download (CSV, Excel, PDF, images)
```

- [ ] **Step 4: Verify Chainlit installs**

Run: `uv run --group apps python -c "import chainlit; print(chainlit.__version__)"`
Expected: Version printed without error

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml apps/data_analyst/config.py apps/data_analyst/.chainlit/ apps/data_analyst/chainlit.md
git commit -m "feat(agent): add config, tools, Chainlit setup for data analyst app"
```

---

## Chunk 2: LLM Provider Abstraction

### Task 2: LLMProvider protocol and Anthropic implementation

**Files:**
- Create: `apps/data_analyst/llm_provider.py`
- Create: `tests/test_data_analyst.py` (provider tests)

- [ ] **Step 1: Write provider tests**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from apps.data_analyst.llm_provider import (
    AnthropicProvider, OpenAIProvider, OllamaProvider,
    LLMResponse, ToolCall, create_provider,
)

class TestAnthropicProvider:
    async def test_chat_returns_text(self):
        provider = AnthropicProvider(model="test")
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=MagicMock(
            content=[MagicMock(type="text", text="Hello")],
            stop_reason="end_turn",
        ))
        provider.client = mock_client
        resp = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            system="You are helpful",
            tools=[],
        )
        assert resp.text == "Hello"
        assert resp.stop_reason == "end"
        assert resp.tool_calls == []

    async def test_chat_returns_tool_use(self):
        provider = AnthropicProvider(model="test")
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=MagicMock(
            content=[
                MagicMock(type="tool_use", id="t1", name="execute_python_code",
                          input={"code": "print(1)"}),
            ],
            stop_reason="tool_use",
        ))
        provider.client = mock_client
        resp = await provider.chat(
            messages=[{"role": "user", "content": "run code"}],
            system="test", tools=[{"name": "execute_python_code", "input_schema": {}}],
        )
        assert resp.stop_reason == "tool_use"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "execute_python_code"

class TestOpenAIProvider:
    async def test_tool_format_conversion(self):
        provider = OpenAIProvider(model="test")
        tools = [{"name": "test", "description": "desc", "input_schema": {"type": "object"}}]
        oai_tools = provider._convert_tools(tools)
        assert oai_tools[0]["type"] == "function"
        assert oai_tools[0]["function"]["parameters"] == {"type": "object"}

class TestCreateProvider:
    def test_anthropic_default(self):
        provider = create_provider("anthropic", "claude-sonnet-4-20250514")
        assert isinstance(provider, AnthropicProvider)

    def test_openai(self):
        with patch("apps.data_analyst.llm_provider.openai"):
            provider = create_provider("openai", "gpt-4o")
        assert isinstance(provider, OpenAIProvider)

    def test_ollama(self):
        provider = create_provider("ollama", "llama3.1")
        assert isinstance(provider, OllamaProvider)

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            create_provider("gemini", "model")
```

- [ ] **Step 2: Implement llm_provider.py**

```python
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
import json


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
    raw_content: list = field(default_factory=list)


@runtime_checkable
class LLMProvider(Protocol):
    async def chat(
        self, messages: list[dict], system: str, tools: list[dict],
    ) -> LLMResponse: ...

    def format_tool_result(self, tool_call_id: str, content: str) -> dict: ...


class AnthropicProvider:
    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        import anthropic
        self.client = anthropic.AsyncAnthropic()
        self.model = model

    async def chat(self, messages, system, tools) -> LLMResponse:
        response = await self.client.messages.create(
            model=self.model, max_tokens=4096,
            system=system, tools=tools, messages=messages,
        )
        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        stop = "tool_use" if response.stop_reason == "tool_use" else "end"
        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=stop,
            raw_content=response.content,
        )

    def format_tool_result(self, tool_call_id: str, content: str) -> dict:
        return {"type": "tool_result", "tool_use_id": tool_call_id, "content": content}


class OpenAIProvider:
    def __init__(self, model: str = "gpt-4o"):
        import openai
        self.client = openai.AsyncOpenAI()
        self.model = model

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t["input_schema"],
        }} for t in tools]

    def _convert_messages(self, messages: list[dict], system: str) -> list[dict]:
        oai = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant" and isinstance(m.get("content"), list):
                # Anthropic assistant with tool_use blocks
                text = ""
                tool_calls = []
                for block in m["content"]:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            text += block.text
                        elif block.type == "tool_use":
                            tool_calls.append({
                                "id": block.id, "type": "function",
                                "function": {"name": block.name, "arguments": json.dumps(block.input)},
                            })
                msg = {"role": "assistant", "content": text or None}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                oai.append(msg)
            elif m["role"] == "user" and isinstance(m.get("content"), list):
                # Tool results
                for block in m["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        oai.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block.get("content", ""),
                        })
            else:
                oai.append({"role": m["role"], "content": m.get("content", "")})
        return oai

    async def chat(self, messages, system, tools) -> LLMResponse:
        oai_tools = self._convert_tools(tools)
        oai_messages = self._convert_messages(messages, system)
        response = await self.client.chat.completions.create(
            model=self.model, tools=oai_tools if oai_tools else None,
            messages=oai_messages,
        )
        choice = response.choices[0]
        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id, name=tc.function.name,
                    input=json.loads(tc.function.arguments),
                ))
        stop = "tool_use" if choice.finish_reason == "tool_calls" else "end"
        return LLMResponse(
            text=choice.message.content,
            tool_calls=tool_calls,
            stop_reason=stop,
            raw_content=[choice.message],
        )

    def format_tool_result(self, tool_call_id: str, content: str) -> dict:
        return {"type": "tool_result", "tool_use_id": tool_call_id, "content": content}


class OllamaProvider(OpenAIProvider):
    def __init__(self, model: str = "llama3.1", base_url: str = "http://localhost:11434/v1"):
        import openai
        self.client = openai.AsyncOpenAI(base_url=base_url, api_key="ollama")
        self.model = model


def create_provider(provider_name: str, model: str) -> LLMProvider:
    if provider_name == "anthropic":
        return AnthropicProvider(model=model)
    elif provider_name == "openai":
        return OpenAIProvider(model=model)
    elif provider_name == "ollama":
        return OllamaProvider(model=model)
    else:
        raise ValueError(f"Unknown LLM provider: {provider_name}")
```

- [ ] **Step 3: Run tests**

Run: `uv run --group apps pytest tests/test_data_analyst.py -v`
Expected: All provider tests pass

- [ ] **Step 4: Commit**

```bash
git add apps/data_analyst/llm_provider.py tests/test_data_analyst.py
git commit -m "feat(agent): LLM provider abstraction — Anthropic, OpenAI, Ollama"
```

---

### Task 3: Provider message format round-trip tests

**Files:**
- Modify: `tests/test_data_analyst.py`

- [ ] **Step 1: Add message format tests**

Test that tool_result messages round-trip correctly through each provider's format conversion. Test the Anthropic → assistant message → tool_result → next request cycle. Test OpenAI message conversion with mixed text + tool_calls.

- [ ] **Step 2: Run tests**

Run: `uv run --group apps pytest tests/test_data_analyst.py -v`

- [ ] **Step 3: Commit**

```bash
git add tests/test_data_analyst.py
git commit -m "test(agent): provider message format round-trip tests"
```

---

## Chunk 3: Agent Core

### Task 4: DataAnalystAgent — session management + file upload/download

**Files:**
- Create: `apps/data_analyst/agent.py`
- Modify: `tests/test_data_analyst.py`

- [ ] **Step 1: Write agent tests for session + file I/O**

Tests:
- `test_start_session_creates_sandbox` — POST /sessions called, session_id stored
- `test_end_session_deletes_sandbox` — DELETE /sessions/{id} called
- `test_upload_small_file` — single base64 execute for < 5MB
- `test_upload_chunked_file` — multiple execute calls for > 5MB
- `test_upload_rejects_oversized` — ValueError for > 50MB
- `test_download_file_reads_via_base64` — execute + decode
- `test_download_rejects_oversized` — size check fails
- `test_download_missing_file` — friendly error

- [ ] **Step 2: Implement agent.py — session + file methods**

```python
import base64
import logging
import mimetypes
from dataclasses import dataclass, field
from typing import AsyncGenerator

import httpx

from .config import (
    CADDY_BASE_URL, DOWNLOAD_MAX_BYTES, EXECUTION_API_URL,
    RECENT_KEEP_FULL, SYSTEM_PROMPT, TOOLS, TRUNCATED_OUTPUT_CHARS,
    UPLOAD_CHUNK_SIZE, UPLOAD_MAX_BYTES,
)
from .llm_provider import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


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
    url: str
    full_url: str

@dataclass
class FileDownload:
    filename: str
    data: bytes
    mime_type: str


AgentEvent = TextDelta | ToolStart | ToolResult | ImageOutput | DashboardLink | FileDownload


class DataAnalystAgent:
    def __init__(self, api_url: str, provider: LLMProvider):
        self.api_url = api_url
        self.provider = provider
        self.session_id: str | None = None
        self.messages: list[dict] = []
        self._client: httpx.AsyncClient | None = None

    async def start_session(self) -> str:
        self._client = httpx.AsyncClient(base_url=self.api_url, timeout=120.0)
        resp = await self._client.post("/sessions")
        resp.raise_for_status()
        self.session_id = resp.json()["session_id"]
        await self._execute("import numpy, pandas\nimport matplotlib; matplotlib.use('Agg')")
        return self.session_id

    async def end_session(self) -> None:
        if self._client and self.session_id:
            try:
                await self._client.delete(f"/sessions/{self.session_id}")
            except Exception:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None
        self.session_id = None

    async def upload_file(self, filename: str, content: bytes) -> str:
        if len(content) > UPLOAD_MAX_BYTES:
            raise ValueError(f"File too large ({len(content)} bytes). Max {UPLOAD_MAX_BYTES // (1024*1024)}MB.")
        if len(content) <= UPLOAD_CHUNK_SIZE:
            return await self._upload_single(filename, content)
        return await self._upload_chunked(filename, content)

    async def _upload_single(self, filename: str, content: bytes) -> str:
        b64 = base64.b64encode(content).decode()
        code = (
            f"import base64, os\n"
            f"os.makedirs('/data', exist_ok=True)\n"
            f"data = base64.b64decode('{b64}')\n"
            f"with open('/data/{filename}', 'wb') as f:\n"
            f"    f.write(data)\n"
            f"print(f'Saved /data/{filename} ({{len(data)}} bytes)')"
        )
        result = await self._execute(code)
        return result.get("stdout", "").strip()

    async def _upload_chunked(self, filename: str, content: bytes) -> str:
        total = len(content)
        path = f"/data/{filename}"
        for offset in range(0, total, UPLOAD_CHUNK_SIZE):
            chunk = content[offset:offset + UPLOAD_CHUNK_SIZE]
            b64 = base64.b64encode(chunk).decode()
            mode = "wb" if offset == 0 else "ab"
            code = (
                f"import base64, os\n"
                f"os.makedirs('/data', exist_ok=True)\n"
                f"with open('{path}', '{mode}') as f:\n"
                f"    f.write(base64.b64decode('{b64}'))\n"
                f"print('chunk {offset // UPLOAD_CHUNK_SIZE + 1}')"
            )
            await self._execute(code)
        return f"Saved {path} ({total} bytes, chunked)"

    async def download_file(self, path: str) -> FileDownload:
        size_result = await self._execute(f"import os; print(os.path.getsize('{path}'))")
        if not size_result.get("success"):
            error = size_result.get("error", {}).get("value", "unknown error")
            raise FileNotFoundError(f"Cannot read {path}: {error}")
        size = int(size_result["stdout"].strip())
        if size > DOWNLOAD_MAX_BYTES:
            raise ValueError(f"File too large ({size} bytes). Max {DOWNLOAD_MAX_BYTES // (1024*1024)}MB.")

        read_result = await self._execute(
            f"import base64\n"
            f"with open('{path}', 'rb') as f:\n"
            f"    print(base64.b64encode(f.read()).decode())"
        )
        data = base64.b64decode(read_result["stdout"].strip())
        filename = path.rsplit("/", 1)[-1]
        mime, _ = mimetypes.guess_type(filename)
        return FileDownload(filename=filename, data=data, mime_type=mime or "application/octet-stream")

    async def _execute(self, code: str) -> dict:
        resp = await self._client.post(
            f"/sessions/{self.session_id}/execute",
            json={"code": code},
        )
        return resp.json()

    # chat() method in Task 5
```

- [ ] **Step 3: Run tests**

Run: `uv run --group apps pytest tests/test_data_analyst.py -v`

- [ ] **Step 4: Commit**

```bash
git add apps/data_analyst/agent.py tests/test_data_analyst.py
git commit -m "feat(agent): DataAnalystAgent — session management + file upload/download"
```

---

### Task 5: Agent chat loop + context management

**Files:**
- Modify: `apps/data_analyst/agent.py`
- Modify: `tests/test_data_analyst.py`

- [ ] **Step 1: Write chat loop tests**

Tests:
- `test_chat_text_response` — LLM returns text, yields TextDelta
- `test_chat_tool_use_loop` — LLM returns tool_use, agent executes, feeds back
- `test_chat_image_extracted` — execute returns image output, yields ImageOutput
- `test_chat_dashboard_launched` — launch_dashboard tool, yields DashboardLink
- `test_chat_download_file` — download_file tool, yields FileDownload
- `test_chat_context_compaction` — after many messages, old tool results truncated
- `test_chat_session_recovery` — execute fails with connection error, session recreated

- [ ] **Step 2: Implement chat() + compact_messages()**

Add to `agent.py`:

```python
    async def chat(self, user_message: str) -> AsyncGenerator[AgentEvent, None]:
        self.messages.append({"role": "user", "content": user_message})
        self.messages = self._compact_messages(self.messages)

        response = await self.provider.chat(
            messages=self.messages, system=SYSTEM_PROMPT, tools=TOOLS,
        )

        while response.stop_reason == "tool_use":
            self.messages.append({"role": "assistant", "content": response.raw_content})
            tool_results = []

            for tc in response.tool_calls:
                yield ToolStart(tool_name=tc.name, code=str(tc.input.get("code", tc.input)))

                if tc.name == "execute_python_code":
                    result = await self._execute_with_recovery(tc.input["code"])
                    output = self._format_result(result)
                    yield ToolResult(tool_name=tc.name, output=output, success=result.get("success", False))
                    for img in self._extract_images(result):
                        yield img
                    tool_results.append(self.provider.format_tool_result(tc.id, output))

                elif tc.name == "launch_dashboard":
                    result = await self._launch_dashboard(tc.input["code"])
                    tool_results.append(self.provider.format_tool_result(tc.id, result["text"]))
                    if result.get("link"):
                        yield result["link"]

                elif tc.name == "download_file":
                    try:
                        fd = await self.download_file(tc.input["path"])
                        yield fd
                        tool_results.append(self.provider.format_tool_result(
                            tc.id, f"File {fd.filename} sent to user for download"))
                    except (FileNotFoundError, ValueError) as e:
                        tool_results.append(self.provider.format_tool_result(tc.id, str(e)))
                        yield ToolResult(tool_name=tc.name, output=str(e), success=False)

            self.messages.append({"role": "user", "content": tool_results})
            self.messages = self._compact_messages(self.messages)
            response = await self.provider.chat(
                messages=self.messages, system=SYSTEM_PROMPT, tools=TOOLS,
            )

        self.messages.append({"role": "assistant", "content": response.raw_content})
        if response.text:
            yield TextDelta(text=response.text)

    async def _execute_with_recovery(self, code: str) -> dict:
        try:
            return await self._execute(code)
        except (httpx.ConnectError, httpx.ReadError):
            await self.start_session()
            return {
                "success": False, "stdout": "", "stderr": "",
                "error": {"name": "SessionRestarted",
                          "value": "Sandbox restarted. Previous variables lost.",
                          "traceback": []},
                "outputs": [], "execution_count": 0,
            }

    async def _launch_dashboard(self, code: str) -> dict:
        resp = await self._client.post(
            f"/sessions/{self.session_id}/dashboard",
            json={"code": code},
        )
        if resp.status_code == 200:
            data = resp.json()
            url = data.get("url", "")
            full_url = f"{CADDY_BASE_URL}{url}"
            return {"text": f"Dashboard at {full_url}", "link": DashboardLink(url=url, full_url=full_url)}
        return {"text": f"Dashboard failed: {resp.text}"}

    @staticmethod
    def _format_result(data: dict) -> str:
        parts = []
        if data.get("stdout"):
            parts.append(data["stdout"])
        if data.get("stderr"):
            parts.append(f"[stderr]: {data['stderr']}")
        if data.get("error"):
            err = data["error"]
            parts.append(f"[error]: {err['name']}: {err['value']}")
        for i, out in enumerate(data.get("outputs", [])):
            mime = out.get("mime_type", "")
            if out.get("data_b64"):
                parts.append(f"[output {i}]: {mime} (image)")
            elif out.get("data"):
                parts.append(f"[output {i}]: {mime}\n{out['data'][:2000]}")
        return "\n".join(parts) or "(no output)"

    @staticmethod
    def _extract_images(data: dict) -> list[ImageOutput]:
        images = []
        for out in data.get("outputs", []):
            if out.get("data_b64") and "image" in out.get("mime_type", ""):
                images.append(ImageOutput(
                    data=base64.b64decode(out["data_b64"]),
                    mime_type=out["mime_type"],
                ))
        return images

    @staticmethod
    def _compact_messages(messages: list[dict]) -> list[dict]:
        tool_indices = [
            i for i, m in enumerate(messages)
            if m["role"] == "user" and isinstance(m.get("content"), list)
        ]
        if len(tool_indices) <= RECENT_KEEP_FULL:
            return messages

        old_indices = set(tool_indices[:-RECENT_KEEP_FULL])
        compacted = []
        for i, m in enumerate(messages):
            if i in old_indices:
                content = []
                for block in m["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        text = block.get("content", "")
                        if len(text) > TRUNCATED_OUTPUT_CHARS:
                            text = text[:TRUNCATED_OUTPUT_CHARS] + "... [truncated]"
                        content.append({**block, "content": text})
                    else:
                        content.append(block)
                compacted.append({**m, "content": content})
            else:
                compacted.append(m)
        return compacted
```

- [ ] **Step 3: Run tests**

Run: `uv run --group apps pytest tests/test_data_analyst.py -v`

- [ ] **Step 4: Commit**

```bash
git add apps/data_analyst/agent.py tests/test_data_analyst.py
git commit -m "feat(agent): chat loop with tool dispatch, context compaction, session recovery"
```

---

## Chunk 4: Chainlit App + Integration

### Task 6: Chainlit app handlers

**Files:**
- Create: `apps/data_analyst/app.py`

- [ ] **Step 1: Implement app.py**

```python
import chainlit as cl

from .agent import (
    AgentEvent, DataAnalystAgent, DashboardLink, FileDownload,
    ImageOutput, TextDelta, ToolResult, ToolStart,
)
from .config import CADDY_BASE_URL, EXECUTION_API_URL, LLM_MODEL, LLM_PROVIDER
from .llm_provider import create_provider


@cl.on_chat_start
async def on_start():
    provider = create_provider(LLM_PROVIDER, LLM_MODEL)
    agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
    await agent.start_session()
    cl.user_session.set("agent", agent)
    await cl.Message(content="Ready! Upload a data file or ask a question.").send()


@cl.on_message
async def on_message(message: cl.Message):
    agent: DataAnalystAgent = cl.user_session.get("agent")

    for el in message.elements or []:
        content = open(el.path, "rb").read() if hasattr(el, "path") else el.content
        try:
            result = await agent.upload_file(el.name, content)
            await cl.Message(content=f"📁 Uploaded `{el.name}` — {result}").send()
        except ValueError as e:
            await cl.Message(content=f"❌ {e}").send()
            return

    response_msg = cl.Message(content="")
    await response_msg.send()

    current_step = None
    async for event in agent.chat(message.content):
        if isinstance(event, TextDelta):
            await response_msg.stream_token(event.text)

        elif isinstance(event, ToolStart):
            current_step = cl.Step(name=event.tool_name, type="tool")
            current_step.input = event.code
            await current_step.send()

        elif isinstance(event, ToolResult):
            if current_step:
                current_step.output = event.output[:500]
                await current_step.update()
                current_step = None

        elif isinstance(event, ImageOutput):
            img = cl.Image(content=event.data, name="plot.png", display="inline")
            response_msg.elements = response_msg.elements or []
            response_msg.elements.append(img)

        elif isinstance(event, DashboardLink):
            iframe = (
                f'<iframe src="{event.full_url}" '
                f'width="100%" height="600" frameborder="0" '
                f'style="border-radius: 8px; border: 1px solid #e0e0e0;"></iframe>'
            )
            await cl.Message(
                content=f"📊 Interactive dashboard ([open full screen]({event.full_url}))",
                elements=[cl.Text(name="dashboard", content=iframe, display="inline")],
            ).send()

        elif isinstance(event, FileDownload):
            await cl.Message(
                content=f"📎 **{event.filename}** ready for download",
                elements=[cl.File(name=event.filename, content=event.data, display="inline")],
            ).send()

    await response_msg.update()


@cl.on_chat_end
async def on_end():
    agent: DataAnalystAgent = cl.user_session.get("agent")
    if agent:
        await agent.end_session()
```

- [ ] **Step 2: Verify Chainlit starts**

Run: `cd apps/data_analyst && uv run --group apps chainlit run app.py --port 8501 &`
Expected: Chainlit starts on port 8501, no import errors

- [ ] **Step 3: Commit**

```bash
git add apps/data_analyst/app.py
git commit -m "feat(agent): Chainlit app — chat, file upload, images, dashboards, downloads"
```

---

### Task 7: Unit tests for full agent flow

**Files:**
- Modify: `tests/test_data_analyst.py`

- [ ] **Step 1: Add full-flow agent tests**

Mock httpx client + LLM provider. Verify:
- `test_full_flow_upload_analyze_download` — upload file → chat → get chart → download result
- `test_provider_switching` — create_provider returns correct type per name
- `test_compact_messages_truncates_old` — verify truncation logic

- [ ] **Step 2: Run all tests**

Run: `uv run --group apps pytest tests/test_data_analyst.py -v`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_data_analyst.py
git commit -m "test(agent): full agent flow + compaction + provider switching tests"
```

---

## Chunk 5: Caddyfile Update + Documentation

### Task 8: Iframe security headers + README + commit

**Files:**
- Modify: `config/Caddyfile`
- Modify: `README.md`

- [ ] **Step 1: Update Caddyfile with iframe CSP**

```
:8080 {
    header Content-Security-Policy "frame-ancestors 'self' localhost:8501 127.0.0.1:8501"

    handle /api/kernels/* {
        reverse_proxy localhost:8888
    }
    handle /artifacts/* {
        root * /var/lib/sandbox-artifacts
        file_server
    }
    handle /health {
        respond "ok" 200
    }
}
```

- [ ] **Step 2: Update README with Data Analyst section**

Add after the Execution API section:

```markdown
## Data Analyst Agent

Interactive data analysis chatbot powered by LLMs + sandboxed Python execution.

\```bash
# Prerequisites: Execution API + Kernel Gateway + Pool Manager running
export ANTHROPIC_API_KEY=sk-ant-...
cd apps/data_analyst
uv run --group apps chainlit run app.py --port 8501
\```

Supports any LLM with tool calling:
\```bash
# OpenAI
LLM_PROVIDER=openai LLM_MODEL=gpt-4o OPENAI_API_KEY=sk-... chainlit run app.py

# Local (Ollama)
LLM_PROVIDER=ollama LLM_MODEL=llama3.1 chainlit run app.py
\```

Features: file upload/download, inline charts, embedded Panel dashboards, conversation memory.
```

- [ ] **Step 3: Final test suite**

Run: `uv run pytest tests/ -q -m "not integration"`
Expected: All unit tests pass (486 existing + new agent tests)

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat(agent): data analyst chatbot — Chainlit + multi-LLM + sandboxed execution

Complete sample app: upload data → ask questions → get charts + dashboards + downloads.
Pluggable LLM: Claude, GPT-4o, Ollama. Embedded Panel dashboards via iframe.
Context window management, chunked upload, size-limited download."
```
