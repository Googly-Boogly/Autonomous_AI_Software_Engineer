import os

import pytest

from engineer.config import DEFAULT_SENSITIVE_PATTERNS
from engineer.policy.claude_code_gate import gate_tool_request
from engineer.policy.paths import PathPolicy

ENGINEER_MCP = frozenset({"search_code", "list_directory", "git_status", "git_diff", "run_check"})


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "worktree"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / ".env").write_text("SECRET=1\n")
    return root


def gate(ws, tool, tool_input=None, *, builtin=True, allowed_writes=()):
    policy = PathPolicy(ws, DEFAULT_SENSITIVE_PATTERNS, allowed_write_paths=tuple(allowed_writes))
    return gate_tool_request(tool, tool_input or {}, policy=policy,
                             allowed_mcp_tools=ENGINEER_MCP, allow_builtin_file_tools=builtin)


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "MultiEdit"])
def test_file_tools_inside_worktree_allowed(ws, tool):
    d = gate(ws, tool, {"file_path": str(ws / "src" / "app.py")})
    assert d.allowed and d.relative_path == "src/app.py"


def test_relative_paths_are_accepted(ws):
    assert gate(ws, "Read", {"file_path": "src/app.py"}).allowed


@pytest.mark.parametrize("path_fn", [
    lambda ws: str(ws / ".env"),
    lambda ws: str(ws / "config" / ".env.production"),
    lambda ws: str(ws / ".git" / "config"),
    lambda ws: str(ws / "keys" / "id_rsa"),
    lambda ws: str(ws.parent / "outside.py"),
    lambda ws: str(ws / ".." / "outside.py"),
    lambda ws: "/etc/passwd",
    lambda ws: os.path.expanduser("~/.ssh/id_rsa"),
    lambda ws: "~/.aws/credentials",
    lambda ws: "",
    lambda ws: None,
])
@pytest.mark.parametrize("tool", ["Read", "Write", "Edit"])
def test_file_tools_outside_policy_denied(ws, tool, path_fn):
    assert not gate(ws, tool, {"file_path": path_fn(ws)}).allowed


def test_symlink_escape_denied(ws, tmp_path):
    (tmp_path / "secret.txt").write_text("s")
    os.symlink(tmp_path / "secret.txt", ws / "src" / "link.txt")
    d = gate(ws, "Read", {"file_path": str(ws / "src" / "link.txt")})
    assert not d.allowed and "outside" in d.reason


def test_write_restricted_to_allowed_paths(ws):
    assert gate(ws, "Write", {"file_path": str(ws / "src" / "new.py")},
                allowed_writes=["src"]).allowed
    assert not gate(ws, "Write", {"file_path": str(ws / "README.md")},
                    allowed_writes=["src"]).allowed
    assert gate(ws, "Read", {"file_path": str(ws / "README.md")}, allowed_writes=["src"]).allowed


@pytest.mark.parametrize("tool", [
    "Bash", "BashOutput", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit", "Glob",
    "Grep", "Skill", "SlashCommand", "SomeFutureTool",
    "mcp__claude_ai_Gmail__send_email", "mcp__github__merge_pull_request",
    "mcp__engineer__write_file", "mcp__engineer__submit_plan", "mcp__engineerX__run_check",
])
def test_everything_else_is_denied(ws, tool):
    assert not gate(ws, tool, {"command": "rm -rf /"}).allowed


@pytest.mark.parametrize("tool", sorted(ENGINEER_MCP))
def test_exposed_mcp_tools_allowed(ws, tool):
    assert gate(ws, f"mcp__engineer__{tool}").allowed


def test_planner_gets_no_builtin_file_access(ws):
    assert not gate(ws, "Read", {"file_path": str(ws / "src" / "app.py")}, builtin=False).allowed


def test_todo_write_is_harmless_and_allowed(ws):
    assert gate(ws, "TodoWrite", {"todos": []}).allowed
