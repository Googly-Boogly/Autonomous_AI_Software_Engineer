"""Engineering run record and the lifecycle state enumeration.

The transition table itself lives in ``engineer.orchestrator.state_machine``;
this module only defines data.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from engineer.models.task import new_id, utcnow


class RunState(StrEnum):
    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    PREPARING_WORKSPACE = "PREPARING_WORKSPACE"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    DEBUGGING = "DEBUGGING"
    REVIEWING = "REVIEWING"
    READY_FOR_PR = "READY_FOR_PR"
    PR_CREATED = "PR_CREATED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    COMPLETED = "COMPLETED"
    # Failure / terminal states
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


TERMINAL_STATES: frozenset[RunState] = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.BLOCKED,
     RunState.BUDGET_EXCEEDED}
)


class StateChange(BaseModel):
    from_state: RunState
    to_state: RunState
    at: datetime = Field(default_factory=utcnow)
    reason: str = ""


class UsageTotals(BaseModel):
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0


class EngineeringRun(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    task_id: str
    state: RunState = RunState.PENDING
    iteration: int = 0
    max_iterations: int = 5
    branch: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    worktree_path: str | None = None
    failure_reason: str | None = None
    usage: UsageTotals = Field(default_factory=UsageTotals)
    history: list[StateChange] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
