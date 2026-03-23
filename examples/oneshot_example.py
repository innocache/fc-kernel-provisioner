"""One-shot example: user asks a question, Claude writes code, sandbox runs it."""

import asyncio

import anthropic

from sandbox_client import SandboxSession

TOOL_DEFINITION = {
    "name": "execute_python_code",
    "description": (
        "Execute Python code in an isolated sandbox. The sandbox has numpy, "
        "pandas, matplotlib, scipy, plotly, and seaborn pre-installed."
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

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        tools=[TOOL_DEFINITION],
        messages=[
            {"role": "user", "content": "What's the 100th Fibonacci number?"},
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "execute_python_code":
            async with SandboxSession("http://localhost:8888") as session:
                result = await session.execute(block.input["code"])

            tool_result = format_result(result)

            final = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                tools=[TOOL_DEFINITION],
                messages=[
                    {
                        "role": "user",
                        "content": "What's the 100th Fibonacci number?",
                    },
                    {"role": "assistant", "content": response.content},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": tool_result,
                            },
                        ],
                    },
                ],
            )
            print(final.content[0].text)


asyncio.run(main())
