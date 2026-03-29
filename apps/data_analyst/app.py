import chainlit as cl

from .agent import (
    DataAnalystAgent, DashboardHTML, DashboardLink, FileDownload,
    ImageOutput, TextDelta, ToolResult, ToolStart,
)
from .config import EXECUTION_API_URL, LLM_MODEL, LLM_PROVIDER
from .llm_provider import create_provider


@cl.on_chat_start
async def on_start():
    provider = create_provider(LLM_PROVIDER, LLM_MODEL)
    agent = DataAnalystAgent(api_url=EXECUTION_API_URL, provider=provider)
    cl.user_session.set("agent", agent)
    await cl.Message(content="Ready! Upload a data file or ask a question.").send()


@cl.on_message
async def on_message(message: cl.Message):
    agent: DataAnalystAgent = cl.user_session.get("agent")

    for el in message.elements or []:
        from pathlib import Path
        content = Path(el.path).read_bytes() if hasattr(el, "path") and el.path else getattr(el, "content", b"")
        if not content:
            continue
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
            await cl.Message(
                content="",
                elements=[cl.Image(content=event.data, name="plot.png", display="inline")],
            ).send()

        elif isinstance(event, DashboardHTML):
            import tempfile
            html_escaped = event.html.decode("utf-8", errors="replace").replace("&", "&amp;").replace('"', "&quot;")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{event.filename}")
            tmp.write(event.html)
            tmp.close()
            await cl.Message(
                content=(
                    f'<iframe srcdoc="{html_escaped}" '
                    f'width="100%" height="700" frameborder="0" '
                    f'sandbox="allow-scripts allow-same-origin allow-downloads" '
                    f'style="border-radius: 8px; border: 1px solid #e0e0e0;"></iframe>'
                ),
                elements=[cl.File(name=event.filename, path=tmp.name, display="side")],
            ).send()

        elif isinstance(event, DashboardLink):
            await cl.Message(
                content=(
                    f'<p><a href="{event.full_url}" target="_blank">Open dashboard in new tab</a></p>\n'
                    f'<iframe src="{event.full_url}" '
                    f'width="100%" height="600" frameborder="0" '
                    f'style="border-radius: 8px; border: 1px solid #e0e0e0;"></iframe>'
                ),
            ).send()

        elif isinstance(event, FileDownload):
            import tempfile
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{event.filename}")
            tmp.write(event.data)
            tmp.close()
            await cl.Message(
                content=f"**{event.filename}** ready for download",
                elements=[cl.File(name=event.filename, path=tmp.name, display="inline")],
            ).send()

    await response_msg.update()


@cl.on_chat_end
async def on_end():
    agent: DataAnalystAgent = cl.user_session.get("agent")
    if agent:
        await agent.end_session()
