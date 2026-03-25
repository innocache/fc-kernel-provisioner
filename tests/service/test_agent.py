import pytest

from apps.data_analyst.agent import (
    DataAnalystAgent, FileDownload, ImageOutput, TextDelta, ToolResult, ToolStart,
)
from apps.data_analyst.llm_provider import LLMResponse, ToolCall

TEST_CSV = b"product,region,revenue\nWidget A,North,12500\nWidget B,South,8300\n"


class MockProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0

    async def chat(self, messages, system, tools):
        r = self._responses[self._idx]
        self._idx += 1
        return r

    def format_tool_result(self, tool_call_id, content):
        return {"type": "tool_result", "tool_use_id": tool_call_id, "content": content}


class TestAgentWithFakeKG:
    async def test_upload_and_execute(self, execution_api):
        provider = MockProvider([
            LLMResponse(text=None, tool_calls=[
                ToolCall(id="t1", name="execute_python_code",
                         input={"code": "print(f'2 + 2 = {2 + 2}')"})
            ], stop_reason="tool_use", raw_content=[]),
            LLMResponse(text="The answer is 4.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=execution_api, provider=provider)
        await agent.start_session()
        try:
            events = [e async for e in agent.chat("What is 2+2?")]
            tool_results = [e for e in events if isinstance(e, ToolResult)]
            assert any("2 + 2 = 4" in r.output for r in tool_results)
        finally:
            await agent.end_session()

    async def test_multi_turn_state(self, execution_api):
        provider = MockProvider([
            LLMResponse(text=None, tool_calls=[
                ToolCall(id="t1", name="execute_python_code", input={"code": "x = 42\nprint('set')"})
            ], stop_reason="tool_use", raw_content=[]),
            LLMResponse(text="Done.", tool_calls=[], stop_reason="end", raw_content=[]),
            LLMResponse(text=None, tool_calls=[
                ToolCall(id="t2", name="execute_python_code", input={"code": "print(f'x={x}')"})
            ], stop_reason="tool_use", raw_content=[]),
            LLMResponse(text="x is 42.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=execution_api, provider=provider)
        await agent.start_session()
        try:
            await collect(agent, "Set x")
            events = await collect(agent, "What is x?")
            assert any("x=42" in e.output for e in events if isinstance(e, ToolResult))
        finally:
            await agent.end_session()

    async def test_error_reported(self, execution_api):
        provider = MockProvider([
            LLMResponse(text=None, tool_calls=[
                ToolCall(id="t1", name="execute_python_code", input={"code": "1/0"})
            ], stop_reason="tool_use", raw_content=[]),
            LLMResponse(text="Error occurred.", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        agent = DataAnalystAgent(api_url=execution_api, provider=provider)
        await agent.start_session()
        try:
            events = await collect(agent, "divide")
            errors = [e for e in events if isinstance(e, ToolResult) and not e.success]
            assert any("ZeroDivisionError" in e.output for e in errors)
        finally:
            await agent.end_session()

async def collect(agent, message):
    return [e async for e in agent.chat(message)]
