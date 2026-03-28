"""Integration tests for the data analyst agent.

Tests use a MockProvider (scripted LLM responses) but hit the real Execution API.
This verifies the agent ↔ sandbox wiring: file upload, code execution, image
generation, file download, and session lifecycle.

Prerequisites: Execution API + Kernel Gateway + Pool Manager running.
Run: uv run pytest tests/test_data_analyst_integration.py -v -m integration
"""

import base64
import os

import pytest

from apps.data_analyst.agent import (
    DataAnalystAgent, DashboardHTML, DashboardLink, FileDownload,
    ImageOutput, TextDelta, ToolResult, ToolStart,
)
from apps.data_analyst.llm_provider import LLMResponse, ToolCall

EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")

TEST_CSV = b"product,region,revenue,units\nWidget A,North,12500,250\nWidget B,South,8300,166\nWidget A,East,15200,304\nWidget C,North,6100,122\nWidget B,West,9800,196\n"


class MockProvider:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._call_count = 0

    async def chat(self, messages, system, tools) -> LLMResponse:
        resp = self._responses[self._call_count]
        self._call_count += 1
        return resp

    def format_tool_result(self, tool_call_id: str, content: str) -> dict:
        return {"type": "tool_result", "tool_use_id": tool_call_id, "content": content}


class TestAgentAnalysisFlow:
    async def test_upload_and_execute(self):
        provider = MockProvider([
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(
                    id="t1", name="execute_python_code",
                    input={"code": "import pandas as pd\ndf = pd.read_csv('/data/test.csv')\nprint(f'rows={df.shape[0]} cols={df.shape[1]}')"},
                )],
                stop_reason="tool_use",
                raw_content=[],
            ),
            LLMResponse(text="The dataset has 5 rows and 4 columns.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        try:
            upload_result = await agent.upload_file("test.csv", TEST_CSV)
            assert "Saved" in upload_result

            events = [e async for e in agent.chat("How many rows?")]
            tool_results = [e for e in events if isinstance(e, ToolResult)]
            assert len(tool_results) == 1
            assert "rows=5" in tool_results[0].output
            assert tool_results[0].success is True

            text_events = [e for e in events if isinstance(e, TextDelta)]
            assert any("5 rows" in e.text for e in text_events)
        finally:
            await agent.end_session()

    async def test_chart_generation(self):
        provider = MockProvider([
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(
                    id="t1", name="execute_python_code",
                    input={"code": (
                        "import matplotlib.pyplot as plt\n"
                        "plt.plot([1, 2, 3], [10, 20, 30])\n"
                        "plt.title('Test')\n"
                        "plt.show()"
                    )},
                )],
                stop_reason="tool_use",
                raw_content=[],
            ),
            LLMResponse(text="Here is the chart.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        try:
            events = [e async for e in agent.chat("Plot a line chart")]
            tool_results = [e for e in events if isinstance(e, ToolResult)]
            assert len(tool_results) == 1
            assert tool_results[0].success is True
            images = [e for e in events if isinstance(e, ImageOutput)]
            assert len(images) >= 1
            assert images[0].mime_type == "image/png"
        finally:
            await agent.end_session()

    async def test_multi_turn_state_persists(self):
        provider = MockProvider([
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="t1", name="execute_python_code", input={"code": "x = 42\nprint('set x')"})],
                stop_reason="tool_use", raw_content=[],
            ),
            LLMResponse(text="Variable x is set to 42.", tool_calls=[], stop_reason="end", raw_content=[]),
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="t2", name="execute_python_code", input={"code": "print(f'x = {x}')"})],
                stop_reason="tool_use", raw_content=[],
            ),
            LLMResponse(text="x is 42.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        try:
            events1 = [e async for e in agent.chat("Set x to 42")]
            assert any("set x" in e.output for e in events1 if isinstance(e, ToolResult))

            events2 = [e async for e in agent.chat("What is x?")]
            tool_results = [e for e in events2 if isinstance(e, ToolResult)]
            assert any("x = 42" in r.output for r in tool_results)
        finally:
            await agent.end_session()

    async def test_execution_error_reported(self):
        provider = MockProvider([
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="t1", name="execute_python_code", input={"code": "1/0"})],
                stop_reason="tool_use", raw_content=[],
            ),
            LLMResponse(text="There was a ZeroDivisionError.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        try:
            events = [e async for e in agent.chat("Divide by zero")]
            tool_results = [e for e in events if isinstance(e, ToolResult)]
            assert len(tool_results) == 1
            assert "ZeroDivisionError" in tool_results[0].output
            assert tool_results[0].success is False
        finally:
            await agent.end_session()


class TestAgentFileRoundTrip:
    async def test_upload_process_download(self):
        provider = MockProvider([
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(
                    id="t1", name="execute_python_code",
                    input={"code": (
                        "import pandas as pd\n"
                        "df = pd.read_csv('/data/input.csv')\n"
                        "df['revenue_per_unit'] = df['revenue'] / df['units']\n"
                        "df.to_csv('/data/output.csv', index=False)\n"
                        "print(f'Processed {len(df)} rows')"
                    )},
                )],
                stop_reason="tool_use", raw_content=[],
            ),
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="t2", name="download_file", input={"path": "/data/output.csv"})],
                stop_reason="tool_use", raw_content=[],
            ),
            LLMResponse(text="Here is the processed file.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        try:
            await agent.upload_file("input.csv", TEST_CSV)
            events = [e async for e in agent.chat("Add revenue_per_unit column and export")]

            downloads = [e for e in events if isinstance(e, FileDownload)]
            assert len(downloads) == 1
            assert downloads[0].filename == "output.csv"
            assert b"revenue_per_unit" in downloads[0].data
            assert downloads[0].mime_type == "text/csv"
        finally:
            await agent.end_session()

    async def test_download_nonexistent_file(self):
        provider = MockProvider([
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="t1", name="download_file", input={"path": "/data/nope.csv"})],
                stop_reason="tool_use", raw_content=[],
            ),
            LLMResponse(text="The file was not found.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        try:
            events = [e async for e in agent.chat("Download nope.csv")]
            errors = [e for e in events if isinstance(e, ToolResult) and not e.success]
            assert len(errors) == 1
            assert "Cannot read" in errors[0].output or "No such file" in errors[0].output
        finally:
            await agent.end_session()


class TestPlotlyDashboard:
    async def test_plotly_dashboard_renders(self):
        provider = MockProvider([
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(
                    id="t1", name="execute_python_code",
                    input={"code": (
                        "import plotly.express as px\n"
                        "import pandas as pd\n"
                        "df = pd.DataFrame({'x': [1,2,3,4,5], 'y': [10,20,15,30,25]})\n"
                        "fig = px.bar(df, x='x', y='y', title='Test Dashboard')\n"
                        "fig.write_html('/data/dashboard.html', include_plotlyjs='cdn')\n"
                        "print('DASHBOARD:/data/dashboard.html')"
                    )},
                )],
                stop_reason="tool_use",
                raw_content=[],
            ),
            LLMResponse(text="Dashboard is ready.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        try:
            events = [e async for e in agent.chat("Create a dashboard")]
            dashboards = [e for e in events if isinstance(e, DashboardHTML)]
            assert len(dashboards) == 1
            assert b"plotly" in dashboards[0].html.lower()
            assert b"Test Dashboard" in dashboards[0].html
            assert dashboards[0].filename == "dashboard.html"

            tool_results = [e for e in events if isinstance(e, ToolResult)]
            assert len(tool_results) == 1
            assert "DASHBOARD:" not in tool_results[0].output
        finally:
            await agent.end_session()

    async def test_plotly_dashboard_iteration(self):
        provider = MockProvider([
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(
                    id="t1", name="execute_python_code",
                    input={"code": (
                        "import plotly.express as px\n"
                        "import pandas as pd\n"
                        "df = pd.DataFrame({'x': [1,2,3], 'y': [10,20,30]})\n"
                        "fig = px.bar(df, x='x', y='y', title='Version 1')\n"
                        "fig.write_html('/data/dashboard.html', include_plotlyjs='cdn')\n"
                        "print('DASHBOARD:/data/dashboard.html')"
                    )},
                )],
                stop_reason="tool_use", raw_content=[],
            ),
            LLMResponse(text="Version 1 ready.", tool_calls=[], stop_reason="end", raw_content=[]),
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(
                    id="t2", name="execute_python_code",
                    input={"code": (
                        "import plotly.express as px\n"
                        "fig = px.scatter(df, x='x', y='y', title='Version 2')\n"
                        "fig.write_html('/data/dashboard.html', include_plotlyjs='cdn')\n"
                        "print('DASHBOARD:/data/dashboard.html')"
                    )},
                )],
                stop_reason="tool_use", raw_content=[],
            ),
            LLMResponse(text="Updated to scatter.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        try:
            events1 = [e async for e in agent.chat("Create a bar chart dashboard")]
            d1 = [e for e in events1 if isinstance(e, DashboardHTML)]
            assert len(d1) == 1
            assert b"Version 1" in d1[0].html

            events2 = [e async for e in agent.chat("Change to scatter")]
            d2 = [e for e in events2 if isinstance(e, DashboardHTML)]
            assert len(d2) == 1
            assert b"Version 2" in d2[0].html
        finally:
            await agent.end_session()

    async def test_no_dashboard_without_marker(self):
        provider = MockProvider([
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(
                    id="t1", name="execute_python_code",
                    input={"code": "print('hello world')"},
                )],
                stop_reason="tool_use", raw_content=[],
            ),
            LLMResponse(text="Done.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        try:
            events = [e async for e in agent.chat("Say hello")]
            dashboards = [e for e in events if isinstance(e, DashboardHTML)]
            assert len(dashboards) == 0
        finally:
            await agent.end_session()


class TestAgentSessionLifecycle:
    async def test_session_created_and_destroyed(self):
        provider = MockProvider([
            LLMResponse(text="Hello!", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        assert agent.session_id is not None
        sid = agent.session_id

        events = [e async for e in agent.chat("Hi")]
        assert any(isinstance(e, TextDelta) for e in events)

        await agent.end_session()
        assert agent.session_id is None

    async def test_prewarm_imports_available(self):
        provider = MockProvider([
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(
                    id="t1", name="execute_python_code",
                    input={"code": "print(pandas.__version__)\nprint(numpy.__version__)"},
                )],
                stop_reason="tool_use", raw_content=[],
            ),
            LLMResponse(text="Libraries available.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
        await agent.start_session()
        try:
            events = [e async for e in agent.chat("Check imports")]
            tool_results = [e for e in events if isinstance(e, ToolResult)]
            assert len(tool_results) == 1
            assert tool_results[0].success is True
            assert "." in tool_results[0].output
        finally:
            await agent.end_session()
