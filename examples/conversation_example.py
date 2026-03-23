"""Multi-turn conversation: session persists, variables carry over."""

import asyncio

import anthropic

from sandbox_client import SandboxSession

TOOL_DEFINITION = {
    "name": "execute_python_code",
    "description": (
        "Execute Python code in an isolated sandbox. The sandbox has numpy, "
        "pandas, matplotlib, scipy, plotly, and seaborn pre-installed. "
        "State persists across calls within the same conversation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute",
            },
        },
        "required": ["code"],
    },
}


def format_result(result):
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
            parts.append(f"[output {i}]: {output.mime_type}\n{output.data}")
        else:
            parts.append(
                f"[output {i}]: {output.mime_type} ({len(output.data)} bytes)",
            )
    return "\n".join(parts) or "(no output)"


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

            while response.stop_reason == "tool_use":
                messages.append(
                    {"role": "assistant", "content": response.content},
                )
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        result = await session.execute(block.input["code"])
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": format_result(result),
                            },
                        )

                messages.append({"role": "user", "content": tool_results})
                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=4096,
                    tools=[TOOL_DEFINITION],
                    messages=messages,
                )

            messages.append(
                {"role": "assistant", "content": response.content},
            )
            for block in response.content:
                if hasattr(block, "text"):
                    print(block.text)
    finally:
        await session.stop()


asyncio.run(main())
