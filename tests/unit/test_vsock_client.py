"""Tests for the vsock client protocol helpers."""

import json
import struct
import pytest
from fc_provisioner.vsock_client import (
    HEADER_FMT, HEADER_SIZE, GUEST_AGENT_PORT,
    _encode_message, _decode_message,
)


class TestMessageFraming:
    def test_encode_message(self):
        msg = {"action": "ping"}
        encoded = _encode_message(msg)
        payload = json.dumps(msg).encode()
        expected = struct.pack(HEADER_FMT, len(payload)) + payload
        assert encoded == expected

    def test_decode_message(self):
        msg = {"status": "alive", "uptime": 42}
        payload = json.dumps(msg).encode()
        data = struct.pack(HEADER_FMT, len(payload)) + payload
        decoded = _decode_message(data)
        assert decoded == msg

    def test_guest_agent_port(self):
        assert GUEST_AGENT_PORT == 52

    def test_header_size(self):
        assert HEADER_SIZE == 4
