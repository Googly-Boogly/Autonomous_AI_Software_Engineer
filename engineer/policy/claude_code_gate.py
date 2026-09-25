"""Deterministic gate for tool requests made by Claude Code.

Claude Code proposes tool calls; this module decides. It is deny-by-default:
only the tools named here are ever permitted, and file tools must target a
non-sensitive path inside the run's worktree (checked by PathPolicy, with
symlinks resolved). Bash, web access, sub-agents, and anything unknown are
denied. The gate is a pure function so it can be tested without Claude Code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engineer.policy.paths import PathDenied, PathPolicy

# Claude Code built-in tools the engineer may use, and whether they write.
BUILTIN_FILE_TOOLS: dict[str, bool] = {
    "Read": False,
    "Write": True,
    "Edit": True,
    "MultiEdit": True,
}
# Built-ins with no side effects outside the conversation.
BUILTIN_HARMLESS: frozenset[str] = frozenset({"TodoWrite"})

MCP_SERVER_NAME = "engineer"
MCP_PREFIX = f"mcp__{MCP_SERVER_NAME}__"


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str = ""
    relative_path: str | None = None


def _to_relative(raw: object, worktree: Path) -> str:
    """Claude Code passes absolute file paths; policy works on repo-relative ones.
    PathPolicy then resolves symlinks and re-checks containment."""
    if not isinstance(raw, str) or not raw.strip():
        raise PathDenied("file_path must be a non-empty string")
    p = Path(raw)
    if not p.is_absolute():
        return raw
    if ".." in p.parts:
        raise PathDenied("'..' is not allowed in paths")
    for root in (worktree, worktree.resolve()):
        if p.is_relative_to(root):
            return p.relative_to(root).as_posix() or "."
    raise PathDenied("path is outside the workspace")


def gate_tool_request(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    policy: PathPolicy,
    allowed_mcp_tools: frozenset[str],
    allow_builtin_file_tools: bool,
) -> GateDecision:
    if tool_name.startswith(MCP_PREFIX):
        short = tool_name[len(MCP_PREFIX):]
        if short in allowed_mcp_tools:
            # Our MCP tools apply policy themselves inside the Toolbox.
            return GateDecision(True)
        return GateDecision(False, f"tool {tool_name!r} is not available to this agent")

    if tool_name in BUILTIN_HARMLESS:
        return GateDecision(True)

    if tool_name in BUILTIN_FILE_TOOLS:
        if not allow_builtin_file_tools:
            return GateDecision(False, f"{tool_name} is not available to this agent")
        writes = BUILTIN_FILE_TOOLS[tool_name]
        try:
            rel = _to_relative(tool_input.get("file_path"), policy.root)
            policy.resolve(rel, for_write=writes)
        except PathDenied as exc:
            return GateDecision(False, str(exc))
        return GateDecision(True, relative_path=rel)

    return GateDecision(
        False,
        f"{tool_name} is not permitted in this environment (no shell, network, or sub-agents)",
    )
