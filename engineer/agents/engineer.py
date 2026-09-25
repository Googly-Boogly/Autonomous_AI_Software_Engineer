"""Implementation agent: performs ONE bounded engineering iteration.

The orchestrator decides how many iterations happen and what counts as
success. This agent's `finish` call only ends its own iteration; it cannot
mark work as done, approved, or passing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
from engineer.tools.toolbox import Toolbox

SYSTEM_PROMPT = f"""You are the implementation stage of a bounded autonomous software engineering \
system. You work inside an isolated git worktree on a feature branch.

Tools: read_file, list_directory, search_code, write_file, replace_in_file, git_status, \
git_diff, run_check (allowlisted validation commands by id), finish.

How to work:
1. Read the relevant files before editing them.
2. Make the smallest reasonable change that satisfies the task and plan. Preserve existing \
architecture, naming, and style. No drive-by refactors or unrelated edits.
3. Add or update tests that prove the change.
4. Run the relevant checks with run_check and fix failures you caused.
5. Call finish with a short factual summary when the acceptance criteria appear satisfied, or \
when you cannot make progress (explain why).

Rules:
- Never weaken, skip, or delete existing tests to make checks pass.
- Paths are repository-relative. There is no shell; you cannot install packages, use the \
network, commit, push, or merge. Validation is re-run independently after you finish, and \
only its result counts.

{UNTRUSTED_CONTENT_NOTE}"""


@dataclass
class IterationOutcome:
    summary: str = ""
    finished: bool = False
    tool_calls: int = 0
    hit_tool_limit: bool = False
    denied_calls: int = 0
    notes: list[str] = field(default_factory=list)


class EngineerAgent:
    def __init__(self, provider: ModelProvider, toolbox: Toolbox, *,
                 max_tool_calls: int, on_turn: UsageHook) -> None:
        self.provider = provider
        self.toolbox = toolbox
        self.max_tool_calls = max_tool_calls
        self.on_turn = on_turn

    def run_iteration(
        self,
        task: EngineeringTask,
        plan: EngineeringPlan,
        ctx: RepositoryContext,
        *,
        iteration: int,
        max_iterations: int,
        feedback: str | None,
    ) -> IterationOutcome:
        self.toolbox.iteration = iteration
        session = self.provider.start_session(role="engineer", system=SYSTEM_PROMPT,
                                              tools=self.toolbox.specs())
        prompt = [
            render_task(task),
            "",
            render_context(ctx),
            "",
            "## Approved plan",
            "```json",
            plan.model_dump_json(indent=2),
            "```",
            "",
            f"This is iteration {iteration} of at most {max_iterations}. "
            f"You may make at most {self.max_tool_calls} tool calls in this iteration.",
        ]
        if feedback:
            prompt += ["", "## Feedback from independent validation of the previous iteration",
                       "The workspace still contains your previous changes. Fix these problems "
                       "with targeted edits:", "", feedback]
        else:
            prompt += ["", "Begin by reading the files named in the plan."]

        out = IterationOutcome()
        turn = session.send(user_text="\n".join(prompt))
        while True:
            self.on_turn("engineer", turn)
            if turn.text.strip():
                out.notes.append(turn.text.strip()[:500])
            if not turn.tool_calls:
                out.summary = turn.text.strip()[:2000] or "(no summary; model stopped)"
                return out
            results: list[ToolResultMessage] = []
            for call in turn.tool_calls:
                if out.tool_calls >= self.max_tool_calls:
                    out.hit_tool_limit = True
                    out.summary = out.summary or "Stopped: per-iteration tool call limit reached."
                    return out
                out.tool_calls += 1
                outcome = self.toolbox.execute(call)
                out.denied_calls += int(outcome.denied)
                if outcome.finished:
                    out.finished = True
                    out.summary = outcome.finish_summary
                results.append(ToolResultMessage(call_id=call.id, content=outcome.content,
                                                 is_error=outcome.is_error))
            if out.finished:
                return out
            turn = session.send(tool_results=results)
