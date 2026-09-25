"""EngineeringPlan: the planner's structured, non-executable proposal."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Complexity(StrEnum):
    TRIVIAL = "trivial"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class EngineeringPlan(BaseModel):
    """Everything here is a *proposal*. Nothing in a plan grants authority:
    listed files and commands are still checked by policy at execution time."""

    model_config = ConfigDict(extra="forbid")

    task_summary: str = Field(min_length=1, max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    files_to_inspect: list[str] = Field(default_factory=list, max_length=50)
    files_expected_to_change: list[str] = Field(default_factory=list, max_length=50)
    implementation_steps: list[str] = Field(min_length=1, max_length=30)
    tests_to_add_or_modify: list[str] = Field(default_factory=list, max_length=30)
    commands_expected_to_run: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Command IDs from the allowlisted registry, e.g. 'pytest'.",
    )
    security_considerations: list[str] = Field(default_factory=list, max_length=20)
    uncertainty: str = Field(default="", max_length=2000)
    estimated_complexity: Complexity = Complexity.SMALL
