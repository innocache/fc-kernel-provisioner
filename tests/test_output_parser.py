"""Tests for the sandbox client output parser."""

import base64
import json

from sandbox_client.output import (
    DisplayOutput,
    ExecutionError,
    ExecutionResult,
    OutputParser,
)


class TestExecutionResultDataclass:
    def test_success_result(self):
        r = ExecutionResult(
            success=True, stdout="hello\n", stderr="", error=None,
            outputs=[], execution_count=1,
        )
        assert r.success is True
        assert r.stdout == "hello\n"
        assert r.execution_count == 1

    def test_error_result(self):
        err = ExecutionError(name="ValueError", value="bad", traceback=["line 1"])
        r = ExecutionResult(
            success=False, stdout="", stderr="", error=err,
            outputs=[], execution_count=1,
        )
        assert r.success is False
        assert r.error.name == "ValueError"


class TestOutputParserStreams:
    def test_stdout(self):
        messages = [
            {"header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "hello\n"}},
        ]
        result = OutputParser.parse(messages)
        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.success is True

    def test_stderr(self):
        messages = [
            {"header": {"msg_type": "stream"}, "content": {"name": "stderr", "text": "warn\n"}},
        ]
        result = OutputParser.parse(messages)
        assert result.stderr == "warn\n"
        assert result.stdout == ""

    def test_multiple_stdout_chunks(self):
        messages = [
            {"header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "a"}},
            {"header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "b"}},
        ]
        result = OutputParser.parse(messages)
        assert result.stdout == "ab"

    def test_empty_messages(self):
        result = OutputParser.parse([])
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.success is True
        assert result.outputs == []
        assert result.execution_count == 0


class TestOutputParserErrors:
    def test_error_message(self):
        messages = [
            {"header": {"msg_type": "error"}, "content": {
                "ename": "ZeroDivisionError",
                "evalue": "division by zero",
                "traceback": ["Traceback ...", "  File ...", "ZeroDivisionError: division by zero"],
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.success is False
        assert result.error is not None
        assert result.error.name == "ZeroDivisionError"
        assert result.error.value == "division by zero"
        assert len(result.error.traceback) == 3

    def test_stdout_before_error(self):
        """stdout captured before error should be preserved."""
        messages = [
            {"header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "before\n"}},
            {"header": {"msg_type": "error"}, "content": {
                "ename": "RuntimeError", "evalue": "oops", "traceback": [],
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.stdout == "before\n"
        assert result.success is False

    def test_execute_reply_extracts_count(self):
        messages = [
            {"header": {"msg_type": "execute_reply"}, "content": {
                "status": "ok", "execution_count": 5,
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.execution_count == 5
        assert result.success is True

    def test_execute_reply_fallback_error(self):
        """execute_reply with status=error and no prior error message."""
        messages = [
            {"header": {"msg_type": "execute_reply"}, "content": {
                "status": "error",
                "ename": "NameError",
                "evalue": "name 'x' is not defined",
                "traceback": ["NameError: name 'x' is not defined"],
                "execution_count": 3,
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.success is False
        assert result.error.name == "NameError"
        assert result.execution_count == 3

    def test_execute_reply_does_not_override_error_message(self):
        """If both error and execute_reply with error, the error message wins."""
        messages = [
            {"header": {"msg_type": "error"}, "content": {
                "ename": "TypeError", "evalue": "from error msg", "traceback": [],
            }},
            {"header": {"msg_type": "execute_reply"}, "content": {
                "status": "error",
                "ename": "TypeError", "evalue": "from reply", "traceback": [],
                "execution_count": 1,
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.error.value == "from error msg"

    def test_unknown_message_types_ignored(self):
        messages = [
            {"header": {"msg_type": "status"}, "content": {"execution_state": "busy"}},
            {"header": {"msg_type": "execute_input"}, "content": {"code": "x = 1"}},
            {"header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "ok\n"}},
        ]
        result = OutputParser.parse(messages)
        assert result.stdout == "ok\n"
        assert result.success is True


class TestOutputParserDisplayOutputs:
    def test_display_data_html(self):
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"text/html": "<b>bold</b>", "text/plain": "bold"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "text/html"
        assert result.outputs[0].data == "<b>bold</b>"
        assert isinstance(result.outputs[0].data, str)

    def test_display_data_png(self):
        png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"image/png": png_b64, "text/plain": "<Figure>"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "image/png"
        assert isinstance(result.outputs[0].data, bytes)
        assert result.outputs[0].data == b"\x89PNG\r\n\x1a\n"

    def test_display_data_svg_is_text(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>'
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"image/svg+xml": svg, "text/plain": "<SVG>"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "image/svg+xml"
        assert isinstance(result.outputs[0].data, str)

    def test_execute_result_text_only(self):
        """text/plain kept when it's the sole representation."""
        messages = [
            {"header": {"msg_type": "execute_result"}, "content": {
                "data": {"text/plain": "42"},
                "execution_count": 1,
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "text/plain"
        assert result.outputs[0].data == "42"

    def test_text_plain_skipped_when_richer_exists(self):
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"text/html": "<table></table>", "text/plain": "DataFrame"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "text/html"

    def test_multiple_display_outputs(self):
        png_b64 = base64.b64encode(b"png1").decode()
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"image/png": png_b64},
            }},
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"text/html": "<p>chart 2</p>"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 2
        assert result.outputs[0].mime_type == "image/png"
        assert result.outputs[1].mime_type == "text/html"

    def test_display_output_url_defaults_none(self):
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"text/html": "<b>x</b>"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert result.outputs[0].url is None

    def test_json_output(self):
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {"application/json": {"key": "value"}, "text/plain": "{'key': 'value'}"},
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 1
        assert result.outputs[0].mime_type == "application/json"
        assert isinstance(result.outputs[0].data, str)
        assert json.loads(result.outputs[0].data) == {"key": "value"}

    def test_bundle_with_multiple_non_text_types(self):
        """A single bundle with image/png + text/html produces two outputs."""
        png_b64 = base64.b64encode(b"img").decode()
        messages = [
            {"header": {"msg_type": "display_data"}, "content": {
                "data": {
                    "image/png": png_b64,
                    "text/html": "<b>chart</b>",
                    "text/plain": "fallback",
                },
            }},
        ]
        result = OutputParser.parse(messages)
        assert len(result.outputs) == 2
        assert result.outputs[0].mime_type == "image/png"
        assert result.outputs[1].mime_type == "text/html"
