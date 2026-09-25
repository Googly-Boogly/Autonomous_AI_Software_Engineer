"""Isolated git worktrees for engineering runs.

Invariants:
* The operator's primary working tree is never modified (no checkout, no
  reset, no stash). We only add a worktree + branch alongside it.
* Worktrees live under ``<data_dir>/runs/<run_id>/worktree``.
* Cleanup never uses --force and refuses to remove a worktree that has
  uncommitted changes or lives outside the runs directory.
* Branches are never deleted automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from engineer.git import cli
from engineer.git.cli import GitError


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class Workspace:
    run_id: str
    repo: Path
    run_dir: Path
    worktree: Path
    branch: str
    base_commit: str

    @property
    def logs_dir(self) -> Path:
        return self.run_dir / "logs"

    @property
    def artifacts_dir(self) -> Path:
        return self.run_dir / "artifacts"

    @property
    def sandbox_home(self) -> Path:
        return self.run_dir / "home"


def make_branch_name(task_id: str, slug: str) -> str:
    name = f"ai-engineer/{task_id}-{slug}"
    if not cli.is_safe_branch_name(name):
        raise WorkspaceError(f"unsafe branch name generated: {name!r}")
    return name


def validate_repository(repo: Path) -> tuple[Path, list[str]]:
    """Check the primary repo is usable. Returns (toplevel, warnings)."""
    if not repo.exists() or not repo.is_dir():
        raise WorkspaceError(f"repository path does not exist: {repo}")
    top = cli.toplevel(repo)
    if top is None:
        raise WorkspaceError(f"not a git repository: {repo}")
    if top != repo.resolve():
        raise WorkspaceError(
            f"{repo} is inside the git repository {top}; pass the repository root instead"
        )
    try:
        cli.head_commit(top)
    except GitError:
        raise WorkspaceError("repository has no commits; commit something first") from None
    op = cli.in_progress_operation(top)
    if op:
        raise WorkspaceError(f"primary repository has an in-progress {op}; finish it first")
    warnings: list[str] = []
    dirty = cli.status_porcelain(top)
    if dirty:
        warnings.append(
            f"primary working tree has {len(dirty)} uncommitted change(s); they are NOT included "
            "in the run (the worktree is created from HEAD) and will not be touched"
        )
    return top, warnings


def create_workspace(repo: Path, runs_dir: Path, run_id: str, branch: str) -> Workspace:
    top, _ = validate_repository(repo)
    if not cli.is_safe_branch_name(branch):
        raise WorkspaceError(f"unsafe branch name: {branch!r}")
    if cli.branch_exists(top, branch):
        raise WorkspaceError(f"branch already exists: {branch}")

    runs_dir = runs_dir.resolve()
    run_dir = runs_dir / run_id
    worktree = run_dir / "worktree"
    if worktree.exists():
        raise WorkspaceError(f"worktree path already exists: {worktree}")
    # The worktree must not live inside the primary repository.
    if worktree.is_relative_to(top):
        raise WorkspaceError("worktree path would be inside the primary repository")

    base = cli.head_commit(top)
    for d in (run_dir / "logs", run_dir / "artifacts", run_dir / "home"):
        d.mkdir(parents=True, exist_ok=True)
    cli.git(top, "worktree", "add", "-b", branch, str(worktree), base)
    return Workspace(run_id, top, run_dir, worktree, branch, base)


def pending_changes(worktree: Path, base: str) -> list[str]:
    """All paths that differ from ``base``: modified/deleted tracked files plus
    untracked, non-ignored files. Callers must filter this through policy
    before showing contents to a model or committing."""
    tracked = cli.git(worktree, "diff", "--name-only", "--no-renames", "-z", base)
    untracked = cli.git(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted({p for p in (tracked + untracked).split("\0") if p})


def diff_paths(worktree: Path, base: str, paths: list[str], *, stat: bool = False) -> str:
    """Diff of exactly ``paths`` (committed, uncommitted or untracked) against ``base``."""
    if not paths:
        return ""
    untracked = set(cli.git(worktree, "ls-files", "--others", "--exclude-standard",
                            "-z").split("\0")) & set(paths)
    if untracked:
        # Intent-to-add makes new files visible to `git diff` without staging content.
        cli.git(worktree, "add", "--intent-to-add", "--", *sorted(untracked))
    args = ["diff", "--no-color", "--no-ext-diff", "--no-renames"]
    if stat:
        args.append("--stat")
    return cli.git(worktree, *args, base, "--", *paths)


def commit_paths(ws: Workspace, paths: list[str], message: str, author_name: str,
                 author_email: str) -> str | None:
    """Commit exactly ``paths`` on the run branch. Returns the new commit SHA,
    or None if there was nothing to commit."""
    if not paths:
        return None
    cli.git(ws.worktree, "add", "-A", "--", *paths)
    if not cli.git(ws.worktree, "diff", "--cached", "--name-only").strip():
        return None
    cli.git(
        ws.worktree,
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
        "-c", "commit.gpgsign=false",
        "commit", "--no-verify", "-q", "-m", message,
    )
    return cli.head_commit(ws.worktree)


def remove_workspace(ws: Workspace) -> None:
    """Remove the worktree directory (branch is kept). Refuses if there is
    uncommitted work or the path is not a run worktree we created."""
    wt = ws.worktree.resolve()
    if wt.parent != ws.run_dir.resolve() or wt.name != "worktree":
        raise WorkspaceError(f"refusing to remove unexpected path: {wt}")
    if not wt.exists():
        return
    if cli.status_porcelain(wt):
        raise WorkspaceError("worktree has uncommitted changes; refusing to remove it")
    cli.git(ws.repo, "worktree", "remove", str(wt))  # no --force, ever
