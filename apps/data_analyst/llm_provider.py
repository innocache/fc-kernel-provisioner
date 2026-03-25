import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: str
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
            tool_calls=tool_calls, stop_reason=stop,
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
                text = ""
                tool_calls_out = []
                for block in m["content"]:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            text += block.text
                        elif block.type == "tool_use":
                            tool_calls_out.append({
                                "id": block.id, "type": "function",
                                "function": {"name": block.name, "arguments": json.dumps(block.input)},
                            })
                msg = {"role": "assistant", "content": text or None}
                if tool_calls_out:
                    msg["tool_calls"] = tool_calls_out
                oai.append(msg)
            elif m["role"] == "user" and isinstance(m.get("content"), list):
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
        oai_tools = self._convert_tools(tools) if tools else None
        oai_messages = self._convert_messages(messages, system)
        response = await self.client.chat.completions.create(
            model=self.model, tools=oai_tools, messages=oai_messages,
        )
        choice = response.choices[0]
        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id, name=tc.function.name,
                    input=json.loads(tc.function.arguments),
                ))
        stop = "tool_use" if choice.finish_reason in ("tool_calls", "function_call") else "end"
        return LLMResponse(
            text=choice.message.content, tool_calls=tool_calls,
            stop_reason=stop, raw_content=[choice.message],
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
