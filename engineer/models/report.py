"""Final run report handed back to the human."""

from __future__ import annotations

from pydantic import BaseModel, Field

from engineer.models.plan import EngineeringPlan
from engineer.models.run import RunState, UsageTotals
from engineer.models.validation import ValidationSummary


class DiffCheck(BaseModel):
    """Deterministic checks on the final diff (Phase 1 stand-in for the review agent)."""

    name: str
    passed: bool
    detail: str = ""
    blocking: bool = True


class RunReport(BaseModel):
    run_id: str
    task_id: str
    task_title: str
    final_state: RunState
    failure_reason: str | None = None
    branch: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    worktree_path: str | None = None
    iterations_used: int
    max_iterations: int
    plan: EngineeringPlan | None = None
    files_changed: list[str] = Field(default_factory=list)
    diff_stat: str = ""
    final_validation: ValidationSummary | None = None
    diff_checks: list[DiffCheck] = Field(default_factory=list)
    engineer_summaries: list[str] = Field(default_factory=list)
    usage: UsageTotals = Field(default_factory=UsageTotals)
    policy_denials: int = 0
    known_limitations: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
