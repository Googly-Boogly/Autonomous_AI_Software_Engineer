"""Validation results. Produced only by the deterministic validation engine."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CheckKind(StrEnum):
    TEST = "test"
    LINT = "lint"
    FORMAT = "format"
    TYPECHECK = "typecheck"
    SECURITY = "security"


class ValidationResult(BaseModel):
    command_id: str
    command: list[str]
    kind: CheckKind
    required: bool
    start_time: datetime
    end_time: datetime
    exit_code: int | None  # None => did not complete (timeout / could not start)
    stdout_tail: str
    stderr_tail: str
    timed_out: bool = False
    passed: bool

    @property
    def duration_seconds(self) -> float:
        return (self.end_time - self.start_time).total_seconds()


class ValidationSummary(BaseModel):
    iteration: int
    results: list[ValidationResult] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only if at least one required check ran and every required check passed."""
        required = [r for r in self.results if r.required]
        return bool(required) and all(r.passed for r in required)

    def failures(self) -> list[ValidationResult]:
        return [r for r in self.results if not r.passed]

    def one_line(self) -> str:
        parts = [f"{r.command_id}={'pass' if r.passed else 'FAIL'}" for r in self.results]
        return ", ".join(parts) or "no checks run"
