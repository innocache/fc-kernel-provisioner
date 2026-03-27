import importlib
import sys
from pathlib import Path

project_root = str(Path(__file__).resolve().parent.parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import apps.data_analyst.app  # noqa: F401, E402 — triggers Chainlit registration
