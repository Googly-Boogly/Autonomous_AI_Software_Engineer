"""Subprocess execution with list-only argv, timeouts, and a scrubbed environment."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Only these variables are forwarded to commands that execute repository code.
# Credentials (tokens, cloud keys, API keys) are never forwarded.
_PASSTHROUGH_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "SYSTEMROOT")


@dataclass
class ProcessResult:
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    start_time: datetime
    end_time: datetime


def scrubbed_env(home: Path | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ}
    if home is not None:
        env["HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["NO_COLOR"] = "1"
    if extra:
        env.update(extra)
    return env


def tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "…[truncated]…\n" + text[-limit:]


def run_process(
    argv: list[str] | tuple[str, ...],
    cwd: Path,
    *,
    timeout: float,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    if isinstance(argv, str):  # defence in depth: never a shell string
        raise TypeError("argv must be a list of arguments, not a string")
    args = [str(a) for a in argv]
    start = datetime.now(UTC)
    try:
        proc = subprocess.run(  # noqa: S603 - list argv, shell=False
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=False,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return ProcessResult(args, proc.returncode, proc.stdout, proc.stderr, False, start,
                             datetime.now(UTC))
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (
            exc.stdout or "")
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (
            exc.stderr or "")
        return ProcessResult(args, None, out, err + f"\n[timed out after {timeout}s]", True,
                             start, datetime.now(UTC))
    except OSError as exc:
        return ProcessResult(args, None, "", f"[could not start: {exc}]", False, start,
                             datetime.now(UTC))
