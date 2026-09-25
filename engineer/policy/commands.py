"""Allowlisted command registry.

Models refer to commands only by ID. The argv for each ID is fixed by this
module and by deterministic repository analysis; there is no path by which a
model-supplied string becomes part of a command line.
"""

from __future__ import annotations

from dataclasses import dataclass

from engineer.models.validation import CheckKind


@dataclass(frozen=True)
class CommandSpec:
    id: str
    argv: tuple[str, ...]
    kind: CheckKind
    required: bool
    description: str


class UnknownCommand(Exception):
    pass


def builtin_commands(python: str) -> dict[str, CommandSpec]:
    """All commands the system knows how to run. ``python`` is the interpreter
    used to invoke tools, so they run in a known environment."""
    specs = [
        CommandSpec(
            "pytest",
            (python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no"),
            CheckKind.TEST,
            required=True,
            description="Run the repository's pytest suite.",
        ),
        CommandSpec(
            "ruff_check",
            (python, "-m", "ruff", "check", "--no-cache", "."),
            CheckKind.LINT,
            required=True,
            description="Run Ruff lint checks.",
        ),
        CommandSpec(
            "ruff_format_check",
            (python, "-m", "ruff", "format", "--check", "--no-cache", "."),
            CheckKind.FORMAT,
            required=False,
            description="Check formatting with Ruff (advisory).",
        ),
        CommandSpec(
            "mypy",
            (python, "-m", "mypy", "--no-incremental", "--cache-dir=/dev/null", "."),
            CheckKind.TYPECHECK,
            required=True,
            description="Run mypy type checks.",
        ),
    ]
    return {s.id: s for s in specs}


class CommandRegistry:
    def __init__(self, commands: dict[str, CommandSpec]) -> None:
        self._commands = dict(commands)

    @classmethod
    def for_repository(cls, python: str, command_ids: list[str]) -> CommandRegistry:
        known = builtin_commands(python)
        return cls({cid: known[cid] for cid in command_ids if cid in known})

    def get(self, command_id: str) -> CommandSpec:
        if not isinstance(command_id, str) or command_id not in self._commands:
            raise UnknownCommand(
                f"unknown command id {command_id!r}; allowed: {sorted(self._commands)}"
            )
        return self._commands[command_id]

    def ids(self) -> list[str]:
        return list(self._commands)

    def specs(self) -> list[CommandSpec]:
        return list(self._commands.values())
