"""Read-only, policy-filtered views over repository content.

* CommitView reads blobs straight from the git object database at a fixed
  commit. Analysis and planning use it, so they see exactly the commit the
  worktree will be created from and never read the operator's uncommitted or
  untracked files.
* WorktreeView reads the run's isolated worktree during implementation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from typing import Protocol

from engineer.git import cli
from engineer.policy.paths import PathDenied, PathPolicy


class FileTooLarge(Exception):
    pass


class RepoView(Protocol):
    policy: PathPolicy

    def list_files(self) -> list[str]: ...

    def read_text(self, path: str, max_bytes: int) -> str:
        """Raise PathDenied / FileNotFoundError / FileTooLarge on failure."""
        ...


class CommitView:
    def __init__(self, repo: Path, commit: str, policy: PathPolicy) -> None:
        self.repo = repo
        self.commit = commit
        self.policy = policy
        self._files: list[str] | None = None

    def list_files(self) -> list[str]:
        if self._files is None:
            out = cli.git(self.repo, "ls-tree", "-r", "--name-only", "-z", self.commit)
            self._files = sorted(f for f in out.split("\0") if f and self.policy.is_readable(f))
        return self._files

    def read_text(self, path: str, max_bytes: int) -> str:
        self.policy.resolve(path)  # pattern checks; raises PathDenied
        norm = PurePosixPath(path.strip()).as_posix()
        if norm not in set(self.list_files()):
            raise FileNotFoundError(path)
        spec = f"{self.commit}:{norm}"
        size = int(cli.git(self.repo, "cat-file", "-s", spec).strip())
        if size > max_bytes:
            raise FileTooLarge(f"{path} is {size} bytes (limit {max_bytes})")
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.repo), "cat-file", "blob", spec],
            capture_output=True, timeout=30, check=False, stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            raise FileNotFoundError(path)
        return proc.stdout.decode("utf-8", errors="replace")


class WorktreeView:
    def __init__(self, worktree: Path, policy: PathPolicy) -> None:
        self.worktree = worktree
        self.policy = policy

    def list_files(self) -> list[str]:
        return [f for f in cli.tracked_and_untracked_files(self.worktree)
                if self.policy.is_readable(f)]

    def read_text(self, path: str, max_bytes: int) -> str:
        target = self.policy.resolve(path)
        if not target.is_file():
            raise FileNotFoundError(path)
        size = target.stat().st_size
        if size > max_bytes:
            raise FileTooLarge(f"{path} is {size} bytes (limit {max_bytes})")
        return target.read_text(encoding="utf-8", errors="replace")


__all__ = ["CommitView", "FileTooLarge", "PathDenied", "RepoView", "WorktreeView"]
