#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time

_APP_DIR = "/apps"
_PORT = 5006
_POLL_INTERVAL = 1.0

_panel_proc = None
_current_file = None


def _latest_dashboard():
    app_file = os.path.join(_APP_DIR, "app.py")
    if os.path.isfile(app_file):
        return app_file
    return None


def _read_prefix():
    try:
        with open(os.path.join(_APP_DIR, ".prefix")) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _start_panel(script_path):
    global _panel_proc, _current_file
    _stop_panel()

    prefix = _read_prefix()
    python = sys.executable or "/usr/bin/python3"
    cmd = [
        python, "-m", "panel", "serve", script_path,
        "--port", str(_PORT),
        "--address", "0.0.0.0",
        "--allow-websocket-origin", "*",
        "--num-procs", "1",
    ]
    if prefix:
        cmd.extend(["--prefix", prefix])
    _panel_proc = subprocess.Popen(
        cmd,
        stdout=open("/tmp/panel-serve.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _current_file = script_path
    print(f"[dispatcher] started panel serve {script_path} prefix={prefix!r} (pid={_panel_proc.pid})", flush=True)


def _stop_panel():
    global _panel_proc, _current_file
    if _panel_proc is not None and _panel_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_panel_proc.pid), signal.SIGTERM)
            _panel_proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(_panel_proc.pid), signal.SIGKILL)
                _panel_proc.wait(timeout=2)
            except Exception:
                pass
    _panel_proc = None
    _current_file = None


def _serve_placeholder():
    """Write and serve a minimal placeholder dashboard."""
    os.makedirs(_APP_DIR, exist_ok=True)
    placeholder = os.path.join(_APP_DIR, "dash_placeholder.py")
    with open(placeholder, "w") as f:
        f.write(
            "import panel as pn\n"
            "app = pn.pane.Markdown('# No dashboard yet\\nAsk the assistant to create one.')\n"
            "app.servable()\n"
        )
    _start_panel(placeholder)


def main():
    os.makedirs(_APP_DIR, exist_ok=True)

    latest = _latest_dashboard()
    if latest:
        _start_panel(latest)
    else:
        _serve_placeholder()

    while True:
        time.sleep(_POLL_INTERVAL)

        newest = _latest_dashboard()
        if newest and newest != _current_file:
            print(f"[dispatcher] new dashboard detected: {newest}", flush=True)
            _start_panel(newest)

        if _panel_proc is not None and _panel_proc.poll() is not None:
            print(f"[dispatcher] panel process died (code={_panel_proc.returncode}), restarting", flush=True)
            if _current_file and os.path.isfile(_current_file):
                _start_panel(_current_file)
            else:
                _serve_placeholder()


if __name__ == "__main__":
    main()
