# jerry tool module: Pi-compatible file and shell operations
# Wraps the four Pi tools (read, bash, edit, write) as tool_ functions.

import json
import os
import subprocess
from typing import Any, Dict, List, Literal, Optional


def tool_read(
    path: str,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    """Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp).

    For text files, output is truncated to 2000 lines or 50KB (whichever is hit first).
    Use offset and limit to read large files.

    Args:
        path: File path to read (relative or absolute)
        offset: Line number to start reading from (1-indexed). Default: 1
        limit: Maximum number of lines to read. Default: 2000
    """
    try:
        if not os.path.isfile(path):
            return f"error: file not found: {path}"

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if offset is not None or limit is not None:
                start = (offset or 1) - 1
                lines = []
                line_count = 0
                for line in f:
                    line_count += 1
                    if line_count > start:
                        lines.append(line)
                        if limit and len(lines) >= limit:
                            break
                content = "".join(lines)
            else:
                content = f.read()

        # Truncate to 50KB or 2000 lines
        if len(content) > 50 * 1024:
            lines = content.split("\n", 2000)
            content = "\n".join(lines[:2000])
            content += "\n... [truncated, exceeds 50KB or 2000 lines]"

        return content

    except PermissionError:
        return f"error: permission denied: {path}"
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def tool_bash(
    command: str,
    timeout: Optional[int] = None,
    cwd: Optional[str] = None,
) -> str:
    """Execute a bash command in the current working directory. Returns stdout and stderr.

    Output is truncated to the last 2000 lines or 50KB (whichever is hit first).
    Supports an optional timeout in seconds.

    Args:
        command: Shell command to execute
        timeout: Timeout in seconds (optional)
        cwd: Working directory to execute in (default: current directory)
    """
    try:
        timeout_val = float(timeout) if timeout is not None else None
    except (ValueError, TypeError):
        return f"error: invalid timeout: {timeout}"

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd or os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_val,
        )
    except subprocess.TimeoutExpired as exc:
        return (
            f"timed out after {timeout_val} seconds\n"
            f"exit {exc.returncode}\n"
            f"stdout:\n{exc.output or '(empty)'}\n"
            f"stderr:\n{exc.stderr or '(empty)'}"
        )
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"

    # Truncate output
    stdout = _truncate_output(proc.stdout)
    stderr = _truncate_output(proc.stderr)

    return (
        f"exit {proc.returncode}\n"
        f"stdout:\n{stdout or '(empty)'}\n"
        f"stderr:\n{stderr or '(empty)'}"
    )


def tool_edit(
    path: str,
    edits: List[Dict[str, str]],
) -> str:
    """Make precise edits to a file using exact text replacement.

    Supports multiple edits in one call. Each edit must match a unique,
    non-overlapping region of the original file.

    Args:
        path: File path to edit (relative or absolute)
        edits: List of edit dicts, each with:
            - oldText: Exact text to find (must be unique in file)
            - newText: Replacement text
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            original = f.read()

        content = original
        applied = 0
        errors = []

        for i, edit in enumerate(edits):
            old_text = edit.get("oldText", "")
            new_text = edit.get("newText", "")

            if old_text not in content:
                errors.append(f"edit {i}: oldText not found: {old_text[:100]}")
                continue

            if content.count(old_text) > 1:
                errors.append(
                    f"edit {i}: oldText matches multiple locations"
                )
                continue

            content = content.replace(old_text, new_text, 1)
            applied += 1

        if errors:
            result = f"Partially applied: {applied}/{len(edits)} edits applied.\nErrors:\n"
            result += "\n".join(errors)
            return result

        if applied == 0:
            return "error: no edits were applied"

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return f"Successfully applied {applied} edit(s) to {path}"

    except FileNotFoundError:
        return f"error: file not found: {path}"
    except PermissionError:
        return f"error: permission denied: {path}"
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def tool_write(
    path: str,
    content: str,
) -> str:
    """Create or overwrite a file. Automatically creates parent directories.

    Args:
        path: File path to write (relative or absolute)
        content: Content to write to the file
    """
    try:
        # Create parent directories
        parent_dir = os.path.dirname(path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return f"Successfully wrote {len(content)} bytes to {path}"

    except PermissionError:
        return f"error: permission denied: {path}"
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def _truncate_output(text: str) -> str:
    """Truncate output to last 2000 lines or 50KB."""
    if not text:
        return ""
    if len(text) > 50 * 1024:
        lines = text.split("\n", 2000)
        text = "\n".join(lines[-2000:])
        text = "... [truncated, exceeds 50KB or 2000 lines]\n" + text
    return text
