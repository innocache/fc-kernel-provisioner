"""Tests for the vsock client protocol helpers."""

import json
import struct
import pytest
from fc_provisioner.vsock_client import (
    HEADER_FMT, HEADER_SIZE, GUEST_AGENT_PORT,
    _encode_message, _decode_message, _safe_log_msg,
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


class TestSafeLogMsg:
    def test_redacts_key_field(self):
        msg = {"action": "start_kernel", "key": "super-secret"}
        result = _safe_log_msg(msg)
        assert result["key"] == "<redacted>"
        assert result["action"] == "start_kernel"

    def test_redacts_all_sensitive_fields(self):
        msg = {"action": "x", "key": "k", "token": "t", "password": "p", "secret": "s"}
        result = _safe_log_msg(msg)
        for field in ("key", "token", "password", "secret"):
            assert result[field] == "<redacted>"
        assert result["action"] == "x"

    def test_non_sensitive_fields_pass_through(self):
        msg = {"action": "signal", "signum": 15}
        assert _safe_log_msg(msg) == msg

    def test_original_msg_not_mutated(self):
        msg = {"action": "start_kernel", "key": "secret-key"}
        _safe_log_msg(msg)
        assert msg["key"] == "secret-key"
