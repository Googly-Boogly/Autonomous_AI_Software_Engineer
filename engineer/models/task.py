"""EngineeringTask: the normalized unit of work, regardless of where it came from."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TaskSource(StrEnum):
    LOCAL = "local"
    GITHUB_ISSUE = "github_issue"


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    ABANDONED = "abandoned"


class EngineeringTask(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20_000)
    acceptance_criteria: list[str] = Field(default_factory=list)
    repository: str
    source: TaskSource = TaskSource.LOCAL
    source_id: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    requested_by: str = "local-operator"
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    security_review_required: bool = False
    max_iterations: int = Field(default=5, ge=1, le=20)
    max_runtime_minutes: int = Field(default=30, ge=1, le=240)
    max_model_cost_usd: float = Field(default=2.0, ge=0.0, le=100.0)
    status: TaskStatus = TaskStatus.OPEN
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("title")
    @classmethod
    def _single_line_title(cls, v: str) -> str:
        return " ".join(v.split())

    def slug(self, max_len: int = 40) -> str:
        """A git-ref-safe slug derived from the title."""
        s = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        if len(s) > max_len:
            cut = s[: max_len + 1]
            s = cut.rsplit("-", 1)[0] if "-" in cut else s[:max_len]
        return s.strip("-") or "task"
