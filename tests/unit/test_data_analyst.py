import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.data_analyst.agent import (
    DataAnalystAgent, DashboardLink, FileDownload,
    ImageOutput, TextDelta, ToolResult, ToolStart,
)
from apps.data_analyst.config import sanitize_filename
from apps.data_analyst.llm_provider import (
    AnthropicProvider, LLMResponse, OllamaProvider,
    OpenAIProvider, ToolCall, create_provider,
)


class TestSanitizeFilename:
    def test_normal_name(self):
        assert sanitize_filename("data.csv") == "data.csv"

    def test_spaces_replaced(self):
        assert sanitize_filename("my file.csv") == "my_file.csv"

    def test_path_separators_replaced(self):
        result = sanitize_filename("../../etc/passwd")
        assert "/" not in result
        assert ".." not in result or result.startswith("..")

    def test_shell_chars_replaced(self):
        assert sanitize_filename("file;rm -rf/.csv") == "file_rm_-rf_.csv"


class TestCreateProvider:
    @patch("apps.data_analyst.llm_provider.anthropic", create=True)
    def test_anthropic(self, _):
        p = create_provider("anthropic", "claude-sonnet-4-20250514")
        assert isinstance(p, AnthropicProvider)

    def test_openai(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            p = create_provider("openai", "gpt-4o")
        assert isinstance(p, OpenAIProvider)

    @patch("apps.data_analyst.llm_provider.openai", create=True)
    def test_ollama(self, _):
        p = create_provider("ollama", "llama3.1")
        assert isinstance(p, OllamaProvider)

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            create_provider("gemini", "model")


class TestAnthropicProvider:
    @patch("apps.data_analyst.llm_provider.anthropic", create=True)
    async def test_text_response(self, _):
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider.model = "test"
        provider.client = AsyncMock()
        provider.client.messages.create = AsyncMock(return_value=MagicMock(
            content=[MagicMock(type="text", text="Hello")],
            stop_reason="end_turn",
        ))
        resp = await provider.chat([{"role": "user", "content": "hi"}], "sys", [])
        assert resp.text == "Hello"
        assert resp.stop_reason == "end"
        assert resp.tool_calls == []

    @patch("apps.data_analyst.llm_provider.anthropic", create=True)
    async def test_tool_use_response(self, _):
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider.model = "test"
        provider.client = AsyncMock()
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "t1"
        tool_block.name = "execute_python_code"
        tool_block.input = {"code": "1+1"}
        provider.client.messages.create = AsyncMock(return_value=MagicMock(
            content=[tool_block],
            stop_reason="tool_use",
        ))
        resp = await provider.chat([{"role": "user", "content": "run"}], "sys", [])
        assert resp.stop_reason == "tool_use"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "execute_python_code"

    @patch("apps.data_analyst.llm_provider.anthropic", create=True)
    def test_format_tool_result(self, _):
        provider = AnthropicProvider.__new__(AnthropicProvider)
        result = provider.format_tool_result("t1", "output text")
        assert result == {"type": "tool_result", "tool_use_id": "t1", "content": "output text"}


class TestOpenAIProvider:
    @patch("apps.data_analyst.llm_provider.openai", create=True)
    def test_convert_tools(self, _):
        provider = OpenAIProvider.__new__(OpenAIProvider)
        tools = [{"name": "test", "description": "desc", "input_schema": {"type": "object"}}]
        oai = provider._convert_tools(tools)
        assert oai[0]["type"] == "function"
        assert oai[0]["function"]["parameters"] == {"type": "object"}
        assert oai[0]["function"]["name"] == "test"


def _mock_provider(text=None, tool_calls=None, stop="end"):
    provider = AsyncMock()
    provider.chat = AsyncMock(return_value=LLMResponse(
        text=text, tool_calls=tool_calls or [], stop_reason=stop, raw_content=[],
    ))
    provider.format_tool_result = MagicMock(
        side_effect=lambda tid, content: {"type": "tool_result", "tool_use_id": tid, "content": content}
    )
    return provider


def _mock_http_client(execute_result=None, session_id="test-session"):
    client = AsyncMock()
    client.post = AsyncMock(side_effect=lambda url, **kw: _mock_response(url, execute_result, session_id))
    client.delete = AsyncMock(return_value=MagicMock(status_code=200))
    client.aclose = AsyncMock()
    return client


def _mock_response(url, execute_result, session_id):
    resp = MagicMock()
    if "/files" in url:
        resp.json.return_value = {"path": "/data/test.csv", "filename": "test.csv", "size": 5}
        resp.raise_for_status = MagicMock()
    elif "/sessions" in url and "/execute" not in url and "/dashboard" not in url:
        resp.json.return_value = {"session_id": session_id}
        resp.raise_for_status = MagicMock()
    elif "/execute" in url:
        resp.json.return_value = execute_result or {
            "success": True, "stdout": "hello\n", "stderr": "", "error": None,
            "outputs": [], "execution_count": 1,
        }
    elif "/dashboard" in url:
        resp.status_code = 200
        resp.json.return_value = {"url": "/dash/s1/dash_abc", "session_id": "s1", "app_id": "abc"}
    return resp


class TestAgentSession:
    async def test_start_creates_session(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"session_id": "sid-1"}
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("apps.data_analyst.agent.httpx.AsyncClient", return_value=mock_client):
            await agent.start_session()
        assert agent.session_id == "sid-1"

    async def test_end_deletes_session(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()
        mock_client.delete = AsyncMock(return_value=MagicMock(status_code=200))
        agent._client = mock_client
        agent.session_id = "sid-1"
        await agent.end_session()
        mock_client.delete.assert_awaited_once_with("/sessions/sid-1")
        assert agent.session_id is None


class TestAgentUpload:
    async def test_small_upload(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        agent._client = _mock_http_client()
        agent.session_id = "s1"
        result = await agent.upload_file("test.csv", b"hello")
        assert result == "Saved /data/test.csv (5 bytes)"

    async def test_rejects_oversized(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        agent.session_id = "s1"
        with pytest.raises(ValueError, match="too large"):
            await agent.upload_file("big.csv", b"x" * (51 * 1024 * 1024))

    async def test_sanitizes_filename(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        agent._client = _mock_http_client()
        agent.session_id = "s1"
        await agent.upload_file("../../etc/passwd", b"data")
        call_args = agent._client.post.call_args
        files = call_args.kwargs.get("files", {}) if call_args.kwargs else call_args[1].get("files", {})
        uploaded_name = files["file"][0]
        assert "../" not in uploaded_name


class TestAgentDownload:
    async def test_download_success(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        encoded = base64.b64encode(b"col1,col2\n1,2").decode()
        call_count = 0

        async def mock_post(url, **kw):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            if call_count == 1:
                resp.json.return_value = {"success": True, "stdout": "28\n", "stderr": "", "error": None, "outputs": [], "execution_count": 1}
            else:
                resp.json.return_value = {"success": True, "stdout": f"{encoded}\n", "stderr": "", "error": None, "outputs": [], "execution_count": 2}
            return resp

        agent._client = AsyncMock()
        agent._client.post = mock_post
        agent.session_id = "s1"
        fd = await agent.download_file("/data/test.csv")
        assert fd.filename == "test.csv"
        assert fd.mime_type == "text/csv"
        assert b"col1" in fd.data

    async def test_download_rejects_outside_data(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        agent.session_id = "s1"
        with pytest.raises(ValueError, match="restricted to /data/"):
            await agent.download_file("/etc/passwd")

    async def test_download_rejects_oversized(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        agent._client = AsyncMock()
        agent._client.post = AsyncMock(return_value=MagicMock(
            json=MagicMock(return_value={"success": True, "stdout": str(20 * 1024 * 1024), "stderr": "", "error": None, "outputs": [], "execution_count": 1})
        ))
        agent.session_id = "s1"
        with pytest.raises(ValueError, match="too large"):
            await agent.download_file("/data/huge.bin")


class TestAgentChat:
    async def test_text_response(self):
        provider = _mock_provider(text="Analysis complete")
        agent = DataAnalystAgent(api_url="http://test", provider=provider)
        agent.session_id = "s1"
        agent._client = _mock_http_client()

        events = [e async for e in agent.chat("analyze")]
        assert any(isinstance(e, TextDelta) and e.text == "Analysis complete" for e in events)

    async def test_tool_use_loop(self):
        provider = AsyncMock()
        provider.chat = AsyncMock(side_effect=[
            LLMResponse(text=None, tool_calls=[ToolCall(id="t1", name="execute_python_code", input={"code": "print(1)"})], stop_reason="tool_use", raw_content=[]),
            LLMResponse(text="Done", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        provider.format_tool_result = MagicMock(return_value={"type": "tool_result", "tool_use_id": "t1", "content": "1\n"})

        agent = DataAnalystAgent(api_url="http://test", provider=provider)
        agent.session_id = "s1"
        agent._client = _mock_http_client()

        events = [e async for e in agent.chat("run code")]
        types = [type(e).__name__ for e in events]
        assert "ToolStart" in types
        assert "ToolResult" in types
        assert "TextDelta" in types

    async def test_image_extraction(self):
        provider = AsyncMock()
        img_b64 = base64.b64encode(b"\x89PNG").decode()
        provider.chat = AsyncMock(side_effect=[
            LLMResponse(text=None, tool_calls=[ToolCall(id="t1", name="execute_python_code", input={"code": "plot"})], stop_reason="tool_use", raw_content=[]),
            LLMResponse(text="Here's the chart", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        provider.format_tool_result = MagicMock(return_value={"type": "tool_result", "tool_use_id": "t1", "content": "ok"})

        agent = DataAnalystAgent(api_url="http://test", provider=provider)
        agent.session_id = "s1"
        agent._client = _mock_http_client({
            "success": True, "stdout": "", "stderr": "", "error": None,
            "outputs": [{"mime_type": "image/png", "data_b64": img_b64}],
            "execution_count": 1,
        })

        events = [e async for e in agent.chat("plot")]
        images = [e for e in events if isinstance(e, ImageOutput)]
        assert len(images) == 1
        assert images[0].data == b"\x89PNG"

    async def test_dashboard_link(self):
        provider = AsyncMock()
        provider.chat = AsyncMock(side_effect=[
            LLMResponse(text=None, tool_calls=[ToolCall(id="t1", name="launch_dashboard", input={"code": "pn.panel(1)"})], stop_reason="tool_use", raw_content=[]),
            LLMResponse(text="Dashboard ready", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        provider.format_tool_result = MagicMock(return_value={"type": "tool_result", "tool_use_id": "t1", "content": "ok"})

        agent = DataAnalystAgent(api_url="http://test", provider=provider)
        agent.session_id = "s1"
        agent._client = _mock_http_client()

        events = [e async for e in agent.chat("dashboard")]
        links = [e for e in events if isinstance(e, DashboardLink)]
        assert len(links) == 1
        assert "/dash/" in links[0].url


class TestContextCompaction:
    def test_no_compaction_under_limit(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": []},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 500}]},
        ]
        result = agent._compact_messages(messages)
        assert result[2]["content"][0]["content"] == "x" * 500

    def test_compaction_truncates_old(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
        for i in range(15):
            messages.append({"role": "assistant", "content": []})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * 500}
            ]})
        result = agent._compact_messages(messages)
        old_result = result[2]["content"][0]["content"]
        assert "[truncated]" in old_result
        assert len(old_result) < 500

        recent_result = result[-1]["content"][0]["content"]
        assert recent_result == "x" * 500


class TestOpenAIMessageConversion:
    @patch("apps.data_analyst.llm_provider.openai", create=True)
    def test_user_message_converted(self, _):
        provider = OpenAIProvider.__new__(OpenAIProvider)
        messages = [{"role": "user", "content": "hello"}]
        oai = provider._convert_messages(messages, "system prompt")
        assert oai[0] == {"role": "system", "content": "system prompt"}
        assert oai[1] == {"role": "user", "content": "hello"}

    @patch("apps.data_analyst.llm_provider.openai", create=True)
    def test_tool_result_converted(self, _):
        provider = OpenAIProvider.__new__(OpenAIProvider)
        messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "output text"},
            ]},
        ]
        oai = provider._convert_messages(messages, "sys")
        assert oai[1] == {"role": "tool", "tool_call_id": "t1", "content": "output text"}

    @patch("apps.data_analyst.llm_provider.openai", create=True)
    def test_assistant_tool_use_converted(self, _):
        provider = OpenAIProvider.__new__(OpenAIProvider)
        tool_block = {
            "type": "tool_use",
            "id": "t1",
            "name": "execute_python_code",
            "input": {"code": "print(1)"},
        }
        messages = [{"role": "assistant", "content": [tool_block]}]
        oai = provider._convert_messages(messages, "sys")
        assert oai[1]["role"] == "assistant"
        assert oai[1]["tool_calls"][0]["id"] == "t1"
        assert oai[1]["tool_calls"][0]["function"]["name"] == "execute_python_code"


class TestMultiToolResponse:
    async def test_two_tools_in_one_response(self):
        provider = AsyncMock()
        provider.chat = AsyncMock(side_effect=[
            LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(id="t1", name="execute_python_code", input={"code": "x = 1"}),
                    ToolCall(id="t2", name="execute_python_code", input={"code": "print(x)"}),
                ],
                stop_reason="tool_use",
                raw_content=[],
            ),
            LLMResponse(text="Done both", tool_calls=[], stop_reason="end", raw_content=[]),
        ])
        provider.format_tool_result = MagicMock(
            side_effect=lambda tid, c: {"type": "tool_result", "tool_use_id": tid, "content": c}
        )
        agent = DataAnalystAgent(api_url="http://test", provider=provider)
        agent.session_id = "s1"
        agent._client = _mock_http_client()
        events = [e async for e in agent.chat("do two things")]
        tool_starts = [e for e in events if isinstance(e, ToolStart)]
        tool_results = [e for e in events if isinstance(e, ToolResult)]
        assert len(tool_starts) == 2
        assert len(tool_results) == 2
        assert tool_starts[0].code == "x = 1"
        assert tool_starts[1].code == "print(x)"


class TestUploadDelegatesChunkingToServer:
    async def test_large_file_uses_single_multipart_post(self):
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        agent._client = _mock_http_client()
        agent.session_id = "s1"
        content = b"x" * (5 * 1024 * 1024)
        result = await agent.upload_file("big.csv", content)
        assert result == "Saved /data/test.csv (5 bytes)"
        post_calls = [c for c in agent._client.post.call_args_list if "/files" in str(c)]
        assert len(post_calls) == 1


class TestSessionRecovery:
    async def test_reconnect_on_connection_error(self):
        import httpx
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        call_count = 0

        async def mock_post(url, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("dead")
            resp = MagicMock()
            resp.json.return_value = {"session_id": "new-session"}
            resp.raise_for_status = MagicMock()
            return resp

        agent._client = AsyncMock()
        agent._client.post = mock_post
        agent._client.aclose = AsyncMock()
        agent.session_id = "old-session"

        with patch("apps.data_analyst.agent.httpx.AsyncClient", return_value=agent._client):
            result = await agent._execute_with_recovery("print(1)")

        assert result["success"] is False
        assert result["error"]["name"] == "SessionRestarted"

    async def test_old_client_closed_on_recovery(self):
        import httpx
        agent = DataAnalystAgent(api_url="http://test", provider=_mock_provider())
        old_client = AsyncMock()
        old_client.post = AsyncMock(side_effect=httpx.ConnectError("dead"))
        old_client.aclose = AsyncMock()
        agent._client = old_client
        agent.session_id = "old"

        new_client = AsyncMock()
        new_resp = MagicMock()
        new_resp.json.return_value = {"session_id": "new"}
        new_resp.raise_for_status = MagicMock()
        new_client.post = AsyncMock(return_value=new_resp)

        with patch("apps.data_analyst.agent.httpx.AsyncClient", return_value=new_client):
            await agent._execute_with_recovery("print(1)")

        old_client.aclose.assert_awaited_once()


class TestProviderErrors:
    async def test_format_result_with_stderr(self):
        r = DataAnalystAgent._format_result({
            "success": True, "stdout": "ok\n", "stderr": "warn\n",
            "error": None, "outputs": [],
        })
        assert "ok\n" in r
        assert "[stderr]: warn\n" in r

    async def test_format_result_with_image_output(self):
        r = DataAnalystAgent._format_result({
            "success": True, "stdout": "", "stderr": "", "error": None,
            "outputs": [{"mime_type": "image/png", "data_b64": "abc"}],
        })
        assert "(image)" in r

    async def test_format_result_with_html_output(self):
        r = DataAnalystAgent._format_result({
            "success": True, "stdout": "", "stderr": "", "error": None,
            "outputs": [{"mime_type": "text/html", "data": "<table>data</table>"}],
        })
        assert "<table>" in r

    async def test_extract_images_filters_non_image(self):
        images = DataAnalystAgent._extract_images({
            "outputs": [
                {"mime_type": "text/html", "data": "<p>text</p>"},
                {"mime_type": "image/png", "data_b64": base64.b64encode(b"\x89PNG").decode()},
            ]
        })
        assert len(images) == 1
        assert images[0].mime_type == "image/png"


class TestFormatResult:
    def test_success(self):
        r = DataAnalystAgent._format_result({"success": True, "stdout": "42\n", "stderr": "", "error": None, "outputs": []})
        assert r == "42\n"

    def test_error(self):
        r = DataAnalystAgent._format_result({
            "success": False, "stdout": "", "stderr": "",
            "error": {"name": "ValueError", "value": "bad", "traceback": []},
            "outputs": [],
        })
        assert "ValueError" in r

    def test_empty(self):
        r = DataAnalystAgent._format_result({"success": True, "stdout": "", "stderr": "", "error": None, "outputs": []})
        assert r == "(no output)"
