import os
import socket

import pytest

EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")


def _api_reachable():
    try:
        s = socket.create_connection(("localhost", 8000), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    if not _api_reachable():
        skip = pytest.mark.skip(reason="Execution API not running on :8000")
        for item in items:
            if "e2e" in str(item.fspath):
                item.add_marker(skip)
