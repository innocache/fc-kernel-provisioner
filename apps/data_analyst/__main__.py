import subprocess
import sys
from pathlib import Path

app_dir = Path(__file__).parent
sys.exit(subprocess.call(
    [sys.executable, "-m", "chainlit", "run", "app.py", *sys.argv[1:]],
    cwd=app_dir,
))
