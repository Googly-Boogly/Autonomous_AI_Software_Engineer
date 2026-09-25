"""Planning agent: read-only exploration, then a structured EngineeringPlan.

The planner has no write tools and no execution tools. Its only output is a
plan submitted through the `submit_plan` tool, validated by Pydantic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from engineer.agents.common import (
    UNTRUSTED_CONTENT_NOTE,
    UsageHook,
    render_context,
    render_task,
)
from engineer.models.plan import EngineeringPlan
from engineer.models.repo_context import RepositoryContext
from engineer.models.task import EngineeringTask
from engineer.providers.base import ModelProvider, ToolResultMessage
from engineer.tools.toolbox import Toolbox, ToolOutcome

SYSTEM_PROMPT = f"""You are the planning stage of a bounded autonomous software engineering system.

Your job: understand a well-scoped engineering task and the repository, then submit a concrete, \
minimal implementation plan by calling the `submit_plan` tool exactly once.

You can only read the repository (read_file, list_directory, search_code). You cannot write \
files or run commands; a separate implementation stage will do that, and deterministic \
software validates everything.

Guidelines:
- Read the files you intend to change and their existing tests before planning.
- Prefer the smallest change that satisfies the task. Preserve existing architecture and style.
- Always plan tests that prove the change (regression tests for bug fixes).
- `commands_expected_to_run` must contain only validation command ids listed in the context.
- State assumptions and any uncertainty honestly. If the task is ambiguous, unsafe, or \
under-specified, say so in `uncertainty`.
- Paths are repository-relative.

{UNTRUSTED_CONTENT_NOTE}"""


class PlanningFailed(Exception):
    pass


class SubmitPlanArgs(EngineeringPlan):
    """The submit_plan tool's arguments are exactly an EngineeringPlan."""


def register_submit_plan(toolbox: Toolbox, sink: Callable[[EngineeringPlan], None]) -> None:
    """Add the submit_plan tool. Arguments are schema-validated by the Toolbox; a
    valid plan is passed to ``sink``. Invalid plans are returned to the model as errors."""

    def submit(args: Any) -> ToolOutcome:
        plan = EngineeringPlan.model_validate(args.model_dump())
        sink(plan)
        return ToolOutcome("Plan accepted. Stop now.", payload=plan)

    toolbox.add("submit_plan",
                "Submit the final implementation plan. Call exactly once, when ready.",
                SubmitPlanArgs, submit)


class PlannerAgent:
    def __init__(self, provider: ModelProvider, toolbox: Toolbox, *, max_turns: int,
                 on_turn: UsageHook) -> None:
        self.provider = provider
        self.toolbox = toolbox
        self.max_turns = max_turns
        self.on_turn = on_turn
        self._plan: EngineeringPlan | None = None
        register_submit_plan(toolbox, self._accept)

    def _accept(self, plan: EngineeringPlan) -> None:
        self._plan = plan

    def plan(self, task: EngineeringTask, ctx: RepositoryContext,
             file_excerpts: dict[str, str]) -> EngineeringPlan:
        session = self.provider.start_session(role="planner", system=SYSTEM_PROMPT,
                                              tools=self.toolbox.specs())
        excerpts = "\n\n".join(f"### {p}\n```\n{t}\n```" for p, t in file_excerpts.items())
        prompt = (f"{render_task(task)}\n\n{render_context(ctx)}\n\n"
                  f"## Excerpts of likely relevant files\n{excerpts or '(none)'}\n\n"
                  "Explore as needed, then call submit_plan.")

        turn = session.send(user_text=prompt)
        for _ in range(self.max_turns):
            self.on_turn("planner", turn)
            if not turn.tool_calls:
                turn = session.send(user_text="Please call the submit_plan tool with your plan.")
                continue
            results: list[ToolResultMessage] = []
            for call in turn.tool_calls:
                outcome = self.toolbox.execute(call)
                results.append(ToolResultMessage(call_id=call.id, content=outcome.content,
                                                 is_error=outcome.is_error))
            if self._plan is not None:
                return self._plan
            turn = session.send(tool_results=results)
        self.on_turn("planner", turn)
        raise PlanningFailed(f"planner did not submit a valid plan within {self.max_turns} turns")
