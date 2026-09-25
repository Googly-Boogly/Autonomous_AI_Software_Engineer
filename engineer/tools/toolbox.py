"""Narrow, typed tools exposed to agents.

Each agent gets a Toolbox built with exactly the capabilities it needs:
the planner gets read-only tools over a CommitView; the engineer gets
read/write tools over its isolated worktree plus allowlisted checks.

Every call is (1) validated against the tool's schema, (2) checked by
deterministic policy, (3) executed, and (4) recorded as a ToolAction whether
it was allowed or denied. There is no shell tool and no code-eval tool.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engineer.git import cli
from engineer.git.views import FileTooLarge, RepoView
from engineer.git.worktree import diff_paths, pending_changes
from engineer.models.tools import PolicyDecision, ToolAction
from engineer.policy.commands import CommandRegistry, UnknownCommand
from engineer.policy.paths import PathDenied, PathPolicy
from engineer.providers.base import ToolCall, ToolSpec
from engineer.validation.engine import ValidationEngine

# ---------------------------------------------------------------- arguments


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileArgs(_Args):
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ListDirectoryArgs(_Args):
    path: str = "."


class SearchCodeArgs(_Args):
    query: str = Field(min_length=2, max_length=200)
    path_prefix: str | None = None


class WriteFileArgs(_Args):
    path: str
    content: str


class ReplaceInFileArgs(_Args):
    path: str
    old: str = Field(min_length=1)
    new: str


class NoArgs(_Args):
    pass


class RunCheckArgs(_Args):
    command_id: str


class FinishArgs(_Args):
    summary: str = Field(min_length=1, max_length=4000)


# ---------------------------------------------------------------- execution


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    denied: bool = False
    finished: bool = False
    finish_summary: str = ""
    payload: Any = None  # e.g. a validated plan


class ToolDenied(Exception):
    pass


Handler = Callable[[Any], ToolOutcome]


@dataclass
class _Tool:
    spec: ToolSpec
    args_model: type[BaseModel]
    handler: Handler


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


class Toolbox:
    def __init__(
        self,
        *,
        agent: str,
        view: RepoView,
        max_file_bytes: int,
        recorder: Callable[[ToolAction], None],
        run_id: str,
        write_policy: PathPolicy | None = None,
        registry: CommandRegistry | None = None,
        validator: ValidationEngine | None = None,
        output_tail_chars: int = 4000,
    ) -> None:
        self.agent = agent
        self.view = view
        self.max_file_bytes = max_file_bytes
        self.recorder = recorder
        self.run_id = run_id
        self.iteration = 0
        self.write_policy = write_policy
        self.registry = registry
        self.validator = validator
        self.output_tail_chars = output_tail_chars
        self._tools: dict[str, _Tool] = {}
        self._register_read_tools()

    # -- registration --------------------------------------------------------

    def add(self, name: str, description: str, args_model: type[BaseModel],
            handler: Handler) -> None:
        self._tools[name] = _Tool(ToolSpec(name=name, description=description,
                                           input_schema=_schema(args_model)), args_model, handler)

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def _register_read_tools(self) -> None:
        self.add("read_file",
                 "Read a UTF-8 text file by repository-relative path. Optionally restrict to "
                 "a 1-based inclusive line range.", ReadFileArgs, self._read_file)
        self.add("list_directory",
                 "List files and subdirectories directly under a repository-relative directory.",
                 ListDirectoryArgs, self._list_directory)
        self.add("search_code",
                 "Case-insensitive literal substring search across repository files. Returns "
                 "up to 50 'path:line: text' matches.", SearchCodeArgs, self._search_code)

    def enable_engineering_tools(self) -> None:
        if self.write_policy is None or self.registry is None or self.validator is None:
            raise RuntimeError("engineering tools need a write policy, registry and validator")
        self.add("write_file",
                 "Create or overwrite a text file in the workspace with the full given content.",
                 WriteFileArgs, self._write_file)
        self.add("replace_in_file",
                 "Replace exactly one occurrence of `old` with `new` in a file. Fails if `old` "
                 "is missing or occurs more than once; include more context to disambiguate.",
                 ReplaceInFileArgs, self._replace_in_file)
        self.add("git_status", "List files changed in the workspace.", NoArgs, self._git_status)
        self.add("git_diff", "Show the diff of all changes made so far in the workspace.",
                 NoArgs, self._git_diff)
        ids = ", ".join(self.registry.ids())
        self.add("run_check",
                 f"Run one allowlisted validation command by id. Available ids: {ids}.",
                 RunCheckArgs, self._run_check)
        self.add("finish",
                 "Call when you believe the task is complete (validation will be run "
                 "independently) or you cannot make further progress. Summarize what you did.",
                 FinishArgs, self._finish)

    # -- dispatch ------------------------------------------------------------

    def execute(self, call: ToolCall) -> ToolOutcome:
        started = time.monotonic()
        tool = self._tools.get(call.name)
        decision = PolicyDecision.ALLOW
        reason = ""
        if tool is None:
            decision, reason = PolicyDecision.DENY, f"tool {call.name!r} is not available"
            outcome = ToolOutcome(f"DENIED: {reason}. Available tools: {self.names()}",
                                  is_error=True, denied=True)
        else:
            try:
                args = tool.args_model.model_validate(call.arguments)
            except ValidationError as exc:
                outcome = ToolOutcome(f"Invalid arguments for {call.name}: "
                                      f"{exc.errors(include_url=False)}", is_error=True)
            else:
                try:
                    outcome = tool.handler(args)
                except (PathDenied, UnknownCommand, ToolDenied) as exc:
                    decision, reason = PolicyDecision.DENY, str(exc)
                    outcome = ToolOutcome(f"DENIED by policy: {exc}", is_error=True, denied=True)
                except FileNotFoundError as exc:
                    outcome = ToolOutcome(f"File not found: {exc}", is_error=True)
                except FileTooLarge as exc:
                    outcome = ToolOutcome(str(exc), is_error=True)
                except (OSError, UnicodeError, cli.GitError) as exc:
                    outcome = ToolOutcome(f"{type(exc).__name__}: {exc}", is_error=True)

        self.recorder(ToolAction(
            run_id=self.run_id, iteration=self.iteration, agent=self.agent, tool=call.name,
            arguments=_redact_args(call.arguments), decision=decision, decision_reason=reason,
            ok=not outcome.is_error, result_summary=outcome.content[:300],
            duration_ms=int((time.monotonic() - started) * 1000),
        ))
        return outcome

    # -- read handlers -------------------------------------------------------

    def _read_file(self, a: ReadFileArgs) -> ToolOutcome:
        text = self.view.read_text(a.path, self.max_file_bytes)
        if a.start_line or a.end_line:
            lines = text.splitlines(keepends=True)
            start = (a.start_line or 1) - 1
            end = a.end_line or len(lines)
            text = "".join(lines[start:end])
        return ToolOutcome(text if text else "(empty file)")

    def _list_directory(self, a: ListDirectoryArgs) -> ToolOutcome:
        self.view.policy.resolve(a.path)
        prefix = PurePosixPath(a.path.strip()).as_posix()
        prefix = "" if prefix in (".", "") else prefix.rstrip("/") + "/"
        entries: set[str] = set()
        for f in self.view.list_files():
            if f.startswith(prefix):
                rest = f[len(prefix):]
                head, sep, _ = rest.partition("/")
                entries.add(head + ("/" if sep else ""))
        if not entries:
            return ToolOutcome(f"No readable entries under {a.path!r}", is_error=True)
        return ToolOutcome("\n".join(sorted(entries)))

    def _search_code(self, a: SearchCodeArgs) -> ToolOutcome:
        needle = a.query.lower()
        prefix = (a.path_prefix or "").strip().strip("/")
        hits: list[str] = []
        for f in self.view.list_files():
            if prefix and not (f == prefix or f.startswith(prefix + "/")):
                continue
            try:
                text = self.view.read_text(f, self.max_file_bytes)
            except (FileTooLarge, FileNotFoundError, PathDenied, OSError):
                continue
            if "\x00" in text[:1024]:
                continue  # binary
            for no, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(f"{f}:{no}: {line.strip()[:200]}")
                    if len(hits) >= 50:
                        return ToolOutcome("\n".join(hits) + "\n(truncated at 50 matches)")
        return ToolOutcome("\n".join(hits) if hits else "No matches.")

    # -- write / exec handlers ----------------------------------------------

    def _require_write_policy(self) -> PathPolicy:
        if self.write_policy is None:
            raise ToolDenied("this agent has no write capability")
        return self.write_policy

    def _checked_write_target(self, path: str, content: str) -> Any:
        target = self._require_write_policy().resolve(path, for_write=True)
        if len(content.encode("utf-8")) > self.max_file_bytes:
            raise ToolDenied(f"content exceeds the {self.max_file_bytes}-byte file limit")
        if target.exists() and not target.is_file():
            raise ToolDenied("target exists and is not a regular file")
        if target.is_symlink():
            raise ToolDenied("refusing to write through a symlink")
        return target

    def _write_file(self, a: WriteFileArgs) -> ToolOutcome:
        target = self._checked_write_target(a.path, a.content)
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(a.content, encoding="utf-8")
        verb = "Updated" if existed else "Created"
        return ToolOutcome(f"{verb} {a.path} ({len(a.content.splitlines())} lines)")

    def _replace_in_file(self, a: ReplaceInFileArgs) -> ToolOutcome:
        target = self._checked_write_target(a.path, a.new)
        if not target.is_file():
            raise FileNotFoundError(a.path)
        text = target.read_text(encoding="utf-8")
        count = text.count(a.old)
        if count != 1:
            return ToolOutcome(
                f"`old` occurs {count} times in {a.path}; it must occur exactly once. "
                "Read the file and include more surrounding context.", is_error=True)
        new_text = text.replace(a.old, a.new, 1)
        if len(new_text.encode("utf-8")) > self.max_file_bytes:
            raise ToolDenied(f"result exceeds the {self.max_file_bytes}-byte file limit")
        target.write_text(new_text, encoding="utf-8")
        return ToolOutcome(f"Replaced 1 occurrence in {a.path}")

    def _worktree(self) -> Any:
        return self._require_write_policy().root

    def _visible_changes(self) -> list[str]:
        """Changed paths, minus anything policy hides (e.g. a .env created by a test run)."""
        policy = self._require_write_policy()
        return [f for f in pending_changes(policy.root, "HEAD") if policy.is_readable(f)]

    def _git_status(self, _: NoArgs) -> ToolOutcome:
        files = self._visible_changes()
        return ToolOutcome("\n".join(files) if files else "No changes.")

    def _git_diff(self, _: NoArgs) -> ToolOutcome:
        out = diff_paths(self._worktree(), "HEAD", self._visible_changes())
        limit = self.output_tail_chars * 4
        return ToolOutcome(out[:limit] + ("\n…[diff truncated]" if len(out) > limit else "")
                           if out else "No changes.")

    def _run_check(self, a: RunCheckArgs) -> ToolOutcome:
        if self.registry is None or self.validator is None:
            raise ToolDenied("this agent cannot run commands")
        spec = self.registry.get(a.command_id)
        r = self.validator.run_one(spec)
        status = "PASSED" if r.passed else ("TIMED OUT" if r.timed_out else
                                            f"FAILED (exit code {r.exit_code})")
        return ToolOutcome(f"{spec.id}: {status}\n--- stdout ---\n{r.stdout_tail}\n"
                           f"--- stderr ---\n{r.stderr_tail}", is_error=not r.passed)

    def _finish(self, a: FinishArgs) -> ToolOutcome:
        return ToolOutcome("Iteration finished; independent validation will now run.",
                           finished=True, finish_summary=a.summary)


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Keep audit records small: file contents are summarized, not stored."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 300:
            out[k] = f"<{len(v)} chars>"
        else:
            out[k] = v
    return out
