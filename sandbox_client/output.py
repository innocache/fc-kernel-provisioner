"""Output parser — converts Jupyter protocol messages into ExecutionResult."""

import base64
import json as _json
from dataclasses import dataclass


@dataclass
class ExecutionError:
    """Error information from a failed execution."""
    name: str
    value: str
    traceback: list[str]


@dataclass
class DisplayOutput:
    """A single rich output (image, HTML, etc.)."""
    mime_type: str
    data: bytes | str
    url: str | None = None


@dataclass
class ExecutionResult:
    """Structured result of a code execution."""
    success: bool
    stdout: str
    stderr: str
    error: ExecutionError | None
    outputs: list[DisplayOutput]
    execution_count: int


# Mime types treated as binary (base64-decoded to bytes).
# All others are kept as str.
_BINARY_MIME_TYPES = frozenset({"image/png"})

# Priority order for selecting display outputs from a mime bundle.
# Lower index = higher priority.  text/plain is only kept when it is
# the sole representation (handled in _extract_display_outputs).
_MIME_PRIORITY = [
    "image/png",
    "image/svg+xml",
    "text/html",
    "application/json",
    "text/plain",
]


def _extract_display_outputs(data: dict) -> list[DisplayOutput]:
    """Extract DisplayOutput entries from a Jupyter mime bundle dict.

    Creates one DisplayOutput per non-text/plain mime type, in priority
    order.  Falls back to text/plain only when it is the sole
    representation.  Binary types are base64-decoded to bytes; text
    types are kept as str.  application/json is serialised to a JSON
    string.
    """
    outputs: list[DisplayOutput] = []
    for mime in _MIME_PRIORITY:
        if mime not in data:
            continue
        if mime == "text/plain" and outputs:
            # Skip text/plain fallback when richer types exist.
            continue
        raw = data[mime]
        if mime in _BINARY_MIME_TYPES:
            decoded = base64.b64decode(raw)
            outputs.append(DisplayOutput(mime_type=mime, data=decoded))
        elif mime == "application/json":
            outputs.append(DisplayOutput(mime_type=mime, data=_json.dumps(raw)))
        else:
            outputs.append(DisplayOutput(mime_type=mime, data=raw))
    return outputs


class OutputParser:
    """Stateless parser for Jupyter kernel protocol messages."""

    @staticmethod
    def parse(messages: list[dict]) -> ExecutionResult:
        """Parse a list of Jupyter messages into an ExecutionResult."""
        stdout = ""
        stderr = ""
        error: ExecutionError | None = None
        outputs: list[DisplayOutput] = []
        execution_count = 0
        success = True

        for msg in messages:
            msg_type = msg.get("header", {}).get("msg_type", "")
            content = msg.get("content", {})

            if msg_type == "stream":
                name = content.get("name", "stdout")
                text = content.get("text", "")
                if name == "stderr":
                    stderr += text
                else:
                    stdout += text

            elif msg_type == "error":
                error = ExecutionError(
                    name=content.get("ename", "Error"),
                    value=content.get("evalue", ""),
                    traceback=content.get("traceback", []),
                )
                success = False

            elif msg_type in ("execute_result", "display_data"):
                bundle = content.get("data", {})
                outputs.extend(_extract_display_outputs(bundle))

            elif msg_type == "execute_reply":
                execution_count = content.get("execution_count", 0) or 0
                # Fallback error detection.
                if content.get("status") == "error" and error is None:
                    error = ExecutionError(
                        name=content.get("ename", "Error"),
                        value=content.get("evalue", ""),
                        traceback=content.get("traceback", []),
                    )
                    success = False

        return ExecutionResult(
            success=success,
            stdout=stdout,
            stderr=stderr,
            error=error,
            outputs=outputs,
            execution_count=execution_count,
        )
