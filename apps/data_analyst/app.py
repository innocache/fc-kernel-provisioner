import chainlit as cl

from .agent import (
    DataAnalystAgent, DashboardLink, FileDownload,
    ImageOutput, TextDelta, ToolResult, ToolStart,
)
from .config import EXECUTION_API_URL, LLM_MODEL, LLM_PROVIDER
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
