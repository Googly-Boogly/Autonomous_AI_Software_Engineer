import pytest

from engineer.git import cli
from engineer.git.worktree import (
    WorkspaceError,
    commit_paths,
    create_workspace,
    diff_paths,
    pending_changes,
    remove_workspace,
    validate_repository,
)
from tests.conftest import commit_all

BRANCH = "ai-engineer/task_abc-fix"


def _snapshot(repo):
    """Everything about the primary repo an operator would care about."""
    files = {p.relative_to(repo).as_posix(): p.read_bytes()
             for p in repo.rglob("*") if p.is_file() and ".git" not in p.relative_to(repo).parts}
    return {
        "head": cli.head_commit(repo),
        "branch": cli.current_branch(repo),
        "status": cli.status_porcelain(repo),
        "files": files,
    }


def test_worktree_created_isolated_and_primary_untouched(make_repo, tmp_path):
    repo = make_repo("calculator")
    # Operator has uncommitted + untracked work in progress.
    (repo / "calculator" / "ops.py").write_text("# operator edit in progress\n")
    (repo / "notes.txt").write_text("my notes\n")
    before = _snapshot(repo)

    ws = create_workspace(repo, tmp_path / "runs", "run_1", BRANCH)
    assert ws.worktree.is_dir() and (ws.worktree / "calculator" / "ops.py").exists()
    assert ws.worktree.resolve().is_relative_to((tmp_path / "runs").resolve())
    assert cli.current_branch(ws.worktree) == BRANCH
    assert ws.base_commit == before["head"]
    # The worktree is built from HEAD: operator's uncommitted work is NOT copied in.
    assert "operator edit" not in (ws.worktree / "calculator" / "ops.py").read_text()
    assert not (ws.worktree / "notes.txt").exists()

    # Make and commit changes in the worktree.
    (ws.worktree / "calculator" / "ops.py").write_text("changed in worktree\n")
    (ws.worktree / "new_file.py").write_text("x = 1\n")
    assert pending_changes(ws.worktree, ws.base_commit) == ["calculator/ops.py", "new_file.py"]
    assert "+x = 1" in diff_paths(ws.worktree, ws.base_commit, ["new_file.py"])
    sha = commit_paths(ws, ["calculator/ops.py", "new_file.py"], "msg", "A", "a@localhost")
    assert sha and sha != ws.base_commit

    # The primary tree, index, HEAD and branch are exactly as the operator left them.
    assert _snapshot(repo) == before


def test_commit_paths_only_commits_listed_paths(make_repo, tmp_path):
    ws = create_workspace(make_repo("calculator"), tmp_path / "runs", "run_1", BRANCH)
    (ws.worktree / "ok.py").write_text("ok\n")
    (ws.worktree / ".env").write_text("SECRET=1\n")
    commit_paths(ws, ["ok.py"], "msg", "A", "a@localhost")
    committed = cli.git(ws.worktree, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["ok.py"]


def test_existing_branch_is_refused(make_repo, tmp_path):
    repo = make_repo("calculator")
    cli.git(repo, "branch", BRANCH)
    with pytest.raises(WorkspaceError, match="already exists"):
        create_workspace(repo, tmp_path / "runs", "run_1", BRANCH)


@pytest.mark.parametrize("bad", ["main..x", "-x", "a b", "x.lock", ".hidden"])
def test_unsafe_branch_refused(make_repo, tmp_path, bad):
    with pytest.raises(WorkspaceError, match="unsafe"):
        create_workspace(make_repo("calculator"), tmp_path / "runs", "run_1", bad)


def test_worktree_inside_primary_repo_refused(make_repo):
    repo = make_repo("calculator")
    with pytest.raises(WorkspaceError, match="inside the primary repository"):
        create_workspace(repo, repo / "data" / "runs", "run_1", BRANCH)


def test_invalid_repositories(tmp_path, make_repo):
    with pytest.raises(WorkspaceError, match="does not exist"):
        validate_repository(tmp_path / "nope")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError, match="not a git repository"):
        validate_repository(plain)
    repo = make_repo("calculator")
    with pytest.raises(WorkspaceError, match="pass the repository root"):
        validate_repository(repo / "calculator")


def test_repo_without_commits_refused(tmp_path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    cli.git(repo, "init", "-q")
    with pytest.raises(WorkspaceError, match="no commits"):
        validate_repository(repo)


def test_in_progress_merge_refused(make_repo):
    repo = make_repo("calculator")
    cli.git(repo, "checkout", "-q", "-b", "other")
    (repo / "README.md").write_text("other\n")
    commit_all(repo, "other")
    cli.git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("main\n")
    commit_all(repo, "main")
    cli.git(repo, "merge", "other", check=False)  # conflicts -> MERGE_HEAD
    with pytest.raises(WorkspaceError, match="in-progress merge"):
        validate_repository(repo)


def test_dirty_repo_warns_but_is_allowed(make_repo):
    repo = make_repo("calculator")
    (repo / "wip.txt").write_text("wip\n")
    _, warnings = validate_repository(repo)
    assert warnings and "NOT included" in warnings[0]


def test_cleanup_keeps_branch_and_never_deletes_operator_work(make_repo, tmp_path):
    repo = make_repo("calculator")
    (repo / "operator_file.txt").write_text("precious\n")
    ws = create_workspace(repo, tmp_path / "runs", "run_1", BRANCH)
    (ws.worktree / "x.py").write_text("x\n")
    commit_paths(ws, ["x.py"], "m", "A", "a@localhost")
    remove_workspace(ws)
    assert not ws.worktree.exists()
    assert cli.branch_exists(repo, BRANCH)  # the work survives on its branch
    assert (repo / "operator_file.txt").read_text() == "precious\n"
    assert (repo / "calculator" / "ops.py").exists()


def test_cleanup_refuses_uncommitted_work(make_repo, tmp_path):
    ws = create_workspace(make_repo("calculator"), tmp_path / "runs", "run_1", BRANCH)
    (ws.worktree / "unsaved.py").write_text("x\n")
    with pytest.raises(WorkspaceError, match="uncommitted"):
        remove_workspace(ws)
    assert (ws.worktree / "unsaved.py").exists()


def test_cleanup_refuses_unexpected_paths(make_repo, tmp_path):
    from dataclasses import replace

    ws = create_workspace(make_repo("calculator"), tmp_path / "runs", "run_1", BRANCH)
    forged = replace(ws, worktree=ws.repo)  # pointing cleanup at the primary repo
    with pytest.raises(WorkspaceError, match="refusing to remove"):
        remove_workspace(forged)
    assert ws.repo.exists() and (ws.repo / "calculator").exists()
