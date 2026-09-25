"""Deterministic path policy.

Every path a model names is interpreted relative to the run's worktree and
checked here before any filesystem access. The model never supplies absolute
paths, never escapes the worktree (including via symlinks), and never touches
sensitive files.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


class PathDenied(Exception):
    """Raised when a path fails policy. The message is safe to show the model."""


def _matches(rel: PurePosixPath, pattern: str) -> bool:
    """Match a repo-relative path against a gitignore-flavoured pattern.

    ``dir/`` matches that directory name at any depth and everything below it.
    Other patterns are matched against every individual path component and
    against the full relative path.
    """
    if pattern.endswith("/"):
        name = pattern.rstrip("/")
        return any(fnmatch.fnmatchcase(part, name) for part in rel.parts)
    if "/" in pattern:
        full = rel.as_posix()
        return fnmatch.fnmatchcase(full, pattern) or full.startswith(pattern.rstrip("*") + "/")
    return any(fnmatch.fnmatchcase(part, pattern) for part in rel.parts)


@dataclass(frozen=True)
class PathPolicy:
    root: Path
    sensitive_patterns: tuple[str, ...]
    forbidden_paths: tuple[str, ...] = ()
    # If non-empty, writes are allowed only beneath these repo-relative prefixes.
    allowed_write_paths: tuple[str, ...] = ()
    _root_resolved: Path = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_root_resolved", self.root.resolve(strict=True))

    def resolve(self, path: str, *, for_write: bool = False) -> Path:
        """Return the absolute path for ``path`` or raise PathDenied."""
        if not isinstance(path, str) or not path.strip():
            raise PathDenied("path must be a non-empty string")
        if "\x00" in path:
            raise PathDenied("path contains a NUL byte")
        raw = path.strip()
        if raw.startswith("~"):
            raise PathDenied("home-relative paths are not allowed; use repository-relative paths")
        pure = PurePosixPath(raw.replace("\\", "/"))
        if pure.is_absolute():
            raise PathDenied("absolute paths are not allowed; use repository-relative paths")
        if ".." in pure.parts:
            raise PathDenied("'..' is not allowed in paths")

        rel = PurePosixPath(*[p for p in pure.parts if p not in ("", ".")]) if pure.parts else pure
        self._check_patterns(rel, for_write=for_write)

        candidate = self._root_resolved.joinpath(*rel.parts)
        # Resolve symlinks (non-strict: the file may not exist yet for writes)
        # and make sure the real target is still inside the worktree.
        real = candidate.resolve(strict=False)
        try:
            real_rel = real.relative_to(self._root_resolved)
        except ValueError:
            raise PathDenied("path resolves outside the workspace") from None
        # Re-check the resolved path too: a symlink could point at .env.
        self._check_patterns(PurePosixPath(real_rel.as_posix()), for_write=for_write)
        return candidate

    def relative(self, absolute: Path) -> str:
        return absolute.resolve().relative_to(self._root_resolved).as_posix()

    def is_readable(self, rel: str) -> bool:
        try:
            self.resolve(rel)
        except PathDenied:
            return False
        return True

    def _check_patterns(self, rel: PurePosixPath, *, for_write: bool) -> None:
        if not rel.parts:
            if for_write:
                raise PathDenied("cannot write to the workspace root")
            return
        for pattern in self.sensitive_patterns:
            if _matches(rel, pattern):
                raise PathDenied(f"access to sensitive path is denied (matches {pattern!r})")
        for pattern in self.forbidden_paths:
            if _matches(rel, pattern):
                raise PathDenied(f"path is forbidden for this task (matches {pattern!r})")
        if for_write and self.allowed_write_paths:
            posix = rel.as_posix()
            if not any(
                posix == p.rstrip("/") or posix.startswith(p.rstrip("/") + "/")
                for p in self.allowed_write_paths
            ):
                raise PathDenied("path is outside the task's allowed paths")
