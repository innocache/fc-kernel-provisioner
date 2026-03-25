import os
import re

EXECUTION_API_URL = os.environ.get("EXECUTION_API_URL", "http://localhost:8000")
CADDY_BASE_URL = os.environ.get("CADDY_BASE_URL", "http://localhost:8080")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514")

UPLOAD_MAX_BYTES = 50 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
DOWNLOAD_MAX_BYTES = 10 * 1024 * 1024

MAX_HISTORY_TOKENS = 80_000
RECENT_KEEP_FULL = 10
TRUNCATED_OUTPUT_CHARS = 200


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", name)


SYSTEM_PROMPT = """You are a data analytics assistant with access to a sandboxed Python environment.

CAPABILITIES:
- execute_python_code: Run Python in an isolated Firecracker microVM
  - Pre-installed: numpy, pandas, matplotlib, scipy, plotly, seaborn, scikit-learn
  - State persists across calls (variables, imports, files)
  - Uploaded files are at /data/<filename>
- launch_dashboard: Create an interactive Panel dashboard (embedded in chat)
- download_file: Read a file from the sandbox and send it to the user for download

WORKFLOW:
1. When data is uploaded, immediately load it and show df.head(), df.shape, df.dtypes
2. For analysis questions, write and execute Python code step by step
3. Show intermediate results — print DataFrames, statistics, value counts
4. For visualizations, use matplotlib (plots appear inline in chat)
5. For interactive exploration, use launch_dashboard with Panel + hvPlot
6. When the user asks to export/download, save the file in the sandbox then use download_file

RULES:
- Always use matplotlib.use('Agg') before importing pyplot
- Print results explicitly — the chat only sees stdout and images
- For large DataFrames, show .head() or .describe(), not the full frame
- Handle errors gracefully — if code fails, explain and retry
- Be concise in explanations, let the data speak
- When saving files for download, use /data/ as the output directory"""

TOOLS = [
    {
        "name": "execute_python_code",
        "description": (
            "Execute Python code in an isolated sandbox. "
            "Pre-installed: numpy, pandas, matplotlib, scipy, plotly, seaborn. "
            "State persists across calls. Uploaded files are at /data/<filename>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "launch_dashboard",
        "description": (
            "Launch an interactive Panel dashboard in the sandbox and return a URL. "
            "The dashboard can access the same data and variables as execute_python_code. "
            "Use for interactive exploration with widgets, filters, and live charts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Panel dashboard Python code"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "download_file",
        "description": (
            "Read a file from the sandbox and send it to the user for download. "
            "First use execute_python_code to create the file (e.g., df.to_csv, plt.savefig), "
            "then call this tool with the file path to deliver it to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path in /data/ (e.g., /data/report.csv)",
                },
            },
            "required": ["path"],
        },
    },
]
