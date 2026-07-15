"""tools/file_tool.py — read and write local files"""

import os, logging
from langchain_core.tools import Tool
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
log = logging.getLogger(__name__)
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)
class WriteFileInput(BaseModel):
    content: str
    filename: str = "output.txt"

def _safe_output_path(filename: str) -> str:
    """Resolve a filename inside OUTPUT_DIR, rejecting any attempt to escape it."""
    base = os.path.abspath(OUTPUT_DIR)
    candidate = os.path.abspath(os.path.join(base, filename))
    if not (candidate == base or candidate.startswith(base + os.sep)):
        raise ValueError(f"Path '{filename}' escapes the output/ sandbox — not allowed.")
    return candidate


def read_file(path: str) -> str:
    try:
        # Only ever read from within output/, regardless of what path looks like.
        safe_path = _safe_output_path(os.path.basename(path) if os.path.isabs(path) else path)
        if os.path.exists(safe_path):
            with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        return f"File not found: {path}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(content: str, filename: str = "output.txt") -> str:
    try:
        safe_path = _safe_output_path(filename)
        os.makedirs(os.path.dirname(safe_path) or ".", exist_ok=True)

        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)

        return f"Saved to {safe_path}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error writing file: {e}"


read_file_tool = Tool(
    name="read_file",
    func=read_file,
    description="Read a local file. Input: file path string.",
)

write_file_tool = StructuredTool.from_function(
    func=write_file,
    name="write_file",
    description="Write text to a local file. Takes content and filename.",
    args_schema=WriteFileInput,
)

from tools.registry import register  # noqa: E402
register(read_file_tool, routes=["FILES", "GENERAL"])
register(write_file_tool, routes=["FILES", "GENERAL"])
