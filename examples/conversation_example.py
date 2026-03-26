"""Multi-turn conversation: session persists, variables carry over."""

import asyncio

import anthropic
import httpx

API_URL = "http://localhost:8000"

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
    messages = []

    async with httpx.AsyncClient(base_url=API_URL, timeout=120) as http:
        sid = (await http.post("/sessions")).json()["session_id"]

        try:
            while True:
                user_input = input("> ")
                if user_input.lower() in ("exit", "quit"):
                    break

                messages.append({"role": "user", "content": user_input})

                response = llm.messages.create(
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
                            resp = await http.post(
                                f"/sessions/{sid}/execute",
                                json={"code": block.input["code"]},
                            )
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": format_result(resp.json()),
                                },
                            )

                    messages.append({"role": "user", "content": tool_results})
                    response = llm.messages.create(
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
            await http.delete(f"/sessions/{sid}")


asyncio.run(main())
