"""End-to-end runs against fixture repositories with a scripted provider.

The scripted provider has no intelligence; these tests verify that the
deterministic machinery (worktrees, policy, validation, repair loop, diff
checks, persistence, reporting) produces correct outcomes for both good and
bad model behaviour.
"""

import json
import sys
from pathlib import Path

import pytest

from engineer.git import cli
from engineer.models.run import RunState
from engineer.process import run_process
from engineer.providers.base import ModelTurn, ProviderError, TurnUsage
from tests.conftest import load_script

S = RunState


def _plan_turn(**overrides):
    args = {
        "task_summary": "s", "implementation_steps": ["do it"],
        "files_expected_to_change": ["calculator/ops.py"],
        "tests_to_add_or_modify": ["tests/test_subtract.py"],
        "commands_expected_to_run": ["pytest"],
    }
    args.update(overrides)
    return {"tool_calls": [{"name": "submit_plan", "arguments": args}]}


def _call(name, **arguments):
    return {"name": name, "arguments": arguments}


def _pytest_in(path):
    return run_process([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], path,
                       timeout=120)


# ------------------------------------------------------------ fixture tasks


@pytest.mark.parametrize("fixture,task,expected_files,iterations", [
    ("calculator", "Fix subtraction and add regression test.",
     ["calculator/ops.py", "tests/test_subtract.py"], 1),
    ("sample-fastapi", "Add GET /health returning {status: 'ok'}.",
     ["app/main.py", "tests/test_health.py"], 2),
    ("inventory", "Add validation preventing negative quantities.",
     ["inventory/stock.py", "tests/test_negative_quantities.py"], 1),
])
def test_fixture_tasks_complete(make_repo, run_task, store, fixture, task, expected_files,
                                iterations):
    repo = make_repo(fixture)
    head_before = cli.head_commit(repo)
    report, _ = run_task(repo, load_script(fixture), description=task)

    assert report.final_state == S.WAITING_FOR_HUMAN, report.failure_reason
    assert report.iterations_used == iterations
    assert report.files_changed == expected_files
    assert report.final_validation is not None and report.final_validation.passed
    assert all(c.passed for c in report.diff_checks if c.blocking)

    # Independent verification: the committed branch really passes its tests.
    wt = Path(report.worktree_path)
    assert cli.status_porcelain(wt) == []
    assert _pytest_in(wt).exit_code == 0
    # Primary repository untouched.
    assert cli.head_commit(repo) == head_before
    assert cli.current_branch(repo) == "main"
    assert cli.status_porcelain(repo) == []
    assert cli.branch_exists(repo, report.branch)

    # Persistence + artifacts.
    run = store.get_run(report.run_id)
    assert run.state == S.WAITING_FOR_HUMAN
    assert store.latest_plan(report.run_id) is not None
    assert len(store.validations(report.run_id)) == iterations
    assert store.model_call_count(report.run_id) == report.usage.model_calls > 0
    artifacts = wt.parent / "artifacts"
    for name in ("report.md", "report.json", "plan.json", "diff.patch",
                 "repository_context.json", "state_history.json"):
        assert (artifacts / name).is_file(), name
    assert "requires human review before merge" in (artifacts / "report.md").read_text()


def test_calculator_bug_actually_fixed(make_repo, run_task):
    report, _ = run_task(make_repo("calculator"), load_script("calculator"))
    ops = Path(report.worktree_path) / "calculator" / "ops.py"
    assert "return a - b" in ops.read_text()


def test_repair_loop_receives_validation_feedback(make_repo, run_task):
    report, provider = run_task(make_repo("sample-fastapi"), load_script("sample-fastapi"))
    assert report.final_state == S.WAITING_FOR_HUMAN
    engineer_sessions = [s for s in provider.sessions if s.role == "engineer"]
    assert len(engineer_sessions) == 2
    second_prompt = engineer_sessions[1].received[0]["user_text"]
    assert "Feedback from independent validation" in second_prompt
    assert "healthy" in second_prompt  # the actual assertion failure was passed back


# ------------------------------------------------------------ failure modes


def test_iteration_limit_stops_cleanly(make_repo, run_task, settings):
    # The "model" keeps claiming success while writing a failing test.
    bad_iteration = [
        {"tool_calls": [_call("write_file", path="tests/test_subtract.py",
                              content="from calculator import subtract\n\n\n"
                                      "def test_s():\n    assert subtract(5, 3) == 2\n")]},
        {"tool_calls": [_call("finish", summary="All tests pass! The task is complete.")]},
    ]
    script = {"planner": [_plan_turn()], "engineer": [bad_iteration] * 5}
    report, provider = run_task(make_repo("calculator"), script, max_iterations=3)
    assert report.final_state == S.FAILED
    assert "after 3 iteration" in report.failure_reason
    assert report.iterations_used == 3
    assert not report.final_validation.passed
    assert len([s for s in provider.sessions if s.role == "engineer"]) == 3
    # Partial work is preserved on the branch, clearly marked.
    msg = cli.git(Path(report.worktree_path), "log", "-1", "--format=%s")
    assert msg.startswith("[INCOMPLETE]")


def test_deleting_tests_does_not_pass(make_repo, run_task):
    cheat = [
        {"tool_calls": [_call("write_file", path="tests/test_ops.py",
                              content="def test_nothing():\n    assert True\n")]},
        {"tool_calls": [_call("finish", summary="done")]},
    ]
    script = {"planner": [_plan_turn(tests_to_add_or_modify=["tests/test_ops.py"])],
              "engineer": [cheat]}
    report, _ = run_task(make_repo("calculator"), script, max_iterations=1)
    assert report.final_validation.passed  # pytest is green...
    assert report.final_state == S.FAILED  # ...but the diff check catches it
    failed = {c.name for c in report.diff_checks if not c.passed}
    assert "no_tests_removed" in failed


def test_no_changes_is_not_success(make_repo, run_task):
    script = {"planner": [_plan_turn()],
              "engineer": [[{"tool_calls": [_call("finish", summary="Nothing to do.")]}]]}
    report, _ = run_task(make_repo("calculator"), script, max_iterations=1)
    assert report.final_state == S.FAILED
    assert {c.name for c in report.diff_checks if not c.passed} >= {"non_empty_diff"}


def test_missing_tests_sent_back_then_fixed(make_repo, run_task):
    it1 = [
        {"tool_calls": [_call("replace_in_file", path="calculator/ops.py",
                              old="def subtract(a: float, b: float) -> float:\n    return a + b",
                              new="def subtract(a: float, b: float) -> float:\n    return a - b")]},
        {"tool_calls": [_call("finish", summary="fixed")]},
    ]
    it2 = [
        {"tool_calls": [_call("write_file", path="tests/test_subtract.py",
                              content="from calculator import subtract\n\n\n"
                                      "def test_s():\n    assert subtract(5, 3) == 2\n")]},
        {"tool_calls": [_call("finish", summary="added test")]},
    ]
    report, provider = run_task(make_repo("calculator"),
                                {"planner": [_plan_turn()], "engineer": [it1, it2]})
    assert report.final_state == S.WAITING_FOR_HUMAN
    assert report.iterations_used == 2
    feedback = [s for s in provider.sessions if s.role == "engineer"][1].received[0]["user_text"]
    assert "tests_added_or_modified" in feedback


def test_provider_error_fails_safely(make_repo, run_task):
    def boom(_results):
        raise ProviderError("provider outage", retryable=True)

    report, _ = run_task(make_repo("calculator"), {"planner": [boom]})
    assert report.final_state == S.FAILED
    assert "provider outage" in report.failure_reason
    assert report.branch is None  # never got as far as creating a workspace


def test_repo_without_tests_is_blocked_before_any_model_call(empty_repo, run_task):
    report, provider = run_task(empty_repo, {"planner": [_plan_turn()]})
    assert report.final_state == S.BLOCKED
    assert "no supported test command" in report.failure_reason
    assert provider.sessions == []


def test_not_a_git_repo_is_blocked(tmp_path, run_task):
    (tmp_path / "notgit").mkdir()
    report, _ = run_task(tmp_path / "notgit", {})
    assert report.final_state == S.BLOCKED
    assert "not a git repository" in report.failure_reason


def test_cost_budget_exceeded(make_repo, run_task):
    expensive = ModelTurn(text="thinking hard", usage=TurnUsage(cost_usd=5.0), model="m")
    report, _ = run_task(make_repo("calculator"), {"planner": [expensive]},
                         max_model_cost_usd=1.0)
    assert report.final_state == S.BUDGET_EXCEEDED
    assert report.branch is None


def test_plan_rejected_by_operator(make_repo, run_task, settings):
    s = settings.model_copy(update={"require_plan_approval": True})
    seen = []
    report, _ = run_task(make_repo("calculator"), load_script("calculator"),
                         settings_override=s, approver=lambda plan: seen.append(plan) or False)
    assert report.final_state == S.CANCELLED
    assert len(seen) == 1 and report.branch is None


def test_plan_approval_required_without_approver_blocks(make_repo, run_task, settings):
    s = settings.model_copy(update={"require_plan_approval": True})
    report, _ = run_task(make_repo("calculator"), load_script("calculator"), settings_override=s)
    assert report.final_state == S.BLOCKED


def test_plan_approved_by_operator(make_repo, run_task, settings):
    s = settings.model_copy(update={"require_plan_approval": True})
    report, _ = run_task(make_repo("calculator"), load_script("calculator"),
                         settings_override=s, approver=lambda plan: True)
    assert report.final_state == S.WAITING_FOR_HUMAN
    history = json.loads(
        (Path(report.worktree_path).parent / "artifacts" / "state_history.json").read_text())
    assert S.WAITING_FOR_APPROVAL in [S(h["to_state"]) for h in history]


def test_malformed_plan_is_rejected_then_corrected(make_repo, run_task):
    script = load_script("calculator")
    bad = {"tool_calls": [{"name": "submit_plan",
                           "arguments": {"task_summary": "s", "implementation_steps": [],
                                         "grant_admin": True}}]}
    script["planner"] = [bad] + script["planner"]
    report, provider = run_task(make_repo("calculator"), script)
    assert report.final_state == S.WAITING_FOR_HUMAN
    planner = provider.sessions[0]
    err = planner.received[1]["tool_results"][0]
    assert err.is_error and "Invalid arguments" in err.content


def test_planner_that_never_submits_fails(make_repo, run_task, settings):
    s = settings.model_copy(update={"max_planner_turns": 3})
    script = {"planner": [{"text": "hmm"}] * 10}
    report, _ = run_task(make_repo("calculator"), script, settings_override=s)
    assert report.final_state == S.FAILED
    assert "planning failed" in report.failure_reason


def test_tool_call_limit_per_iteration(make_repo, run_task, settings):
    s = settings.model_copy(update={"max_tool_calls_per_iteration": 3})
    spam = [{"tool_calls": [_call("read_file", path="README.md")]}] * 10
    report, _ = run_task(make_repo("calculator"), {"planner": [_plan_turn()], "engineer": [spam]},
                         settings_override=s, max_iterations=1)
    assert report.final_state == S.FAILED
    assert report.usage.tool_calls <= 3 + 1  # +1 for the planner's submit_plan


def test_denied_actions_are_recorded_and_run_continues(make_repo, run_task, store):
    script = load_script("calculator")
    script["engineer"][0] = [
        {"tool_calls": [_call("read_file", path=".env"),
                        _call("read_file", path="../../../etc/passwd")]},
    ] + script["engineer"][0]
    report, _ = run_task(make_repo("calculator"), script)
    assert report.final_state == S.WAITING_FOR_HUMAN
    assert report.policy_denials == 2
    denied = [a for a in store.tool_actions(report.run_id) if a.decision == "deny"]
    assert {a.arguments["path"] for a in denied} == {".env", "../../../etc/passwd"}
