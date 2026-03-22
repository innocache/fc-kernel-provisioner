"""Edge case tests for the fc_guest_agent script."""

import importlib.util
import json
import os
import struct
import time
from unittest.mock import MagicMock, patch

import pytest

AGENT_PATH = os.path.join(os.path.dirname(__file__), "..", "guest", "fc_guest_agent.py")
HEADER_FMT = "!I"


def load_agent_module():
    spec = importlib.util.spec_from_file_location("fc_guest_agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _encode(obj: dict) -> bytes:
    body = json.dumps(obj).encode()
    return struct.pack(HEADER_FMT, len(body)) + body


def _decode(data: bytes) -> dict:
    header_size = struct.calcsize(HEADER_FMT)
    length = struct.unpack(HEADER_FMT, data[:header_size])[0]
    return json.loads(data[header_size : header_size + length])


class TestHandleMessageEdgeCases:
    def _get_fresh_mod(self):
        mod = load_agent_module()
        mod.kernel_proc = None
        mod.boot_time = time.monotonic()
        return mod

    def test_malformed_json_returns_error(self):
        """Non-JSON body should return error, not crash."""
        mod = self._get_fresh_mod()
        # Valid length header but garbage payload
        garbage = b"not json at all"
        data = struct.pack(HEADER_FMT, len(garbage)) + garbage
        response = _decode(mod.handle_message(data))
        assert response["status"] == "error"
        assert "bad message" in response["message"]

    def test_empty_action_returns_error(self):
        mod = self._get_fresh_mod()
        msg = {"action": ""}
        response = _decode(mod.handle_message(_encode(msg)))
        assert response["status"] == "error"
        assert "unknown action" in response["message"]

    def test_missing_action_returns_error(self):
        mod = self._get_fresh_mod()
        msg = {"foo": "bar"}
        response = _decode(mod.handle_message(_encode(msg)))
        assert response["status"] == "error"

    def test_start_kernel_crash_on_launch(self):
        """Kernel process that exits immediately should return error."""
        mod = self._get_fresh_mod()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # Exited immediately

        msg = {
            "action": "start_kernel",
            "ports": {
                "shell_port": 5555,
                "iopub_port": 5556,
                "stdin_port": 5557,
                "control_port": 5558,
                "hb_port": 5559,
            },
            "key": "abc",
        }

        with patch("subprocess.Popen", return_value=mock_proc), patch("time.sleep"):
            response = _decode(mod.handle_message(_encode(msg)))

        assert response["status"] == "error"
        assert "exited immediately" in response["message"]

    def test_start_kernel_missing_ports(self):
        """Missing ports dict should still work (empty dict default)."""
        mod = self._get_fresh_mod()
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None

        msg = {"action": "start_kernel", "key": "abc"}

        with patch("subprocess.Popen", return_value=mock_proc), patch("time.sleep"):
            response = _decode(mod.handle_message(_encode(msg)))

        assert response["status"] == "ready"

    def test_start_kernel_missing_key(self):
        """Missing key should default to empty string."""
        mod = self._get_fresh_mod()
        mock_proc = MagicMock()
        mock_proc.pid = 200
        mock_proc.poll.return_value = None

        msg = {"action": "start_kernel", "ports": {}}

        with patch("subprocess.Popen", return_value=mock_proc), patch("time.sleep"):
            response = _decode(mod.handle_message(_encode(msg)))

        assert response["status"] == "ready"

    def test_restart_kills_existing_then_starts(self):
        """restart_kernel should terminate existing kernel first."""
        mod = self._get_fresh_mod()

        old_proc = MagicMock()
        old_proc.poll.return_value = None
        mod.kernel_proc = old_proc

        new_proc = MagicMock()
        new_proc.pid = 300
        new_proc.poll.return_value = None

        msg = {
            "action": "restart_kernel",
            "ports": {"shell_port": 5555},
            "key": "xyz",
        }

        with patch("subprocess.Popen", return_value=new_proc), patch("time.sleep"):
            response = _decode(mod.handle_message(_encode(msg)))

        old_proc.terminate.assert_called_once()
        assert response["status"] == "ready"

    def test_restart_with_dead_kernel_does_not_terminate(self):
        """If existing kernel already exited, no terminate call needed."""
        mod = self._get_fresh_mod()

        old_proc = MagicMock()
        old_proc.poll.return_value = 0  # Already exited
        mod.kernel_proc = old_proc

        new_proc = MagicMock()
        new_proc.pid = 400
        new_proc.poll.return_value = None

        msg = {"action": "restart_kernel", "ports": {}, "key": "abc"}

        with patch("subprocess.Popen", return_value=new_proc), patch("time.sleep"):
            response = _decode(mod.handle_message(_encode(msg)))

        old_proc.terminate.assert_not_called()
        assert response["status"] == "ready"

    def test_signal_dead_kernel_returns_error(self):
        """Signal to an already-exited kernel should return error."""
        mod = self._get_fresh_mod()
        proc = MagicMock()
        proc.poll.return_value = 0  # Already exited
        mod.kernel_proc = proc

        msg = {"action": "signal", "signum": 2}
        response = _decode(mod.handle_message(_encode(msg)))
        assert response["status"] == "error"
        assert "no kernel" in response["message"].lower()

    def test_signal_default_signum_is_15(self):
        """Signal without signum should default to 15 (SIGTERM)."""
        mod = self._get_fresh_mod()
        proc = MagicMock()
        proc.pid = 500
        proc.poll.return_value = None
        mod.kernel_proc = proc

        msg = {"action": "signal"}  # No signum

        with patch("os.getpgid", return_value=500), patch("os.killpg") as mock_killpg:
            response = _decode(mod.handle_message(_encode(msg)))

        mock_killpg.assert_called_once_with(500, 15)
        assert response["status"] == "ok"

    def test_signal_os_error_returns_error(self):
        """If os.killpg fails, should return error response."""
        mod = self._get_fresh_mod()
        proc = MagicMock()
        proc.pid = 600
        proc.poll.return_value = None
        mod.kernel_proc = proc

        msg = {"action": "signal", "signum": 9}

        with patch("os.getpgid", side_effect=ProcessLookupError("No such process")):
            response = _decode(mod.handle_message(_encode(msg)))

        assert response["status"] == "error"

    def test_ping_with_running_kernel(self):
        """Ping should report kernel_alive=True when kernel is running."""
        mod = self._get_fresh_mod()
        proc = MagicMock()
        proc.poll.return_value = None
        mod.kernel_proc = proc

        msg = {"action": "ping"}
        response = _decode(mod.handle_message(_encode(msg)))
        assert response["status"] == "alive"
        assert response["kernel_alive"] is True
        assert response["uptime"] >= 0

    def test_ping_with_dead_kernel(self):
        """Ping should report kernel_alive=False when kernel has exited."""
        mod = self._get_fresh_mod()
        proc = MagicMock()
        proc.poll.return_value = 1
        mod.kernel_proc = proc

        msg = {"action": "ping"}
        response = _decode(mod.handle_message(_encode(msg)))
        assert response["status"] == "alive"
        assert response["kernel_alive"] is False


class TestRecvExactlyEdgeCases:
    def test_zero_bytes_returns_empty(self):
        """Requesting 0 bytes should return empty bytes immediately."""
        mod = load_agent_module()
        sock = MagicMock()
        result = mod.recv_exactly(sock, 0)
        assert result == b""
        sock.recv.assert_not_called()

    def test_exact_size_single_recv(self):
        """When recv returns exactly the right amount on first call."""
        mod = load_agent_module()
        sock = MagicMock()
        sock.recv.return_value = b"ABCD"
        result = mod.recv_exactly(sock, 4)
        assert result == b"ABCD"
        sock.recv.assert_called_once_with(4)


class TestWriteConnectionFileEdgeCases:
    def test_overwrites_existing_file(self, tmp_path):
        """Writing to an existing file should overwrite it."""
        mod = load_agent_module()
        conn_file = tmp_path / "kernel.json"
        conn_file.write_text("old content")

        ports = {"shell_port": 5555}
        mod.write_connection_file(str(conn_file), ports, "key1")

        data = json.loads(conn_file.read_text())
        assert data["key"] == "key1"
        assert data["shell_port"] == 5555

    def test_empty_key_is_valid(self, tmp_path):
        mod = load_agent_module()
        conn_file = tmp_path / "kernel.json"
        mod.write_connection_file(str(conn_file), {}, "")
        data = json.loads(conn_file.read_text())
        assert data["key"] == ""
