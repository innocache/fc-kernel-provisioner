"""Canonical tool definitions for the Execution API.

This is the SINGLE SOURCE OF TRUTH for tool schemas.  Every other
representation — claude.json, openai.json, README examples, agent config —
is derived from these definitions.

The format is provider-neutral (Anthropic-style ``input_schema``).
Each LLM provider converts to its own wire format at runtime.

To regenerate the JSON files::

    python -m execution_api.tool_schemas.tools
"""

from __future__ import annotations

import json
import pathlib

TOOLS: list[dict] = [
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
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
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
                "code": {
                    "type": "string",
                    "description": "Panel dashboard Python code",
                },
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


def to_openai(tools: list[dict] | None = None) -> list[dict]:
    """Convert provider-neutral definitions to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        }
        for t in (tools or TOOLS)
    ]


def _generate_json_files() -> None:
    """Regenerate claude.json and openai.json from canonical definitions."""
    here = pathlib.Path(__file__).parent
    with open(here / "claude.json", "w") as f:
        json.dump(TOOLS, f, indent=2)
        f.write("\n")
    with open(here / "openai.json", "w") as f:
        json.dump(to_openai(), f, indent=2)
        f.write("\n")
    print(f"Generated {here / 'claude.json'}")
    print(f"Generated {here / 'openai.json'}")


if __name__ == "__main__":
    _generate_json_files()
