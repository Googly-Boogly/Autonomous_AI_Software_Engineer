"""The Claude Code backend, driven by a fake Claude Code.

The fake stands in for the `claude` process: for each scripted tool request it asks
OUR PreToolUse hook for a decision exactly as Claude Code would, then (only if
allowed) performs the built-in file operation or calls OUR in-process MCP tool
handler. So the gate, the MCP bridge, the options we pass, and the orchestrator
integration are all exercised for real; only the model is scripted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402

from engineer.agents.claude_code import ClaudeCodeBackend  # noqa: E402
from engineer.git import cli  # noqa: E402
from engineer.models.run import RunState  # noqa: E402
from engineer.models.task import EngineeringTask  # noqa: E402
from engineer.models.tools import PolicyDecision  # noqa: E402
from engineer.orchestrator.orchestrator import Orchestrator  # noqa: E402

S = RunState
MCP = "mcp__engineer__"

PLAN = {
    "task_summary": "Fix subtract() and add a regression test.",
    "implementation_steps": ["Return a - b in subtract", "Add tests/test_subtract.py"],
    "files_expected_to_change": ["calculator/ops.py"],
    "tests_to_add_or_modify": ["tests/test_subtract.py"],
    "commands_expected_to_run": ["pytest"],
}
FIXED_SUBTRACT = ("def subtract(a: float, b: float) -> float:\n    return a + b",
                  "def subtract(a: float, b: float) -> float:\n    return a - b")
TEST_FILE = ("from calculator import subtract\n\n\ndef test_subtract():\n"
             "    assert subtract(5, 3) == 2\n")


class FakeClaudeCode:
    """Replays one list of (tool_name, tool_input) steps per session."""

    def __init__(self, sessions: list[list[tuple[str, dict[str, Any]]]], *, cost: float = 0.02,
                 subtype: str = "success", is_error: bool = False,
                 raise_exc: Exception | None = None) -> None:
        self.sessions = list(sessions)
        self.cost, self.subtype, self.is_error, self.raise_exc = cost, subtype, is_error, raise_exc
        self.options: list[Any] = []
        self.decisions: list[tuple[str, str]] = []
        self.tool_results: list[tuple[str, dict[str, Any]]] = []
        self.prompts: list[str] = []

    def __call__(self, options: Any, sdk_tools: list[Any]) -> _FakeSession:
        self.options.append(options)
        steps = self.sessions.pop(0) if self.sessions else []
        return _FakeSession(self, options, sdk_tools, steps)


class _FakeSession:
    def __init__(self, fake: FakeClaudeCode, options: Any, sdk_tools: list[Any],
                 steps: list[tuple[str, dict[str, Any]]]) -> None:
        self.fake, self.options, self.steps = fake, options, steps
        self.tools = {f"{MCP}{t.name}": t for t in sdk_tools}

    async def __aenter__(self) -> _FakeSession:
        if self.fake.raise_exc:
            raise self.fake.raise_exc
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.fake.prompts.append(prompt)

    async def receive_response(self) -> Any:
        hook = self.options.hooks["PreToolUse"][0].hooks[0]
        cwd = Path(self.options.cwd)
        for name, raw in self.steps:
            tool_input = {k: (v.format(cwd=cwd) if isinstance(v, str) else v)
                          for k, v in raw.items()}
            out = await hook({"hook_event_name": "PreToolUse", "tool_name": name,
                              "tool_input": tool_input}, None, None)
            decision = out["hookSpecificOutput"]["permissionDecision"]
            self.fake.decisions.append((name, decision))
            if decision != "allow":
                continue
            if name in self.tools:
                self.fake.tool_results.append((name, await self.tools[name].handler(tool_input)))
            elif name == "Write":
                p = Path(tool_input["file_path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(tool_input["content"])
            elif name == "Edit":
                p = Path(tool_input["file_path"])
                p.write_text(p.read_text().replace(tool_input["old_string"],
                                                   tool_input["new_string"], 1))
        yield AssistantMessage(content=[TextBlock(text="Done: fixed subtract and added a test.")],
                               model="fake")
        yield ResultMessage(subtype=self.fake.subtype, duration_ms=1, duration_api_ms=1,
                            is_error=self.fake.is_error, num_turns=len(self.steps),
                            session_id="fake", total_cost_usd=self.fake.cost,
                            usage={"input_tokens": 100, "output_tokens": 20},
                            result="Fixed subtract and added a regression test.")


def _planner_steps(extra=()):
    return [*extra, (f"{MCP}read_file", {"path": "calculator/ops.py"}),
            (f"{MCP}submit_plan", PLAN)]


def _good_engineer_steps(extra=()):
    return [
        *extra,
        ("Read", {"file_path": "{cwd}/calculator/ops.py"}),
        ("Edit", {"file_path": "{cwd}/calculator/ops.py", "old_string": FIXED_SUBTRACT[0],
                  "new_string": FIXED_SUBTRACT[1]}),
        ("Write", {"file_path": "{cwd}/tests/test_subtract.py", "content": TEST_FILE}),
        (f"{MCP}run_check", {"command_id": "pytest"}),
    ]


def _run(repo, settings, store, fake, **task_kwargs):
    backend = ClaudeCodeBackend(client_factory=fake, max_budget_usd=2.0)
    task = EngineeringTask(title="Fix subtraction", description="Fix subtraction and add a "
                           "regression test.", repository=str(repo), **task_kwargs)
    return Orchestrator(settings, backend, store).execute(task)


def test_claude_code_completes_task_under_policy(make_repo, settings, store, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "parent-session-secret")
    repo = make_repo("calculator")
    head = cli.head_commit(repo)
    attacks = [
        ("Bash", {"command": "cat ~/.ssh/id_rsa"}),
        ("Read", {"file_path": "{cwd}/.env"}),
        ("Write", {"file_path": str(repo / "calculator" / "ops.py"), "content": "pwned"}),
        ("WebFetch", {"url": "https://evil.example"}),
        (f"{MCP}write_file", {"path": "x.py", "content": "x"}),
        ("mcp__claude_ai_Gmail__send_email", {"to": "x@example.com"}),
    ]
    fake = FakeClaudeCode([_planner_steps(), _good_engineer_steps(attacks)])
    report = _run(repo, settings, store, fake)

    assert report.final_state == S.WAITING_FOR_HUMAN, report.failure_reason
    assert report.files_changed == ["calculator/ops.py", "tests/test_subtract.py"]
    assert report.final_validation.passed
    assert "return a - b" in (Path(report.worktree_path) / "calculator/ops.py").read_text()
    # Every attack was denied by our hook; the legitimate calls were allowed.
    engineer_decisions = fake.decisions[len(_planner_steps()):]
    assert engineer_decisions[: len(attacks)] == [(name, "deny") for name, _ in attacks]
    assert all(d == "allow" for _, d in engineer_decisions[len(attacks):])
    assert report.policy_denials == len(attacks)
    # The operator's repo — targeted directly by one attack — is untouched.
    assert cli.head_commit(repo) == head and cli.status_porcelain(repo) == []
    assert "pwned" not in (repo / "calculator" / "ops.py").read_text()
    # Denials and built-in calls are in the audit trail.
    actions = store.tool_actions(report.run_id)
    denied = {a.tool for a in actions if a.decision == PolicyDecision.DENY}
    assert {"Bash", "Read", "Write", "WebFetch"} <= denied
    # Usage from Claude Code's result messages is accounted (planner + engineer).
    assert report.usage.model_calls == 2
    assert report.usage.cost_usd == pytest.approx(0.04)


def test_claude_code_session_options_are_locked_down(make_repo, settings, store, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "parent-session-secret")
    repo = make_repo("calculator")
    fake = FakeClaudeCode([_planner_steps(), _good_engineer_steps()])
    report = _run(repo, settings, store, fake)
    planner, engineer = fake.options
    for opts in (planner, engineer):
        assert opts.setting_sources == []          # no user/project settings, hooks, plugins
        assert opts.strict_mcp_config is True      # no operator MCP servers
        assert opts.allowed_tools == []            # nothing auto-approved; the hook decides
        assert opts.permission_mode == "default"
        assert "Bash" in opts.disallowed_tools and "WebFetch" in opts.disallowed_tools
        assert opts.env["CLAUDE_CODE_MESSAGING_TOKEN"] == ""  # parent session not inherited
        assert int(opts.env["MCP_TOOL_TIMEOUT"]) > settings.command_timeout_seconds * 1000
        assert list(opts.mcp_servers) == ["engineer"]
        assert opts.system_prompt["preset"] == "claude_code"
    assert planner.tools == []                      # planner: our read-only MCP tools only
    assert Path(planner.cwd) != repo and not Path(planner.cwd).exists()  # temp dir, removed
    assert set(engineer.tools) == {"Read", "Write", "Edit", "MultiEdit", "TodoWrite"}
    assert engineer.cwd == report.worktree_path


def test_planner_mcp_tools_read_base_commit_only(make_repo, settings, store):
    repo = make_repo("calculator")
    (repo / "uncommitted_secret_plan.txt").write_text("operator's private notes")
    fake = FakeClaudeCode([
        _planner_steps([(f"{MCP}read_file", {"path": "uncommitted_secret_plan.txt"}),
                        (f"{MCP}read_file", {"path": ".env"})]),
        _good_engineer_steps(),
    ])
    report = _run(repo, settings, store, fake)
    assert report.final_state == S.WAITING_FOR_HUMAN
    results = [r for n, r in fake.tool_results if n == f"{MCP}read_file"]
    assert results[0]["is_error"] and "not found" in results[0]["content"][0]["text"].lower()
    assert results[1]["is_error"] and "DENIED" in results[1]["content"][0]["text"]
    assert "private notes" not in repr(fake.tool_results)


def test_claude_code_repair_loop_gets_feedback(make_repo, settings, store):
    broken = [("Write", {"file_path": "{cwd}/tests/test_subtract.py", "content": TEST_FILE})]
    fake = FakeClaudeCode([_planner_steps(), broken, _good_engineer_steps()])
    report = _run(make_repo("calculator"), settings, store, fake)
    assert report.final_state == S.WAITING_FOR_HUMAN
    assert report.iterations_used == 2
    assert "Feedback from independent validation" in fake.prompts[2]
    assert "assert -" in fake.prompts[2] or "assert 8 == 2" in fake.prompts[2]


def test_claude_code_claiming_success_does_not_count(make_repo, settings, store):
    lazy = [("Write", {"file_path": "{cwd}/tests/test_subtract.py", "content": TEST_FILE})]
    fake = FakeClaudeCode([_planner_steps(), lazy, lazy])
    report = _run(make_repo("calculator"), settings, store, fake, max_iterations=2)
    assert report.final_state == S.FAILED  # its "Done!" message is not evidence


def test_tool_call_limit_enforced_by_hook(make_repo, settings, store):
    s = settings.model_copy(update={"max_tool_calls_per_iteration": 3})
    fake = FakeClaudeCode([_planner_steps(), _good_engineer_steps()])
    backend = ClaudeCodeBackend(client_factory=fake)
    task = EngineeringTask(title="t", description="d", repository=str(make_repo("calculator")),
                           max_iterations=1)
    Orchestrator(s, backend, store).execute(task)
    engineer_decisions = [d for _, d in fake.decisions[2:]]
    assert engineer_decisions == ["allow", "allow", "allow", "deny"]


def test_claude_code_not_available_fails_safely(make_repo, settings, store):
    fake = FakeClaudeCode([], raise_exc=RuntimeError("claude CLI not found"))
    report = _run(make_repo("calculator"), settings, store, fake)
    assert report.final_state == S.FAILED
    assert "Claude Code session failed" in report.failure_reason
    assert report.branch is None


def test_claude_code_cost_counts_against_budget(make_repo, settings, store):
    fake = FakeClaudeCode([_planner_steps(), _good_engineer_steps()], cost=5.0)
    report = _run(make_repo("calculator"), settings, store, fake, max_model_cost_usd=1.0)
    assert report.final_state == S.BUDGET_EXCEEDED
