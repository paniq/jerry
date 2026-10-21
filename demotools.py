# jerry tool module: arbitrary shell command execution
# Use only in a trusted/sandboxed environment.

import subprocess
from typing import Dict, Optional


def tool_run_shell(
    command: str,
    timeout: int = 10,
    cwd: Optional[str] = None,
    stdin: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> str:
    """Run an arbitrary shell command.

    Args:
        command: shell command line to execute
        timeout: seconds to wait for command before killing it
        cwd: working directory to execute command in
        stdin: text passed to command's stdin
        env: environment variables to override/add
    """
    def decode(value) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    try:
        timeout = int(timeout)
    except Exception as exc:
        return f"error: invalid timeout: {exc}"

    if timeout < 0:
        return "error: timeout must be 0 or greater"

    if command is None:
        return "error: command is required"

    if command == "":
        return "command: empty"

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            input=stdin,
            timeout=timeout,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return (
            f"timed out after {timeout} seconds\n"
            f"exit {exc.returncode}\n"
            f"stdout:\n{decode(exc.output) or '(empty)'}\n"
            f"stderr:\n{decode(exc.stderr) or '(empty)'}"
        )
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"

    return (
        f"exit {proc.returncode}\n"
        f"stdout:\n{decode(proc.stdout) or '(empty)'}\n"
        f"stderr:\n{decode(proc.stderr) or '(empty)'}"
    )
