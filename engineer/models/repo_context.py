"""RepositoryContext: deterministic facts about a repository, produced before any model call."""

from __future__ import annotations

from pydantic import BaseModel, Field


class InstructionDoc(BaseModel):
    path: str
    excerpt: str  # truncated content


class RelevantFile(BaseModel):
    path: str
    score: float
    reason: str


class RepositoryContext(BaseModel):
    root: str
    head_commit: str
    current_branch: str | None
    languages: dict[str, int] = Field(
        default_factory=dict, description="language -> number of tracked files"
    )
    frameworks: list[str] = Field(default_factory=list)
    test_frameworks: list[str] = Field(default_factory=list)
    directory_tree: str = ""
    total_files: int = 0
    config_files: list[str] = Field(default_factory=list)
    ci_workflows: list[str] = Field(default_factory=list)
    instruction_docs: list[InstructionDoc] = Field(default_factory=list)
    available_commands: list[str] = Field(
        default_factory=list, description="IDs of registry commands applicable to this repo"
    )
    relevant_files: list[RelevantFile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
