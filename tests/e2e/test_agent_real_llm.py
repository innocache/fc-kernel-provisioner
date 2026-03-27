"""Real LLM tests for the data analyst agent.

These use actual LLM API calls + real sandbox execution.
Requires ANTHROPIC_API_KEY env var and all services running.

Run:  ANTHROPIC_API_KEY=sk-ant-... uv run pytest tests/test_data_analyst_llm.py -v -m slow
Skip: uv run pytest tests/ -m "not slow"
"""

import os

import pytest

from apps.data_analyst.agent import (
    DataAnalystAgent, FileDownload, ImageOutput, TextDelta, ToolResult, ToolStart,
)
from apps.data_analyst.llm_provider import AnthropicProvider

pytestmark = pytest.mark.slow

EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

TEST_CSV = (
    b"product,region,revenue,units\n"
    b"Widget A,North,12500,250\n"
    b"Widget B,South,8300,166\n"
    b"Widget A,East,15200,304\n"
    b"Widget C,North,6100,122\n"
    b"Widget B,West,9800,196\n"
)


@pytest.fixture
async def agent():
    if not API_KEY:
        pytest.skip("ANTHROPIC_API_KEY not set")
    provider = AnthropicProvider(model="claude-sonnet-4-20250514")
    a = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
    await a.start_session()
    yield a
    await a.end_session()


async def collect_events(agent, message):
    events = []
    async for event in agent.chat(message):
        events.append(event)
    return events


class TestLLMAnalysis:
    async def test_llm_summarizes_uploaded_data(self, agent):
        await agent.upload_file("sales.csv", TEST_CSV)
        events = await collect_events(agent, "Summarize this dataset. Print the shape and column names.")
        tool_results = [e for e in events if isinstance(e, ToolResult)]
        assert any(r.success for r in tool_results), "No successful tool execution"
        all_output = " ".join(r.output for r in tool_results)
        assert "5" in all_output or "rows" in all_output.lower()

    async def test_llm_answers_analytical_question(self, agent):
        await agent.upload_file("sales.csv", TEST_CSV)
        events = await collect_events(agent, "Which product has the highest total revenue? Print the answer.")
        text = " ".join(e.text for e in events if isinstance(e, TextDelta) and e.text)
        tool_output = " ".join(e.output for e in events if isinstance(e, ToolResult))
        combined = text + " " + tool_output
        assert any(p in combined for p in ["Widget A", "Widget B", "Widget C"]), \
            f"Expected a product name in response. Got: {combined[:300]}"

    async def test_llm_generates_working_chart(self, agent):
        await agent.upload_file("sales.csv", TEST_CSV)
        events = await collect_events(agent, "Create a bar chart of total revenue by product. Use matplotlib. Show the plot inline.")
        tool_results = [e for e in events if isinstance(e, ToolResult)]
        assert any(r.success for r in tool_results), "Code execution failed"
        has_image = any(isinstance(e, ImageOutput) for e in events)
        chart_keywords = ("plot", "saved", "chart", "bar", "figure", "png", "image")
        all_output = " ".join(r.output.lower() for r in tool_results if r.success)
        all_text = " ".join(e.text.lower() for e in events if isinstance(e, TextDelta) and e.text)
        has_chart_evidence = any(kw in all_output or kw in all_text for kw in chart_keywords)
        assert has_image or has_chart_evidence, \
            f"No chart evidence in output. Tool output: {all_output[:500]}"

    async def test_llm_exports_file(self, agent):
        await agent.upload_file("sales.csv", TEST_CSV)
        await collect_events(agent, "Load the data into a DataFrame called df.")
        events = await collect_events(agent, "Save a summary table to /data/summary.csv and give me the file to download.")
        downloads = [e for e in events if isinstance(e, FileDownload)]
        if downloads:
            assert downloads[0].filename.endswith(".csv")
            assert len(downloads[0].data) > 0
        else:
            tool_results = [e for e in events if isinstance(e, ToolResult)]
            assert any("summary" in r.output.lower() for r in tool_results), \
                "LLM didn't create summary file"

    async def test_llm_state_persists_across_turns(self, agent):
        await agent.upload_file("sales.csv", TEST_CSV)
        await collect_events(agent, "Load sales.csv into a variable called df. Print df.shape.")
        events = await collect_events(agent, "Print the mean of the revenue column from df.")
        tool_results = [e for e in events if isinstance(e, ToolResult)]
        assert any(r.success for r in tool_results), "Second turn failed — state may not persist"
        all_output = " ".join(r.output for r in tool_results)
        assert any(c.isdigit() for c in all_output), "Expected a number in revenue mean output"
