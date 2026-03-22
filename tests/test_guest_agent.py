"""Tests for guest agent message handling logic.

The guest agent runs inside the VM, but we can test its pure message
handling functions by importing them directly. We mock subprocess.Popen
since there's no ipykernel available in the test environment.
"""

import json
import struct
import pytest
from unittest.mock import patch, MagicMock

import importlib.util
import os

AGENT_PATH = os.path.join(os.path.dirname(__file__), "..", "guest", "fc_guest_agent.py")


def load_agent_module():
    """Load the guest agent as a module without executing main()."""
    spec = importlib.util.spec_from_file_location("fc_guest_agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWriteConnectionFile:
    def test_writes_valid_json(self, tmp_path):
        agent = load_agent_module()
        path = str(tmp_path / "kernel.json")
        ports = {
            "shell_port": 5555,
            "iopub_port": 5556,
            "stdin_port": 5557,
            "control_port": 5558,
            "hb_port": 5559,
        }
        agent.write_connection_file(path, ports, "test-key-123")

        with open(path) as f:
            conn = json.load(f)

        assert conn["ip"] == "0.0.0.0"
        assert conn["transport"] == "tcp"
        assert conn["key"] == "test-key-123"
        assert conn["signature_scheme"] == "hmac-sha256"
        assert conn["shell_port"] == 5555
        assert conn["iopub_port"] == 5556
        assert conn["hb_port"] == 5559


class TestHandleMessage:
    @patch("subprocess.Popen")
    def test_start_kernel_success(self, mock_popen):
        agent = load_agent_module()
        agent.kernel_proc = None

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        msg = json.dumps({
            "action": "start_kernel",
            "ports": {
                "shell_port": 5555,
                "iopub_port": 5556,
                "stdin_port": 5557,
                "control_port": 5558,
                "hb_port": 5559,
            },
            "key": "abc123",
        }).encode()

        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "ready"
        assert resp["pid"] == 42

    def test_ping_response(self):
        agent = load_agent_module()
        agent.kernel_proc = None

        msg = json.dumps({"action": "ping"}).encode()
        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "alive"
        assert "uptime" in resp
        assert resp["kernel_alive"] is False

    @patch("subprocess.Popen")
    def test_restart_kernel_kills_existing(self, mock_popen):
        agent = load_agent_module()

        # Simulate a running kernel
        old_proc = MagicMock()
        old_proc.poll.return_value = None  # still running
        old_proc.wait.return_value = 0
        agent.kernel_proc = old_proc

        new_proc = MagicMock()
        new_proc.poll.return_value = None
        new_proc.pid = 99
        mock_popen.return_value = new_proc

        msg = json.dumps({
            "action": "restart_kernel",
            "ports": {
                "shell_port": 5555,
                "iopub_port": 5556,
                "stdin_port": 5557,
                "control_port": 5558,
                "hb_port": 5559,
            },
            "key": "newkey",
        }).encode()

        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "ready"
        assert resp["pid"] == 99
        old_proc.terminate.assert_called_once()

    @patch("os.killpg")
    @patch("os.getpgid", return_value=100)
    def test_signal_forwards_to_process_group(self, mock_getpgid, mock_killpg):
        agent = load_agent_module()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # running
        mock_proc.pid = 42
        agent.kernel_proc = mock_proc

        msg = json.dumps({"action": "signal", "signum": 15}).encode()
        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "ok"
        mock_killpg.assert_called_once_with(100, 15)

    def test_signal_no_kernel(self):
        agent = load_agent_module()
        agent.kernel_proc = None

        msg = json.dumps({"action": "signal", "signum": 2}).encode()
        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "error"
        assert "no kernel running" in resp["error"]

    def test_unknown_action(self):
        agent = load_agent_module()
        msg = json.dumps({"action": "explode"}).encode()
        resp_bytes = agent.handle_message(msg)
        resp = json.loads(resp_bytes)

        assert resp["status"] == "error"
        assert "unknown action" in resp["error"]
