import os
import socket
from urllib.parse import urlparse

import pytest

EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")


def _api_reachable():
    parsed = urlparse(EXECUTION_API_URL)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8000
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    if not _api_reachable():
        skip = pytest.mark.skip(reason=f"Execution API not reachable at {EXECUTION_API_URL}")
        for item in items:
            if "e2e" in str(item.fspath):
                item.add_marker(skip)
