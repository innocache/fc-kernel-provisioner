"""Tests for SandboxSession (mocked HTTP/WebSocket)."""

import asyncio
import base64
import json

import aiohttp
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sandbox_client.session import SandboxSession


def _make_mock_session(kernel_id="test-kernel-id"):
    """Create a mock aiohttp.ClientSession with REST responses."""
    mock_http = AsyncMock()

    # POST /api/kernels → kernel_id
    post_resp = AsyncMock()
    post_resp.status = 200
    post_resp.raise_for_status = MagicMock()
    post_resp.json = AsyncMock(return_value={"id": kernel_id})
    mock_http.post = AsyncMock(return_value=post_resp)

    # DELETE /api/kernels/{id}
    delete_resp = AsyncMock()
    delete_resp.status = 204
    delete_resp.raise_for_status = MagicMock()
    mock_http.delete = AsyncMock(return_value=delete_resp)

    # ws_connect
    mock_ws = AsyncMock()
    mock_ws.close = AsyncMock()
    mock_ws.closed = False

    ws_ctx = AsyncMock()
    ws_ctx.__aenter__ = AsyncMock(return_value=mock_ws)
    ws_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_http.ws_connect = MagicMock(return_value=ws_ctx)

    # close
    mock_http.close = AsyncMock()

    return mock_http, mock_ws


def _queue_ws_messages(mock_ws, messages, idle_msg_id=None):
    """Configure mock_ws.receive to yield messages then status:idle."""
    raw_msgs = []
    for msg in messages:
        raw = MagicMock()
        raw.type = aiohttp.WSMsgType.TEXT
        raw.data = json.dumps(msg)
        raw_msgs.append(raw)

    # Add idle status message.
    if idle_msg_id:
        idle = MagicMock()
        idle.type = aiohttp.WSMsgType.TEXT
        idle.data = json.dumps({
            "header": {"msg_type": "status"},
            "parent_header": {"msg_id": idle_msg_id},
            "content": {"execution_state": "idle"},
        })
        raw_msgs.append(idle)

    mock_ws.receive = AsyncMock(side_effect=raw_msgs)


class TestSandboxSessionLifecycle:
    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_start_creates_kernel(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        mock_http.post.assert_called_once_with(
            "http://gw:8888/api/kernels",
            json={"name": "python3-firecracker"},
        )

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_start_opens_websocket(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        mock_http.ws_connect.assert_called_once_with(
            "ws://gw:8888/api/kernels/test-kernel-id/channels",
        )

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_stop_deletes_kernel(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()
        await session.stop()

        mock_http.delete.assert_called_once_with(
            "http://gw:8888/api/kernels/test-kernel-id",
        )
        mock_http.close.assert_called_once()

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_context_manager(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        async with SandboxSession("http://gw:8888") as session:
            assert session._kernel_id == "test-kernel-id"

        mock_http.delete.assert_called_once()

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_context_manager_suppresses_stop_errors(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        mock_http.delete = AsyncMock(side_effect=ConnectionError("gone"))
        MockClientSession.return_value = mock_http

        # Should not raise even though stop() fails
        async with SandboxSession("http://gw:8888") as session:
            pass

    async def test_execute_before_start_raises(self):
        session = SandboxSession("http://gw:8888")
        with pytest.raises(RuntimeError, match="Session not started"):
            await session.execute("print('hi')")

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_stop_is_idempotent(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()
        await session.stop()
        await session.stop()  # Should not raise

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_start_503_raises(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        post_resp = AsyncMock()
        post_resp.status = 503
        mock_http.post = AsyncMock(return_value=post_resp)
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        with pytest.raises(RuntimeError, match="No VMs available"):
            await session.start()


class TestSandboxSessionExecute:
    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_execute_returns_stdout(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="abc123")
            _queue_ws_messages(mock_ws, [
                {
                    "header": {"msg_type": "stream"},
                    "parent_header": {"msg_id": "abc123"},
                    "content": {"name": "stdout", "text": "hello\n"},
                },
            ], idle_msg_id="abc123")

            result = await session.execute("print('hello')")

        assert result.success is True
        assert result.stdout == "hello\n"

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_execute_sends_correct_message(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="msg123")
            _queue_ws_messages(mock_ws, [], idle_msg_id="msg123")
            await session.execute("x = 1")

        sent = mock_ws.send_json.call_args[0][0]
        assert sent["header"]["msg_type"] == "execute_request"
        assert sent["content"]["code"] == "x = 1"
        assert sent["channel"] == "shell"

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_execute_timeout_interrupts_kernel(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        # Make ws.receive hang forever to trigger timeout.
        async def hang_forever():
            await asyncio.sleep(999)

        mock_ws.receive = AsyncMock(side_effect=hang_forever)

        # POST for interrupt
        interrupt_resp = AsyncMock()
        interrupt_resp.status = 204
        original_post = mock_http.post
        call_count = 0

        async def smart_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "interrupt" in url:
                return interrupt_resp
            return await original_post(url, **kwargs)

        mock_http.post = AsyncMock(side_effect=smart_post)

        session = SandboxSession("http://gw:8888", default_timeout=30)
        await session.start()

        result = await session.execute("import time; time.sleep(999)", timeout=0.1)

        assert result.success is False
        assert result.error is not None
        assert result.error.name == "TimeoutError"

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_execute_websocket_close_raises(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        close_msg = MagicMock()
        close_msg.type = aiohttp.WSMsgType.CLOSE
        mock_ws.receive = AsyncMock(return_value=close_msg)

        session = SandboxSession("http://gw:8888")
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="x")
            with pytest.raises(ConnectionError):
                await session.execute("print('hi')")


class TestSandboxSessionArtifacts:
    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_execute_with_artifact_store(self, MockClientSession):
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        mock_store = AsyncMock()
        mock_store.save = AsyncMock(return_value="http://cdn/session-1/output_0.png")

        session = SandboxSession("http://gw:8888", artifact_store=mock_store)
        await session.start()

        png_b64 = base64.b64encode(b"fake-png").decode()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="aid1")
            _queue_ws_messages(mock_ws, [
                {
                    "header": {"msg_type": "display_data"},
                    "parent_header": {"msg_id": "aid1"},
                    "content": {"data": {"image/png": png_b64}},
                },
            ], idle_msg_id="aid1")

            result = await session.execute("plt.show()")

        assert len(result.outputs) == 1
        assert result.outputs[0].url == "http://cdn/session-1/output_0.png"
        assert result.outputs[0].data == b"fake-png"
        mock_store.save.assert_called_once()

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_artifact_store_str_data_encoded_to_bytes(self, MockClientSession):
        """Text display outputs are encoded to UTF-8 bytes before save()."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        mock_store = AsyncMock()
        mock_store.save = AsyncMock(return_value="http://cdn/s/output_0.html")

        session = SandboxSession("http://gw:8888", artifact_store=mock_store)
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="sid1")
            _queue_ws_messages(mock_ws, [
                {
                    "header": {"msg_type": "display_data"},
                    "parent_header": {"msg_id": "sid1"},
                    "content": {"data": {"text/html": "<b>hello</b>"}},
                },
            ], idle_msg_id="sid1")

            result = await session.execute("display(HTML('<b>hello</b>'))")

        # save() should receive bytes, not str
        call_args = mock_store.save.call_args
        assert call_args[0][2] == b"<b>hello</b>"  # data arg is bytes
        assert call_args[0][3] == "text/html"  # content_type

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_artifact_store_unknown_mime_uses_bin_extension(self, MockClientSession):
        """Unknown mime types get .bin extension for artifact filename."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        mock_store = AsyncMock()
        mock_store.save = AsyncMock(return_value="http://cdn/s/output_0.bin")

        session = SandboxSession("http://gw:8888", artifact_store=mock_store)
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="unk1")
            _queue_ws_messages(mock_ws, [
                {
                    "header": {"msg_type": "display_data"},
                    "parent_header": {"msg_id": "unk1"},
                    "content": {"data": {"text/plain": "some data"}},
                },
            ], idle_msg_id="unk1")

            result = await session.execute("print('test')")

        call_args = mock_store.save.call_args
        assert call_args[0][1] == "output_0.txt"  # text/plain → .txt


class TestSandboxSessionEdgeCases:
    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_https_to_wss_conversion(self, MockClientSession):
        """HTTPS gateway URL is converted to WSS for WebSocket."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("https://gw:8888")
        await session.start()

        mock_http.ws_connect.assert_called_once_with(
            "wss://gw:8888/api/kernels/test-kernel-id/channels",
        )

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_custom_kernel_name(self, MockClientSession):
        """Custom kernel_name is passed to POST /api/kernels."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888", kernel_name="my-kernel")
        await session.start()

        mock_http.post.assert_called_once_with(
            "http://gw:8888/api/kernels",
            json={"name": "my-kernel"},
        )

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_trailing_slash_stripped_from_url(self, MockClientSession):
        """Trailing slash in gateway_url is stripped."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888/")
        await session.start()

        mock_http.post.assert_called_once_with(
            "http://gw:8888/api/kernels",
            json={"name": "python3-firecracker"},
        )

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_messages_with_wrong_msg_id_filtered(self, MockClientSession):
        """Messages from other executions are filtered by msg_id."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="my-msg")
            _queue_ws_messages(mock_ws, [
                # This message has a different parent msg_id — should be ignored
                {
                    "header": {"msg_type": "stream"},
                    "parent_header": {"msg_id": "other-msg"},
                    "content": {"name": "stdout", "text": "from other\n"},
                },
                # This one matches
                {
                    "header": {"msg_type": "stream"},
                    "parent_header": {"msg_id": "my-msg"},
                    "content": {"name": "stdout", "text": "from mine\n"},
                },
            ], idle_msg_id="my-msg")

            result = await session.execute("print('hi')")

        assert result.stdout == "from mine\n"

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_ws_closing_type_raises_connection_error(self, MockClientSession):
        """CLOSING WebSocket type also raises ConnectionError."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        closing_msg = MagicMock()
        closing_msg.type = aiohttp.WSMsgType.CLOSING
        mock_ws.receive = AsyncMock(return_value=closing_msg)

        session = SandboxSession("http://gw:8888")
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="x")
            with pytest.raises(ConnectionError):
                await session.execute("x")

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_ws_error_type_raises_connection_error(self, MockClientSession):
        """ERROR WebSocket type raises ConnectionError."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        err_msg = MagicMock()
        err_msg.type = aiohttp.WSMsgType.ERROR
        mock_ws.receive = AsyncMock(return_value=err_msg)

        session = SandboxSession("http://gw:8888")
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="x")
            with pytest.raises(ConnectionError):
                await session.execute("x")

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_stop_handles_ws_exit_exception(self, MockClientSession):
        """stop() tolerates exceptions from WebSocket context exit."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        # Make ws_ctx.__aexit__ raise
        ws_ctx = mock_http.ws_connect.return_value
        ws_ctx.__aexit__ = AsyncMock(side_effect=RuntimeError("ws exit boom"))

        session = SandboxSession("http://gw:8888")
        await session.start()
        await session.stop()  # Should not raise

        # Kernel should still be deleted despite ws exit error
        mock_http.delete.assert_called_once()

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_stop_handles_delete_exception(self, MockClientSession):
        """stop() tolerates exceptions from kernel DELETE."""
        mock_http, mock_ws = _make_mock_session()
        mock_http.delete = AsyncMock(side_effect=ConnectionError("delete boom"))
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()
        await session.stop()  # Should not raise

        # HTTP session should still be closed
        mock_http.close.assert_called_once()

    async def test_execute_after_stop_raises(self):
        """execute() after stop() raises RuntimeError."""
        session = SandboxSession("http://gw:8888")
        # Never started, so _started=False
        with pytest.raises(RuntimeError, match="Session not started"):
            await session.execute("x")

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_default_timeout_used_when_none(self, MockClientSession):
        """default_timeout is used when execute() timeout is None."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        # Make receive hang to trigger timeout
        async def hang_forever():
            await asyncio.sleep(999)
        mock_ws.receive = AsyncMock(side_effect=hang_forever)

        interrupt_resp = AsyncMock()
        interrupt_resp.status = 204
        original_post = mock_http.post
        async def smart_post(url, **kwargs):
            if "interrupt" in url:
                return interrupt_resp
            return await original_post(url, **kwargs)
        mock_http.post = AsyncMock(side_effect=smart_post)

        session = SandboxSession("http://gw:8888", default_timeout=0.1)
        await session.start()

        result = await session.execute("import time; time.sleep(999)")

        assert result.success is False
        assert result.error.name == "TimeoutError"
        assert "0.1s" in result.error.value

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_context_manager_propagates_body_exception(self, MockClientSession):
        """Exceptions from the async with body propagate (not suppressed)."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        with pytest.raises(ValueError, match="body error"):
            async with SandboxSession("http://gw:8888") as session:
                raise ValueError("body error")

        # stop() should still have been called
        mock_http.delete.assert_called_once()

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_binary_ws_message_parsed(self, MockClientSession):
        """BINARY WebSocket messages are also parsed."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="bin1")

            # Create raw messages: one BINARY, then idle
            binary_msg = MagicMock()
            binary_msg.type = aiohttp.WSMsgType.BINARY
            binary_msg.data = json.dumps({
                "header": {"msg_type": "stream"},
                "parent_header": {"msg_id": "bin1"},
                "content": {"name": "stdout", "text": "binary\n"},
            })

            idle_msg = MagicMock()
            idle_msg.type = aiohttp.WSMsgType.TEXT
            idle_msg.data = json.dumps({
                "header": {"msg_type": "status"},
                "parent_header": {"msg_id": "bin1"},
                "content": {"execution_state": "idle"},
            })

            mock_ws.receive = AsyncMock(side_effect=[binary_msg, idle_msg])

            result = await session.execute("print('binary')")

        assert result.stdout == "binary\n"

    @patch("sandbox_client.session.aiohttp.ClientSession")
    async def test_non_text_non_binary_ws_message_skipped(self, MockClientSession):
        """Non-TEXT/BINARY WebSocket messages (e.g. PING) are skipped."""
        mock_http, mock_ws = _make_mock_session()
        MockClientSession.return_value = mock_http

        session = SandboxSession("http://gw:8888")
        await session.start()

        with patch("sandbox_client.session.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="ping1")

            ping_msg = MagicMock()
            ping_msg.type = aiohttp.WSMsgType.PING

            stdout_msg = MagicMock()
            stdout_msg.type = aiohttp.WSMsgType.TEXT
            stdout_msg.data = json.dumps({
                "header": {"msg_type": "stream"},
                "parent_header": {"msg_id": "ping1"},
                "content": {"name": "stdout", "text": "after ping\n"},
            })

            idle_msg = MagicMock()
            idle_msg.type = aiohttp.WSMsgType.TEXT
            idle_msg.data = json.dumps({
                "header": {"msg_type": "status"},
                "parent_header": {"msg_id": "ping1"},
                "content": {"execution_state": "idle"},
            })

            mock_ws.receive = AsyncMock(side_effect=[ping_msg, stdout_msg, idle_msg])

            result = await session.execute("print('test')")

        assert result.stdout == "after ping\n"
