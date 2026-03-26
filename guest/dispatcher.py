#!/usr/bin/env python3
import importlib.util
import os

import panel as pn

_APP_DIR = "/apps"


def get_dashboard():
    try:
        apps = [f for f in os.listdir(_APP_DIR)
                if f.startswith("dash_") and f.endswith(".py")]
    except FileNotFoundError:
        return pn.pane.Markdown("# No dashboard yet\nAsk the assistant to create one.")

    if not apps:
        return pn.pane.Markdown("# No dashboard yet\nAsk the assistant to create one.")

    apps.sort(key=lambda f: os.path.getmtime(os.path.join(_APP_DIR, f)), reverse=True)
    latest = os.path.join(_APP_DIR, apps[0])

    try:
        spec = importlib.util.spec_from_file_location("dashboard", latest)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return pn.pane.Markdown(
            f"## Dashboard Error\n\n```\n{type(e).__name__}: {e}\n```\n\n"
            "Ask the assistant to fix the code."
        )

    app = getattr(mod, "app", None)
    if app is None:
        return pn.pane.Markdown(
            "## Dashboard Error\n\n"
            "Dashboard code must export an `app` variable.\n\n"
            "Example: `app = pn.Column(widget, plot)`"
        )

    return app


if __name__ == "__main__":
    os.makedirs(_APP_DIR, exist_ok=True)
    pn.serve(
        {"app": get_dashboard},
        port=5006,
        address="0.0.0.0",
        allow_websocket_origin=["*"],
        show=False,
    )
