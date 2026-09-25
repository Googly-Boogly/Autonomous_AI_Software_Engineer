"""Deterministic repository analysis.

Produces a compact RepositoryContext without any model involvement and
without reading sensitive files. Only files git considers part of the tree
(tracked + untracked-but-not-ignored) are considered.
"""

from __future__ import annotations

import re
import tomllib
from collections import Counter
from pathlib import PurePosixPath

from engineer.git.views import FileTooLarge, RepoView
from engineer.models.repo_context import InstructionDoc, RelevantFile, RepositoryContext
from engineer.policy.paths import PathDenied

LANGUAGE_BY_EXT = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".java": "Java",
    ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C",
    ".cpp": "C++", ".hpp": "C++", ".sh": "Shell", ".sql": "SQL",
}
FRAMEWORK_MARKERS = {
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "starlette": "Starlette",
    "sqlalchemy": "SQLAlchemy", "pydantic": "Pydantic", "click": "Click", "typer": "Typer",
}
CONFIG_FILES = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "requirements-dev.txt",
    "Pipfile", "tox.ini", "pytest.ini", "mypy.ini", ".mypy.ini", "ruff.toml", ".ruff.toml",
    "package.json", "Dockerfile", "compose.yaml", "docker-compose.yml", "Makefile",
    ".pre-commit-config.yaml",
)
INSTRUCTION_DOCS = ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", "README.md", "README.rst")
_STOPWORDS = frozenset(
    "a an and are as at be by add for from in into is it of on or that the this to with "
    "should returning return returns test tests make ensure new use when fix".split()
)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for t in _TOKEN.findall(text):
        low = t.lower()
        if low not in _STOPWORDS:
            out.add(low)
            out.update(p for p in low.split("_") if len(p) > 2 and p not in _STOPWORDS)
    return out


class RepositoryAnalyzer:
    """Analyzes a policy-filtered RepoView (files hidden by policy are never read)."""

    def __init__(self, view: RepoView, *, max_relevant: int = 12) -> None:
        self.view = view
        self.max_relevant = max_relevant

    def _read(self, path: str, limit: int) -> str:
        try:
            return self.view.read_text(path, max_bytes=2_000_000)[:limit]
        except (PathDenied, FileNotFoundError, FileTooLarge, OSError):
            return ""

    def analyze(
        self, task_text: str, *, root: str, head_commit: str, current_branch: str | None
    ) -> RepositoryContext:
        files = self.view.list_files()
        warnings: list[str] = []

        languages = Counter(
            LANGUAGE_BY_EXT[s] for f in files if (s := PurePosixPath(f).suffix) in LANGUAGE_BY_EXT
        )
        config_files = [f for f in files if PurePosixPath(f).name in CONFIG_FILES]
        ci = [f for f in files if f.startswith(".github/workflows/") or f == ".gitlab-ci.yml"]
        deps_text = self._dependency_text(config_files)
        frameworks = sorted(
            {name for marker, name in FRAMEWORK_MARKERS.items()
             if re.search(rf"\b{marker}\b", deps_text)}
        )
        test_frameworks, commands = self._detect_tooling(files, deps_text)

        return RepositoryContext(
            root=root,
            head_commit=head_commit,
            current_branch=current_branch,
            languages=dict(languages.most_common()),
            frameworks=frameworks,
            test_frameworks=test_frameworks,
            directory_tree=self._tree(files),
            total_files=len(files),
            config_files=config_files,
            ci_workflows=ci,
            instruction_docs=self._instruction_docs(files),
            available_commands=commands,
            relevant_files=self._relevant(files, task_text),
            warnings=warnings,
        )

    # -- helpers -------------------------------------------------------------

    def _dependency_text(self, config_files: list[str]) -> str:
        chunks: list[str] = []
        for f in config_files:
            if PurePosixPath(f).name in ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
                                         "package.json") or "requirements" in f:
                chunks.append(self._read(f, 50_000).lower())
        return "\n".join(chunks)

    def _pyproject(self) -> dict[str, object]:
        text = self._read("pyproject.toml", 200_000)
        if not text:
            return {}
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {}

    def _detect_tooling(self, files: list[str], deps_text: str) -> tuple[list[str], list[str]]:
        names = {PurePosixPath(f).name for f in files}
        tool = self._pyproject().get("tool", {})
        tool = tool if isinstance(tool, dict) else {}
        has_py_tests = any(
            PurePosixPath(f).suffix == ".py"
            and (PurePosixPath(f).name.startswith("test_") or PurePosixPath(f).name.endswith(
                "_test.py") or f.startswith("tests/"))
            for f in files
        )
        test_frameworks: list[str] = []
        commands: list[str] = []
        if has_py_tests or "pytest" in tool or "pytest.ini" in names or "conftest.py" in names or (
            "pytest" in deps_text
        ):
            test_frameworks.append("pytest")
            commands.append("pytest")
        if "ruff" in tool or "ruff.toml" in names or ".ruff.toml" in names:
            commands.extend(["ruff_check", "ruff_format_check"])
        if "mypy" in tool or "mypy.ini" in names or ".mypy.ini" in names:
            commands.append("mypy")
        return test_frameworks, commands

    def _instruction_docs(self, files: list[str]) -> list[InstructionDoc]:
        docs = []
        for name in INSTRUCTION_DOCS:
            if name in files:
                docs.append(InstructionDoc(path=name, excerpt=self._read(name, 4000)))
        return docs

    @staticmethod
    def _tree(files: list[str], max_lines: int = 80) -> str:
        lines: list[str] = []
        seen_dirs: set[str] = set()
        for f in files:
            parts = PurePosixPath(f).parts
            for depth in range(1, len(parts)):
                d = "/".join(parts[:depth])
                if d not in seen_dirs:
                    seen_dirs.add(d)
                    lines.append("  " * (depth - 1) + parts[depth - 1] + "/")
            lines.append("  " * (len(parts) - 1) + parts[-1])
            if len(lines) >= max_lines:
                lines.append(f"… ({len(files)} files total)")
                break
        return "\n".join(lines)

    def _relevant(self, files: list[str], task_text: str) -> list[RelevantFile]:
        """Cheap lexical relevance: task tokens vs. path and content tokens.
        Source files and their tests get a small prior."""
        query = _tokens(task_text)
        scored: list[RelevantFile] = []
        for f in files:
            p = PurePosixPath(f)
            if p.suffix not in LANGUAGE_BY_EXT and p.name not in CONFIG_FILES:
                continue
            path_hits = query & _tokens(f.replace("/", " ").replace(".", " "))
            content = self._read(f, 30_000)
            content_hits = query & _tokens(content)
            score = 3.0 * len(path_hits) + 1.0 * len(content_hits)
            if p.suffix == ".py":
                score += 0.5
            if score <= 0.5:
                continue
            reason_bits = []
            if path_hits:
                reason_bits.append("path matches " + ", ".join(sorted(path_hits)))
            if content_hits:
                reason_bits.append("content mentions " + ", ".join(sorted(content_hits)[:6]))
            scored.append(RelevantFile(path=f, score=round(score, 2),
                                       reason="; ".join(reason_bits) or "source file"))
        scored.sort(key=lambda r: (-r.score, r.path))
        return scored[: self.max_relevant]
