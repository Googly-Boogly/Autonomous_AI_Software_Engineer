"""Agent backends: who runs the planner / engineer loops.

* ProviderBackend — our own tool loop over a ModelProvider (Anthropic API, scripted).
* ClaudeCodeBackend (agents/claude_code.py) — Claude Code runs the loop, gated by our
  policy through hooks and permission callbacks.

Whichever backend runs, the orchestrator still owns state, workspaces, validation and
the decision about whether work is done.
"""

from __future__ import annotations

from typing import Protocol

from engineer.agents.common import UsageHook
from engineer.agents.engineer import EngineerAgent, IterationOutcome
from engineer.agents.planner import PlannerAgent
from engineer.models.plan import EngineeringPlan
from engineer.models.repo_context import RepositoryContext
from engineer.models.task import EngineeringTask
from engineer.providers.base import ModelProvider
from engineer.tools.toolbox import Toolbox


class Planner(Protocol):
    def plan(self, task: EngineeringTask, ctx: RepositoryContext,
             file_excerpts: dict[str, str]) -> EngineeringPlan: ...


class Engineer(Protocol):
    def run_iteration(
        self,
        task: EngineeringTask,
        plan: EngineeringPlan,
        ctx: RepositoryContext,
        *,
        iteration: int,
        max_iterations: int,
        feedback: str | None,
    ) -> IterationOutcome: ...


class AgentBackend(Protocol):
    name: str

    def planner(self, toolbox: Toolbox, *, max_turns: int, on_turn: UsageHook) -> Planner: ...

    def engineer(self, toolbox: Toolbox, *, max_tool_calls: int,
                 on_turn: UsageHook) -> Engineer: ...


class ProviderBackend:
    """Runs our own planner/engineer tool loops on a ModelProvider."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider
        self.name = provider.name

    def planner(self, toolbox: Toolbox, *, max_turns: int, on_turn: UsageHook) -> Planner:
        return PlannerAgent(self.provider, toolbox, max_turns=max_turns, on_turn=on_turn)

    def engineer(self, toolbox: Toolbox, *, max_tool_calls: int,
                 on_turn: UsageHook) -> Engineer:
        return EngineerAgent(self.provider, toolbox, max_tool_calls=max_tool_calls,
                             on_turn=on_turn)
