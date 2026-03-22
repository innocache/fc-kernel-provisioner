"""Edge case tests for the vsock client protocol helpers."""

import json
import struct
import pytest
from fc_provisioner.vsock_client import (
    HEADER_FMT,
    HEADER_SIZE,
    _encode_message,
    _decode_message,
)


class TestEncodeEdgeCases:
    def test_empty_dict(self):
        """Encoding an empty dict should produce valid framed message."""
        encoded = _encode_message({})
        header = struct.unpack(HEADER_FMT, encoded[:HEADER_SIZE])[0]
        payload = json.loads(encoded[HEADER_SIZE:])
        assert payload == {}
        assert header == len(json.dumps({}).encode())

    def test_nested_dict(self):
        """Nested structures should encode correctly."""
        msg = {"ports": {"shell": 5555, "iopub": 5556}}
        encoded = _encode_message(msg)
        decoded = _decode_message(encoded)
        assert decoded == msg

    def test_unicode_values(self):
        """Unicode strings should be handled correctly."""
        msg = {"name": "test-\u00e9\u00e8\u00ea"}
        encoded = _encode_message(msg)
        decoded = _decode_message(encoded)
        assert decoded["name"] == "test-\u00e9\u00e8\u00ea"

    def test_large_payload(self):
        """Large payloads should encode and decode correctly."""
        msg = {"data": "x" * 100_000}
        encoded = _encode_message(msg)
        decoded = _decode_message(encoded)
        assert decoded["data"] == "x" * 100_000

    def test_special_json_characters(self):
        """Strings with quotes, backslashes, newlines."""
        msg = {"value": 'hello "world"\n\\end'}
        encoded = _encode_message(msg)
        decoded = _decode_message(encoded)
        assert decoded == msg


class TestDecodeEdgeCases:
    def test_extra_trailing_bytes_ignored(self):
        """Decoder should only read the declared length, ignoring extra data."""
        msg = {"action": "ping"}
        payload = json.dumps(msg).encode()
        data = struct.pack(HEADER_FMT, len(payload)) + payload + b"EXTRA_GARBAGE"
        decoded = _decode_message(data)
        assert decoded == msg

    def test_truncated_mid_json_raises(self):
        """If payload is cut mid-JSON, json.loads should fail."""
        # A valid JSON string truncated so it's no longer valid
        broken_payload = b'{"action": "pi'  # Cut mid-string
        data = struct.pack(HEADER_FMT, len(broken_payload)) + broken_payload
        with pytest.raises(json.JSONDecodeError):
            _decode_message(data)

    def test_zero_length_payload(self):
        """Zero-length payload is empty string — invalid JSON."""
        data = struct.pack(HEADER_FMT, 0) + b""
        with pytest.raises(json.JSONDecodeError):
            _decode_message(data)

    def test_header_only_no_payload(self):
        """Header declaring 10 bytes but no payload should raise."""
        data = struct.pack(HEADER_FMT, 10)
        with pytest.raises(json.JSONDecodeError):
            _decode_message(data)


class TestRoundTrip:
    @pytest.mark.parametrize(
        "msg",
        [
            {"action": "ping"},
            {"action": "start_kernel", "ports": {"shell_port": 5555}, "key": "abc"},
            {"status": "alive", "uptime": 123.456, "kernel_alive": True},
            {"status": "error", "message": "no kernel running"},
            {},
        ],
    )
    def test_encode_decode_roundtrip(self, msg):
        encoded = _encode_message(msg)
        decoded = _decode_message(encoded)
        assert decoded == msg
