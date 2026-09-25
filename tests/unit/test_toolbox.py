import sys

import pytest

from engineer.config import DEFAULT_SENSITIVE_PATTERNS
from engineer.git.views import CommitView, WorktreeView
from engineer.models.tools import PolicyDecision
from engineer.policy.commands import CommandRegistry
from engineer.policy.paths import PathPolicy
from engineer.providers.base import ToolCall
from engineer.tools.toolbox import Toolbox
from engineer.validation.engine import ValidationEngine


@pytest.fixture
def repo(make_repo):
    r = make_repo("calculator")
    (r / ".env").write_text("API_KEY=sk-live-123\n")  # untracked secret in the tree
    return r


def _engineer_box(repo, tmp_path, actions):
    policy = PathPolicy(repo, DEFAULT_SENSITIVE_PATTERNS)
    reg = CommandRegistry.for_repository(sys.executable, ["pytest"])
    validator = ValidationEngine(reg, repo, tmp_path / "home", timeout=60, tail_chars=2000)
    box = Toolbox(agent="engineer", view=WorktreeView(repo, policy), max_file_bytes=10_000,
                  recorder=actions.append, run_id="run_x", write_policy=policy, registry=reg,
                  validator=validator)
    box.enable_engineering_tools()
    return box


def call(name, **args):
    return ToolCall(id="c1", name=name, arguments=args)


def test_planner_toolbox_is_read_only(repo):
    policy = PathPolicy(repo, DEFAULT_SENSITIVE_PATTERNS)
    box = Toolbox(agent="planner", view=CommitView(repo, "HEAD", policy), max_file_bytes=10_000,
                  recorder=lambda a: None, run_id="r")
    assert set(box.names()) == {"read_file", "list_directory", "search_code"}
    out = box.execute(call("write_file", path="x.py", content="x"))
    assert out.denied and "not available" in out.content


def test_commit_view_hides_untracked_and_sensitive_files(repo):
    (repo / "scratch.py").write_text("uncommitted = True\n")
    view = CommitView(repo, "HEAD", PathPolicy(repo, DEFAULT_SENSITIVE_PATTERNS))
    files = view.list_files()
    assert "calculator/ops.py" in files
    assert "scratch.py" not in files and ".env" not in files
    with pytest.raises(FileNotFoundError):
        view.read_text("scratch.py", 10_000)


def test_read_file_and_line_range(repo, tmp_path):
    actions = []
    box = _engineer_box(repo, tmp_path, actions)
    out = box.execute(call("read_file", path="calculator/ops.py", start_line=1, end_line=1))
    assert out.content.strip() == '"""Basic arithmetic operations."""'
    assert actions[-1].decision == PolicyDecision.ALLOW and actions[-1].ok


def test_read_env_is_denied_and_recorded(repo, tmp_path):
    actions = []
    box = _engineer_box(repo, tmp_path, actions)
    out = box.execute(call("read_file", path=".env"))
    assert out.denied and out.is_error
    assert "sk-live-123" not in out.content
    assert actions[-1].decision == PolicyDecision.DENY
    assert "sensitive" in actions[-1].decision_reason


def test_search_never_returns_sensitive_content(repo, tmp_path):
    box = _engineer_box(repo, tmp_path, [])
    out = box.execute(call("search_code", query="sk-live"))
    assert out.content == "No matches."


def test_list_directory(repo, tmp_path):
    box = _engineer_box(repo, tmp_path, [])
    out = box.execute(call("list_directory", path="."))
    assert "calculator/" in out.content and "tests/" in out.content
    assert ".env" not in out.content


def test_write_and_replace(repo, tmp_path):
    box = _engineer_box(repo, tmp_path, [])
    assert not box.execute(call("write_file", path="pkg/new.py", content="A = 1\n")).is_error
    assert (repo / "pkg" / "new.py").read_text() == "A = 1\n"
    out = box.execute(call("replace_in_file", path="pkg/new.py", old="A = 1", new="A = 2"))
    assert not out.is_error and (repo / "pkg" / "new.py").read_text() == "A = 2\n"


def test_ambiguous_replace_is_rejected_without_changes(repo, tmp_path):
    box = _engineer_box(repo, tmp_path, [])
    before = (repo / "calculator" / "ops.py").read_text()
    out = box.execute(call("replace_in_file", path="calculator/ops.py",
                           old="return a + b", new="return a - b"))
    assert out.is_error and "2 times" in out.content
    assert (repo / "calculator" / "ops.py").read_text() == before


@pytest.mark.parametrize("path", [".env", "../escape.py", "/tmp/x.py", ".git/hooks/pre-commit",
                                  ".github/../.env"])
def test_writes_outside_policy_denied(repo, tmp_path, path):
    box = _engineer_box(repo, tmp_path, [])
    out = box.execute(call("write_file", path=path, content="pwned"))
    assert out.denied


def test_oversized_write_denied(repo, tmp_path):
    box = _engineer_box(repo, tmp_path, [])
    out = box.execute(call("write_file", path="big.txt", content="x" * 20_000))
    assert out.denied and not (repo / "big.txt").exists()


def test_invalid_arguments_rejected(repo, tmp_path):
    box = _engineer_box(repo, tmp_path, [])
    out = box.execute(call("read_file", path="a.py", shell="rm -rf /"))
    assert out.is_error and "Invalid arguments" in out.content


def test_run_check_only_allowlisted(repo, tmp_path):
    box = _engineer_box(repo, tmp_path, [])
    assert box.execute(call("run_check", command_id="bash -c id")).denied
    out = box.execute(call("run_check", command_id="pytest"))
    assert "pytest: PASSED" in out.content  # the calculator's existing tests don't cover subtract


def test_git_status_and_diff_never_expose_sensitive_files(repo, tmp_path):
    # Regression: an untracked .env (e.g. created by a test run) must not leak
    # into git_status / git_diff output via intent-to-add.
    box = _engineer_box(repo, tmp_path, [])
    box.execute(call("write_file", path="NEW.md", content="hello\n"))
    status = box.execute(call("git_status")).content
    diff = box.execute(call("git_diff")).content
    assert "NEW.md" in status and "+hello" in diff
    assert ".env" not in status
    assert ".env" not in diff and "sk-live-123" not in diff


def test_git_diff_no_changes(make_repo, tmp_path):
    box = _engineer_box(make_repo("inventory"), tmp_path, [])
    assert box.execute(call("git_diff")).content == "No changes."


def test_finish(repo, tmp_path):
    box = _engineer_box(repo, tmp_path, [])
    out = box.execute(call("finish", summary="done"))
    assert out.finished and out.finish_summary == "done"
