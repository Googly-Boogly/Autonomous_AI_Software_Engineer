"""Provider-neutral model interface.

Agents own their prompts and tool loops; providers only translate a
conversation into a vendor API and back. A session owns its native message
history (append-only), so provider-specific details such as thinking blocks
never leak into agent code.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultMessage(BaseModel):
    call_id: str
    content: str
    is_error: bool = False


class TurnUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ModelTurn(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: TurnUsage = Field(default_factory=TurnUsage)
    model: str = ""


class ProviderError(Exception):
    """A model call failed. ``retryable`` hints whether the caller may retry."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ModelSession(Protocol):
    def send(
        self,
        *,
        user_text: str | None = None,
        tool_results: list[ToolResultMessage] | None = None,
    ) -> ModelTurn: ...


class ModelProvider(Protocol):
    name: str

    def start_session(self, *, role: str, system: str, tools: list[ToolSpec]) -> ModelSession:
        """``role`` identifies the agent ('planner', 'engineer') for routing/logging."""
        ...
