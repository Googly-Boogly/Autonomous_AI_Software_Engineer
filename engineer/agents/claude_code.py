"""Claude Code backend: Claude Code writes the code, deterministic Python decides.

Claude Code (via the Claude Agent SDK) runs the planner and engineer agent loops. It
is boxed in as follows:

* **Planner.** It gets no built-in tools at all. It can only call our MCP tools
  (read_file / list_directory / search_code over the base commit, plus submit_plan).
  Its cwd is an empty temp directory, so it never sees the operator's working tree.
* **Engineer.** cwd is the isolated worktree. It may use Claude Code's native
  Read / Write / Edit / MultiEdit, and our MCP tools (search_code, list_directory,
  git_status, git_diff, run_check). There is no Bash, web, or sub-agent access.
* **Every** tool request passes a PreToolUse hook that calls the deterministic gate
  (engineer/policy/claude_code_gate.py): deny-by-default, with PathPolicy on every path.
  Denials are recorded as ToolActions. A can_use_tool callback denies anything that
  would otherwise prompt for permission.
* No user/project settings are loaded (setting_sources=[]), so a hostile repo cannot
  inject hooks, permissions, or MCP servers through .claude/settings.json.
* Tool-call and cost ceilings apply. Validation still runs independently afterwards,
  and only its result counts.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import anyio

from engineer.agents.common import (
    UNTRUSTED_CONTENT_NOTE,
    UsageHook,
    render_context,
    render_task,
)
from engineer.agents.engineer import IterationOutcome
from engineer.agents.planner import PlanningFailed, register_submit_plan
from engineer.models.plan import EngineeringPlan
from engineer.models.repo_context import RepositoryContext
from engineer.models.task import EngineeringTask
from engineer.models.tools import PolicyDecision, ToolAction
from engineer.policy.claude_code_gate import MCP_PREFIX, MCP_SERVER_NAME, gate_tool_request
from engineer.providers.base import ModelTurn, ProviderError, ToolCall, TurnUsage
from engineer.tools.toolbox import Toolbox

ENGINEER_MCP_TOOLS = frozenset(
    {"search_code", "list_directory", "git_status", "git_diff", "run_check"})
PLANNER_MCP_TOOLS = frozenset({"read_file", "list_directory", "search_code", "submit_plan"})
# Belt and braces: also tell Claude Code these must never be offered.
DISALLOWED_BUILTINS = [
    "Bash", "BashOutput", "KillShell", "KillBash", "WebFetch", "WebSearch", "Task", "Agent",
    "NotebookEdit", "Glob", "Grep", "LS", "Skill", "SlashCommand", "ExitPlanMode",
]
# Variables of an enclosing Claude Code session (e.g. when launched from a Claude Code
# terminal) must not leak into ours. The SDK itself drops CLAUDECODE and sets
# CLAUDE_CODE_ENTRYPOINT, so that one is left alone.
_STRIP_ENV_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_PID", "CLAUDE_EFFORT")
_KEEP_ENV = frozenset({"CLAUDE_CODE_ENTRYPOINT"})

ENGINEER_RULES = f"""
You are running as the implementation stage of a bounded autonomous software engineering \
system, inside an isolated git worktree (your working directory) on a feature branch.

Available tools: Read, Write, Edit (and MultiEdit) for files in the working directory, plus \
mcp__{MCP_SERVER_NAME}__search_code, __list_directory, __git_status, __git_diff, and \
__run_check (allowlisted validation commands by id). There is no shell, network, or \
sub-agent access. Requests outside policy are denied; don't retry them.

How to work:
1. Read the relevant files before editing them.
2. Make the smallest reasonable change that satisfies the task and plan. Preserve existing \
architecture, naming, and style. No unrelated edits.
3. Add or update tests that prove the change. Never weaken, skip, or delete existing tests.
4. Run the checks with run_check and fix failures you caused.
5. Finish with a short factual summary of what you changed. Validation is re-run \
independently after you stop, and only its result counts. You cannot commit, push, or merge.

{UNTRUSTED_CONTENT_NOTE}
"""

PLANNER_RULES = f"""
You are the planning stage of a bounded autonomous software engineering system. You can \
only read the repository through mcp__{MCP_SERVER_NAME}__read_file, __list_directory and \
__search_code. You cannot write files or run commands.

Understand the task and the repository, then call mcp__{MCP_SERVER_NAME}__submit_plan \
exactly once with a concrete, minimal plan. Prefer the smallest change; always plan tests. \
`commands_expected_to_run` may only contain the validation command ids listed in the \
context. State assumptions and uncertainty honestly. After the plan is accepted, stop.

{UNTRUSTED_CONTENT_NOTE}
"""


def _child_env(tool_timeout_s: float) -> dict[str, str]:
    """Environment overrides for the Claude Code subprocess (the SDK merges these over
    os.environ, so inherited session variables are blanked rather than removed)."""
    env = {k: "" for k in os.environ
           if k.startswith(_STRIP_ENV_PREFIXES) and k not in _KEEP_ENV}
    # run_check may run a test suite for minutes; don't let the MCP call time out first.
    env["MCP_TOOL_TIMEOUT"] = str(int(tool_timeout_s * 1000))
    return env


@dataclass
class _SessionStats:
    tool_calls: int = 0
    denied: int = 0
    texts: list[str] = field(default_factory=list)
    result: Any = None


class ClaudeCodeBackend:
    name = "claude-code"

    def __init__(self, *, model: str | None = None, max_budget_usd: float | None = None,
                 cli_path: str | None = None, tool_timeout_s: float = 360.0,
                 client_factory: ClientFactory | None = None) -> None:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ProviderError(
                "the 'claude-agent-sdk' package is not installed: "
                "pip install -e '.[claude-code]'") from exc
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.cli_path = cli_path
        self.tool_timeout_s = tool_timeout_s
        # (options, mcp_tools) -> async context manager with query()/receive_response().
        # Defaults to the real ClaudeSDKClient; tests inject a fake Claude Code.
        self._client_factory = client_factory or _default_client

    def planner(self, toolbox: Toolbox, *, max_turns: int, on_turn: UsageHook) -> ClaudeCodePlanner:
        return ClaudeCodePlanner(self, toolbox, max_turns=max_turns, on_turn=on_turn)

    def engineer(self, toolbox: Toolbox, *, max_tool_calls: int,
                 on_turn: UsageHook) -> ClaudeCodeEngineer:
        return ClaudeCodeEngineer(self, toolbox, max_tool_calls=max_tool_calls, on_turn=on_turn)

    # ------------------------------------------------------------------ session

    def run_session(
        self,
        *,
        role: str,
        prompt: str,
        rules: str,
        cwd: str,
        toolbox: Toolbox,
        mcp_tools: frozenset[str],
        allow_builtin_file_tools: bool,
        max_tool_calls: int,
        on_turn: UsageHook,
    ) -> _SessionStats:
        """Run one Claude Code session to completion under our gate."""
        stats = _SessionStats()
        try:
            anyio.run(self._session, role, prompt, rules, cwd, toolbox, mcp_tools,
                      allow_builtin_file_tools, max_tool_calls, stats)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Claude Code session failed: {type(exc).__name__}: {exc}") from exc
        finally:
            if stats.result is not None:
                on_turn(role, _to_turn(stats))
        if stats.result is None:
            raise ProviderError("Claude Code ended without a result message")
        return stats

    async def _session(self, role: str, prompt: str, rules: str, cwd: str, toolbox: Toolbox,
                       mcp_tools: frozenset[str], allow_builtin_file_tools: bool,
                       max_tool_calls: int, stats: _SessionStats) -> None:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            HookMatcher,
            PermissionResultDeny,
            ResultMessage,
            TextBlock,
            create_sdk_mcp_server,
        )

        policy = toolbox.write_policy or toolbox.view.policy
        sdk_tools = mcp_tools_for(toolbox, mcp_tools)

        async def pre_tool_use(input_data: dict[str, Any], tool_use_id: str | None,
                               context: Any) -> dict[str, Any]:
            name = str(input_data.get("tool_name", ""))
            tool_input = input_data.get("tool_input") or {}
            decision = gate_tool_request(name, tool_input, policy=policy,
                                         allowed_mcp_tools=mcp_tools,
                                         allow_builtin_file_tools=allow_builtin_file_tools)
            reason = decision.reason
            if decision.allowed and stats.tool_calls >= max_tool_calls:
                decision, reason = decision.__class__(False), (
                    f"tool call limit ({max_tool_calls}) reached for this iteration; "
                    "summarize and stop")
            if decision.allowed:
                stats.tool_calls += 1
            else:
                stats.denied += 1
            # MCP calls are recorded by the Toolbox itself when they execute.
            if not (decision.allowed and name.startswith(MCP_PREFIX)):
                toolbox.recorder(ToolAction(
                    run_id=toolbox.run_id, iteration=toolbox.iteration, agent=role,
                    tool=name, arguments=_summarize(tool_input),
                    decision=PolicyDecision.ALLOW if decision.allowed else PolicyDecision.DENY,
                    decision_reason=reason, ok=decision.allowed,
                    result_summary="executed by Claude Code" if decision.allowed else reason,
                ))
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if decision.allowed else "deny",
                "permissionDecisionReason": reason or "permitted by policy",
            }}

        async def deny_everything_else(tool_name: str, tool_input: dict[str, Any],
                                       context: Any) -> Any:
            return PermissionResultDeny(message=f"{tool_name} is not permitted", interrupt=False)

        options = ClaudeAgentOptions(
            cwd=cwd,
            system_prompt={"type": "preset", "preset": "claude_code", "append": rules},
            mcp_servers={MCP_SERVER_NAME: create_sdk_mcp_server(
                name=MCP_SERVER_NAME, version="1.0.0", tools=sdk_tools)},
            strict_mcp_config=True,  # only our server; never the operator's own MCP servers
            tools=["Read", "Write", "Edit", "MultiEdit", "TodoWrite"]
            if allow_builtin_file_tools else [],
            allowed_tools=[],
            disallowed_tools=DISALLOWED_BUILTINS,
            # The SDK types hook inputs as a TypedDict union; we read PreToolUse fields only.
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[cast(Any, pre_tool_use)])]},
            can_use_tool=deny_everything_else,
            permission_mode="default",
            setting_sources=[],
            max_turns=max_tool_calls + 5,
            max_budget_usd=self.max_budget_usd,
            model=self.model,
            env=_child_env(self.tool_timeout_s),
            cli_path=self.cli_path,
        )
        async with self._client_factory(options, sdk_tools) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            stats.texts.append(block.text.strip())
                elif isinstance(message, ResultMessage):
                    stats.result = message


class ClaudeCodePlanner:
    def __init__(self, backend: ClaudeCodeBackend, toolbox: Toolbox, *, max_turns: int,
                 on_turn: UsageHook) -> None:
        self.backend = backend
        self.toolbox = toolbox
        self.max_turns = max_turns
        self.on_turn = on_turn
        self._plans: list[EngineeringPlan] = []
        register_submit_plan(toolbox, self._plans.append)

    def plan(self, task: EngineeringTask, ctx: RepositoryContext,
             file_excerpts: dict[str, str]) -> EngineeringPlan:
        excerpts = "\n\n".join(f"### {p}\n```\n{t}\n```" for p, t in file_excerpts.items())
        prompt = (f"{render_task(task)}\n\n{render_context(ctx)}\n\n"
                  f"## Excerpts of likely relevant files\n{excerpts or '(none)'}\n\n"
                  "Explore as needed, then call submit_plan.")
        # An empty scratch cwd: the planner has no built-in file access anyway.
        with tempfile.TemporaryDirectory(prefix="engineer-planner-") as scratch:
            self.backend.run_session(
                role="planner", prompt=prompt, rules=PLANNER_RULES, cwd=scratch,
                toolbox=self.toolbox, mcp_tools=PLANNER_MCP_TOOLS,
                allow_builtin_file_tools=False, max_tool_calls=self.max_turns * 3,
                on_turn=self.on_turn)
        if not self._plans:
            raise PlanningFailed("Claude Code did not submit a valid plan")
        return self._plans[-1]


class ClaudeCodeEngineer:
    def __init__(self, backend: ClaudeCodeBackend, toolbox: Toolbox, *, max_tool_calls: int,
                 on_turn: UsageHook) -> None:
        self.backend = backend
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
        ids = ", ".join(self.toolbox.registry.ids()) if self.toolbox.registry else "none"
        parts = [
            render_task(task), "", render_context(ctx), "",
            "## Approved plan", "```json", plan.model_dump_json(indent=2), "```", "",
            f"Validation command ids for run_check: {ids}.",
            f"This is iteration {iteration} of at most {max_iterations}. "
            f"You may make at most {self.max_tool_calls} tool calls in this iteration.",
        ]
        if feedback:
            parts += ["", "## Feedback from independent validation of the previous iteration",
                      "The working directory still contains your previous changes. Fix these "
                      "problems with targeted edits:", "", feedback]
        if self.toolbox.write_policy is None:
            raise ProviderError("engineer toolbox has no workspace")
        stats = self.backend.run_session(
            role="engineer", prompt="\n".join(parts), rules=ENGINEER_RULES,
            cwd=str(self.toolbox.write_policy.root), toolbox=self.toolbox,
            mcp_tools=ENGINEER_MCP_TOOLS, allow_builtin_file_tools=True,
            max_tool_calls=self.max_tool_calls, on_turn=self.on_turn)
        result = stats.result
        subtype = str(getattr(result, "subtype", ""))
        summary = (getattr(result, "result", None) or (stats.texts[-1] if stats.texts else "")
                   or "(no summary)")
        return IterationOutcome(
            summary=summary.strip()[:2000],
            finished=not getattr(result, "is_error", False),
            tool_calls=stats.tool_calls,
            hit_tool_limit="max_turns" in subtype or stats.tool_calls >= self.max_tool_calls,
            denied_calls=stats.denied,
            notes=stats.texts[-5:],
        )


# ---------------------------------------------------------------- helpers


ClientFactory = Callable[[Any, list[Any]], Any]


def _default_client(options: Any, sdk_tools: list[Any]) -> Any:
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options=options)


def mcp_tools_for(toolbox: Toolbox, names: frozenset[str]) -> list[Any]:
    """Expose selected Toolbox tools to Claude Code as in-process MCP tools.
    Execution (and its policy checks and recording) stays in the Toolbox."""
    from claude_agent_sdk import tool

    specs = [s for s in toolbox.specs() if s.name in names]
    counter = [0]

    def make(spec_name: str) -> Any:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            counter[0] += 1
            call = ToolCall(id=f"cc_{counter[0]}", name=spec_name, arguments=dict(args))
            # Toolbox calls may run tests for minutes: keep them off the event loop.
            outcome = await anyio.to_thread.run_sync(toolbox.execute, call)
            return {"content": [{"type": "text", "text": outcome.content}],
                    "is_error": outcome.is_error}
        return handler

    return [tool(s.name, s.description, s.input_schema)(make(s.name)) for s in specs]


def _summarize(tool_input: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in tool_input.items():
        out[k] = f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 300 else v
    return out


def _to_turn(stats: _SessionStats) -> ModelTurn:
    r = stats.result
    usage = getattr(r, "usage", None) or {}
    inp = int(usage.get("input_tokens", 0) or 0) + int(
        usage.get("cache_read_input_tokens", 0) or 0) + int(
        usage.get("cache_creation_input_tokens", 0) or 0)
    return ModelTurn(
        text=(getattr(r, "result", None) or "")[:2000],
        stop_reason=str(getattr(r, "subtype", "")),
        usage=TurnUsage(input_tokens=inp, output_tokens=int(usage.get("output_tokens", 0) or 0),
                        cost_usd=float(getattr(r, "total_cost_usd", 0.0) or 0.0)),
        model="claude-code",
    )
