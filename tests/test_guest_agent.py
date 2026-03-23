"""Tests for the fc_guest_agent standalone script."""
import importlib.util
import json
import os
import struct
import sys
import time
from unittest.mock import MagicMock, call, patch

import pytest

AGENT_PATH = os.path.join(os.path.dirname(__file__), "..", "guest", "fc_guest_agent.py")


def load_agent_module():
    spec = importlib.util.spec_from_file_location("fc_guest_agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# TestRecvExactly
# ---------------------------------------------------------------------------

class TestRecvExactly:
    def _make_sock(self, chunks: list):
        """Return a mock socket whose recv() yields *chunks* in sequence."""
        sock = MagicMock()
        sock.recv.side_effect = chunks
        return sock

    def test_single_recv_returns_all_bytes(self):
        mod = load_agent_module()
        sock = self._make_sock([b"hello"])
        result = mod.recv_exactly(sock, 5)
        assert result == b"hello"
        sock.recv.assert_called_once_with(5)

    def test_multiple_partial_recvs_are_reassembled(self):
        mod = load_agent_module()
        # Simulate three partial reads for a 6-byte request
        sock = self._make_sock([b"ab", b"cd", b"ef"])
        result = mod.recv_exactly(sock, 6)
        assert result == b"abcdef"
        assert sock.recv.call_count == 3

    def test_connection_closed_mid_stream_raises(self):
        mod = load_agent_module()
        # Two bytes arrive then the connection closes (empty bytes)
        sock = self._make_sock([b"ab", b""])
        with pytest.raises(ConnectionError):
            mod.recv_exactly(sock, 6)

    def test_connection_closed_immediately_raises(self):
        mod = load_agent_module()
        sock = self._make_sock([b""])
        with pytest.raises(ConnectionError):
            mod.recv_exactly(sock, 4)

    def test_recv_requests_only_remaining_bytes(self):
        """Each recv() call should request only the outstanding byte count."""
        mod = load_agent_module()
        sock = self._make_sock([b"A", b"BC", b"D"])
        result = mod.recv_exactly(sock, 4)
        assert result == b"ABCD"
        assert sock.recv.call_args_list == [call(4), call(3), call(1)]


# ---------------------------------------------------------------------------
# TestWriteConnectionFile
# ---------------------------------------------------------------------------


class TestWriteConnectionFile:
    def test_writes_valid_connection_json(self, tmp_path):
        mod = load_agent_module()
        conn_file = tmp_path / "kernel.json"
        ports = {
            "shell_port": 5555,
            "iopub_port": 5556,
            "stdin_port": 5557,
            "control_port": 5558,
            "hb_port": 5559,
        }
        key = "test-key-abc"
        mod.write_connection_file(str(conn_file), ports, key, ip="172.16.0.2")

        assert conn_file.exists(), "Connection file was not created"
        data = json.loads(conn_file.read_text())

        assert data["ip"] == "0.0.0.0"
        assert data["transport"] == "tcp"
        assert data["key"] == key
        assert data["signature_scheme"] == "hmac-sha256"
        for port_name, port_val in ports.items():
            assert data[port_name] == port_val, f"Port {port_name} mismatch"


# ---------------------------------------------------------------------------
# TestHandleMessage
# ---------------------------------------------------------------------------

HEADER_FMT = "!I"


def _encode(obj: dict) -> bytes:
    body = json.dumps(obj).encode()
    return struct.pack(HEADER_FMT, len(body)) + body


def _decode(data: bytes) -> dict:
    header_size = struct.calcsize(HEADER_FMT)
    length = struct.unpack(HEADER_FMT, data[:header_size])[0]
    return json.loads(data[header_size: header_size + length])


class TestHandleMessage:
    def _get_fresh_mod(self):
        """Load a fresh copy of the module so module-globals are reset."""
        mod = load_agent_module()
        mod.kernel_proc = None
        mod.boot_time = time.monotonic()
        return mod

    def test_start_kernel_success(self):
        mod = self._get_fresh_mod()
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # still running

        msg = {
            "action": "start_kernel",
            "ports": {
                "shell_port": 5555,
                "iopub_port": 5556,
                "stdin_port": 5557,
                "control_port": 5558,
                "hb_port": 5559,
            },
            "key": "abc123",
            "ip": "172.16.0.2",
        }

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("time.sleep"), \
             patch.object(mod, "wait_for_kernel_ports") as mock_wait:
            response = _decode(mod.handle_message(_encode(msg)))

        assert response["status"] == "ready"
        assert response["pid"] == 12345
        mock_wait.assert_called_once_with("172.16.0.2", msg["ports"])
        assert mock_popen.call_args.kwargs["env"]["PYTHONHASHSEED"] == "0"

    def test_ping_response(self):
        mod = self._get_fresh_mod()
        msg = {"action": "ping"}
        response = _decode(mod.handle_message(_encode(msg)))

        assert response["status"] == "alive"
        assert "uptime" in response
        assert response["kernel_alive"] is False

    def test_restart_kernel_kills_existing(self):
        mod = self._get_fresh_mod()

        old_proc = MagicMock()
        old_proc.poll.return_value = None  # running
        mod.kernel_proc = old_proc

        new_proc = MagicMock()
        new_proc.pid = 99999
        new_proc.poll.return_value = None

        msg = {
            "action": "restart_kernel",
            "ports": {
                "shell_port": 6555,
                "iopub_port": 6556,
                "stdin_port": 6557,
                "control_port": 6558,
                "hb_port": 6559,
            },
            "key": "xyz",
        }

        with patch("subprocess.Popen", return_value=new_proc), \
             patch("time.sleep"), \
             patch.object(mod, "wait_for_kernel_ports"):
            response = _decode(mod.handle_message(_encode(msg)))

        old_proc.terminate.assert_called_once()
        assert response["status"] == "ready"
        assert response["pid"] == 99999

    def test_signal_forwards_to_process_group(self):
        mod = self._get_fresh_mod()

        running_proc = MagicMock()
        running_proc.pid = 77777
        running_proc.poll.return_value = None
        mod.kernel_proc = running_proc

        msg = {"action": "signal", "signum": 15}

        with patch("os.getpgid", return_value=77777) as mock_getpgid, \
             patch("os.killpg") as mock_killpg:
            response = _decode(mod.handle_message(_encode(msg)))

        mock_getpgid.assert_called_once_with(77777)
        mock_killpg.assert_called_once_with(77777, 15)
        assert response["status"] == "ok"

    def test_signal_no_kernel(self):
        mod = self._get_fresh_mod()
        mod.kernel_proc = None

        msg = {"action": "signal", "signum": 15}
        response = _decode(mod.handle_message(_encode(msg)))

        assert response["status"] == "error"
        assert "no kernel" in response.get("message", "").lower()

    def test_unknown_action(self):
        mod = self._get_fresh_mod()
        msg = {"action": "frobnicate"}
        response = _decode(mod.handle_message(_encode(msg)))

        assert response["status"] == "error"
