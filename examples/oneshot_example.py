"""One-shot example: user asks a question, Claude writes code, sandbox runs it."""

import asyncio

import anthropic
import httpx

API_URL = "http://localhost:8000"

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


def format_result(data: dict) -> str:
    parts = []
    if data.get("stdout"):
        parts.append(data["stdout"])
    if data.get("stderr"):
        parts.append(f"[stderr]: {data['stderr']}")
    if data.get("error"):
        err = data["error"]
        parts.append(f"[error]: {err['name']}: {err['value']}")
    for i, out in enumerate(data.get("outputs", [])):
        parts.append(f"[output {i}]: {out.get('mime_type', '?')}")
    return "\n".join(parts) or "(no output)"


async def main():
    llm = anthropic.Anthropic()

    response = llm.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        tools=[TOOL_DEFINITION],
        messages=[
            {"role": "user", "content": "What's the 100th Fibonacci number?"},
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "execute_python_code":
            async with httpx.AsyncClient(base_url=API_URL, timeout=120) as http:
                resp = await http.post("/execute", json={"code": block.input["code"]})
                result = resp.json()

            final = llm.messages.create(
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
                                "content": format_result(result),
                            },
                        ],
                    },
                ],
            )
            print(final.content[0].text)


asyncio.run(main())
