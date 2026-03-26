"""Tests for the fc_guest_agent standalone script."""
import importlib.util
import json
import os
import subprocess
import struct
import sys
import time
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

AGENT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "guest", "fc_guest_agent.py")


def load_agent_module() -> Any:
    spec = importlib.util.spec_from_file_location("fc_guest_agent", AGENT_PATH)
    assert spec is not None
    assert spec.loader is not None
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
        mod.write_connection_file(str(conn_file), ports, ip="172.16.0.2")

        assert conn_file.exists(), "Connection file was not created"
        data = json.loads(conn_file.read_text())

        assert data["ip"] == "0.0.0.0"
        assert data["transport"] == "tcp"
        assert data["key"] == ""
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
            "ip": "172.16.0.2",
        }

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("time.sleep"), \
             patch.object(mod, "wait_for_kernel_ports") as mock_wait:
            response = _decode(mod.handle_message(_encode(msg)))

        assert response["status"] == "ready"
        assert response["pid"] == 12345
        mock_wait.assert_called_once_with("172.16.0.2", msg["ports"], proc=mock_proc)
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


class TestPreWarmKernel:
    def _get_fresh_mod(self):
        mod = load_agent_module()
        mod.kernel_proc = None
        mod._kernel_ports = None
        return mod

    def test_pre_warm_kernel_starts_kernel(self, tmp_path):
        mod = self._get_fresh_mod()
        mock_proc = MagicMock()
        mock_proc.pid = 43210
        mock_proc.poll.return_value = None
        with patch.object(mod, "_CONN_FILE", str(tmp_path / "conn.json")), \
             patch.object(mod, "_KERNEL_LOG", str(tmp_path / "kernel.log")), \
             patch.object(mod, "start_kernel", wraps=mod.start_kernel) as mock_start, \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("time.sleep"), \
             patch.object(mod, "wait_for_kernel_ports"):
            mod.pre_warm_kernel()

        mock_start.assert_called_once_with(mod._DEFAULT_PORTS, "0.0.0.0")

    def test_pre_warm_kernel_stores_ports(self, tmp_path):
        mod = self._get_fresh_mod()
        mock_proc = MagicMock()
        mock_proc.pid = 11111
        mock_proc.poll.return_value = None
        with patch.object(mod, "_CONN_FILE", str(tmp_path / "conn.json")), \
             patch.object(mod, "_KERNEL_LOG", str(tmp_path / "kernel.log")), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch("time.sleep"), \
             patch.object(mod, "wait_for_kernel_ports"):
            info = mod.pre_warm_kernel()

        assert mod._kernel_ports == mod._DEFAULT_PORTS
        assert info["ports"] == mod._DEFAULT_PORTS

    def test_get_kernel_info_returns_stored_values(self):
        mod = self._get_fresh_mod()
        proc = MagicMock()
        proc.poll.return_value = None
        mod.kernel_proc = proc
        mod._kernel_ports = dict(mod._DEFAULT_PORTS)

        info = mod.get_kernel_info()

        assert info["ports"] == mod._DEFAULT_PORTS
        assert info["running"] is True

    def test_get_kernel_info_before_prewarm(self):
        mod = self._get_fresh_mod()

        info = mod.get_kernel_info()

        assert info["ports"] is None
        assert info["running"] is False

    def test_pre_warm_kernel_handle_message(self):
        mod = self._get_fresh_mod()
        msg = {"action": "pre_warm_kernel"}

        with patch.object(mod, "start_kernel", return_value=54321):
            response = _decode(mod.handle_message(_encode(msg)))

        assert response["status"] == "ok"
        assert response["ports"] == mod._DEFAULT_PORTS
        assert response["pid"] == 54321

    def test_get_kernel_info_handle_message(self):
        mod = self._get_fresh_mod()
        mod._kernel_ports = dict(mod._DEFAULT_PORTS)
        proc = MagicMock()
        proc.poll.return_value = None
        mod.kernel_proc = proc

        response = _decode(mod.handle_message(_encode({"action": "get_kernel_info"})))

        assert response["status"] == "ok"
        assert response["ports"] == mod._DEFAULT_PORTS
        assert response["running"] is True


class TestDashboardLifecycle:
    def _get_fresh_mod(self):
        mod = load_agent_module()
        mod.kernel_proc = None
        mod.panel_proc = None
        return mod

    def test_launch_dashboard_writes_file_and_starts_panel(self, tmp_path):
        mod = self._get_fresh_mod()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch.object(mod, "_APPS_DIR", str(tmp_path)), \
             patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(mod, "wait_for_kernel_ports"):
            mod.start_dashboard("print('x')", 5006, "app1", "sess1")
        assert (tmp_path / "dash_app1.py").exists()

    def test_launch_dashboard_kills_existing_panel_first(self, tmp_path):
        mod = self._get_fresh_mod()
        existing = MagicMock()
        existing.poll.return_value = None
        mod.panel_proc = existing
        new_proc = MagicMock()
        new_proc.poll.return_value = None
        with patch.object(mod, "_APPS_DIR", str(tmp_path)), \
             patch.object(mod, "_kill_proc") as kill_proc, \
             patch("subprocess.Popen", return_value=new_proc), \
             patch.object(mod, "wait_for_kernel_ports"):
            mod.start_dashboard("print('x')", 5006, "app1", "sess1")
        kill_proc.assert_called_once_with(existing)

    def test_launch_dashboard_port_timeout_returns_error(self):
        mod = self._get_fresh_mod()
        msg = {
            "action": "launch_dashboard",
            "code": "import panel as pn",
            "port": 5006,
            "app_id": "app1",
            "session_id": "sess1",
        }
        with patch.object(mod, "start_dashboard", side_effect=RuntimeError("panel serve did not start: timeout")):
            response = _decode(mod.handle_message(_encode(msg)))
        assert response["status"] == "error"

    def test_launch_dashboard_process_exits_early_returns_error(self):
        mod = self._get_fresh_mod()
        msg = {
            "action": "launch_dashboard",
            "code": "import panel as pn",
            "port": 5006,
            "app_id": "app1",
            "session_id": "sess1",
        }
        with patch.object(mod, "start_dashboard", side_effect=RuntimeError("panel exited immediately")):
            response = _decode(mod.handle_message(_encode(msg)))
        assert response["status"] == "error"

    def test_launch_dashboard_missing_code_returns_error(self):
        mod = self._get_fresh_mod()
        response = _decode(mod.handle_message(_encode({"action": "launch_dashboard", "session_id": "s1"})))
        assert response["status"] == "error"

    def test_stop_dashboard_kills_process(self):
        mod = self._get_fresh_mod()
        proc = MagicMock()
        proc.poll.return_value = None
        mod.panel_proc = proc
        with patch.object(mod, "_kill_proc") as kill_proc:
            mod.stop_dashboard()
        kill_proc.assert_called_once_with(proc)
        assert mod.panel_proc is None

    def test_stop_dashboard_idempotent_when_no_panel(self):
        mod = self._get_fresh_mod()
        mod.panel_proc = None
        mod.stop_dashboard()
        assert mod.panel_proc is None

    def test_stop_dashboard_cleans_app_files(self, tmp_path):
        mod = self._get_fresh_mod()
        (tmp_path / "dash_a.py").write_text("x")
        (tmp_path / "dash_b.py").write_text("x")
        with patch.object(mod, "_APPS_DIR", str(tmp_path)):
            mod.stop_dashboard()
        assert not (tmp_path / "dash_a.py").exists()
        assert not (tmp_path / "dash_b.py").exists()

    def test_kill_proc_sigterm_then_sigkill(self):
        mod = self._get_fresh_mod()
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="x", timeout=1), None]
        mod._kill_proc(proc, timeout=0.01)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


class TestNetworkReconfigure:
    def _get_fresh_mod(self):
        mod = load_agent_module()
        mod.kernel_proc = None
        mod.panel_proc = None
        return mod

    def test_reconfigure_network_runs_all_commands(self):
        mod = self._get_fresh_mod()
        ip = "172.16.0.22"
        mac = "02:fc:00:00:00:22"
        gateway = "172.16.0.1"

        with patch("subprocess.run") as mock_run:
            mod.reconfigure_network(ip, mac, gateway)

        assert mock_run.call_args_list == [
            call(["/sbin/ip", "link", "set", "eth0", "down"], check=True, capture_output=True, timeout=5),
            call(["/sbin/ip", "link", "set", "eth0", "address", mac], check=True, capture_output=True, timeout=5),
            call(["/sbin/ip", "addr", "flush", "dev", "eth0"], check=True, capture_output=True, timeout=5),
            call(["/sbin/ip", "addr", "add", f"{ip}/24", "dev", "eth0"], check=True, capture_output=True, timeout=5),
            call(["/sbin/ip", "link", "set", "eth0", "up"], check=True, capture_output=True, timeout=5),
            call(["/sbin/ip", "route", "replace", "default", "via", gateway, "dev", "eth0"], check=True, capture_output=True, timeout=5),
            call(["/usr/sbin/arping", "-c", "1", "-U", "-I", "eth0", ip], check=False, capture_output=True, timeout=5),
        ]

    def test_reconfigure_network_missing_ip_returns_error(self):
        mod = self._get_fresh_mod()
        response = _decode(mod.handle_message(_encode({
            "action": "reconfigure_network",
            "ip": "",
            "mac": "02:fc:00:00:00:22",
            "gateway": "172.16.0.1",
        })))
        assert response["status"] == "error"
        assert response["message"] == "ip, mac, and gateway required"

    def test_reconfigure_network_missing_mac_returns_error(self):
        mod = self._get_fresh_mod()
        response = _decode(mod.handle_message(_encode({
            "action": "reconfigure_network",
            "ip": "172.16.0.22",
            "mac": "",
            "gateway": "172.16.0.1",
        })))
        assert response["status"] == "error"
        assert response["message"] == "ip, mac, and gateway required"

    def test_reconfigure_network_missing_gateway_returns_error(self):
        mod = self._get_fresh_mod()
        response = _decode(mod.handle_message(_encode({
            "action": "reconfigure_network",
            "ip": "172.16.0.22",
            "mac": "02:fc:00:00:00:22",
            "gateway": "",
        })))
        assert response["status"] == "error"
        assert response["message"] == "ip, mac, and gateway required"

    def test_reconfigure_network_command_failure_returns_error(self):
        mod = self._get_fresh_mod()
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(returncode=1, cmd=["ip"])):
            response = _decode(mod.handle_message(_encode({
                "action": "reconfigure_network",
                "ip": "172.16.0.22",
                "mac": "02:fc:00:00:00:22",
                "gateway": "172.16.0.1",
            })))
        assert response["status"] == "error"
        assert response.get("message", "")

    def test_reconfigure_network_arping_not_found_ok(self):
        mod = self._get_fresh_mod()
        with patch("subprocess.run", side_effect=[None, None, None, None, None, None, FileNotFoundError()] ) as mock_run:
            mod.reconfigure_network("172.16.0.22", "02:fc:00:00:00:22", "172.16.0.1")
        assert mock_run.call_count == 7

    def test_reconfigure_network_custom_netmask(self):
        mod = self._get_fresh_mod()
        with patch("subprocess.run") as mock_run:
            mod.reconfigure_network("172.16.0.22", "02:fc:00:00:00:22", "172.16.0.1", netmask="16")
        assert mock_run.call_args_list[3] == call(
            ["/sbin/ip", "addr", "add", "172.16.0.22/16", "dev", "eth0"],
            check=True,
            capture_output=True,
            timeout=5,
        )
