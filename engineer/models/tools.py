"""Tool call records. Every tool invocation proposed by a model is recorded,
including those that policy denied."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from engineer.models.task import new_id, utcnow


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class ToolAction(BaseModel):
    id: str = Field(default_factory=lambda: new_id("act"))
    run_id: str
    iteration: int
    agent: str  # "planner" | "engineer"
    tool: str
    arguments: dict[str, Any]
    decision: PolicyDecision
    decision_reason: str = ""
    ok: bool = False
    result_summary: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    duration_ms: int = 0
