"""Thin, typed wrapper over the git CLI. Only called by trusted Python code."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


class GitError(Exception):
    pass


# Git ref rules (subset of `git check-ref-format`), plus our own conservative charset.
_REF_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


def is_safe_branch_name(name: str) -> bool:
    if not _REF_OK.match(name):
        return False
    bad = ("..", "//", "@{", "/.", ".lock/")
    if any(b in name for b in bad) or name.endswith((".", "/", ".lock")):
        return False
    return not any(part.startswith(".") for part in name.split("/"))


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env


def git(repo: Path, *args: str, check: bool = True, timeout: float = 60) -> str:
    proc = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def toplevel(path: Path) -> Path | None:
    try:
        out = git(path, "rev-parse", "--show-toplevel").strip()
    except (GitError, FileNotFoundError, NotADirectoryError):
        return None
    return Path(out).resolve() if out else None


def head_commit(repo: Path) -> str:
    return git(repo, "rev-parse", "--verify", "HEAD").strip()


def current_branch(repo: Path) -> str | None:
    out = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
    return out or None


def status_porcelain(repo: Path) -> list[str]:
    return [ln for ln in git(repo, "status", "--porcelain=v1", "-uall").splitlines() if ln]


def branch_exists(repo: Path, branch: str) -> bool:
    out = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return bool(out.strip())


def git_dir(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--absolute-git-dir").strip())


def in_progress_operation(repo: Path) -> str | None:
    """Return the name of an in-progress merge/rebase/etc. in the primary repo, if any."""
    gd = git_dir(repo)
    markers = {
        "MERGE_HEAD": "merge",
        "rebase-merge": "rebase",
        "rebase-apply": "rebase/am",
        "CHERRY_PICK_HEAD": "cherry-pick",
        "REVERT_HEAD": "revert",
        "BISECT_LOG": "bisect",
    }
    for marker, name in markers.items():
        if (gd / marker).exists():
            return name
    return None


def tracked_and_untracked_files(repo: Path) -> list[str]:
    """Files git would consider part of the tree (respects .gitignore)."""
    out = git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({p for p in out.split("\0") if p})
