import pytest

from engineer.models.run import TERMINAL_STATES, EngineeringRun, RunState
from engineer.orchestrator.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    can_transition,
    transition,
)
from engineer.persistence.store import Store

S = RunState

HAPPY_PATH = [
    S.ANALYZING, S.PLANNING, S.PREPARING_WORKSPACE, S.IMPLEMENTING, S.VALIDATING,
    S.DEBUGGING, S.IMPLEMENTING, S.VALIDATING, S.REVIEWING, S.READY_FOR_PR,
    S.PR_CREATED, S.WAITING_FOR_HUMAN, S.COMPLETED,
]


def test_every_state_has_a_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(RunState)


def test_happy_path_transitions_succeed_and_are_recorded():
    run = EngineeringRun(task_id="t")
    for dst in HAPPY_PATH:
        transition(run, dst, f"to {dst}")
    assert run.state == S.COMPLETED
    assert [h.to_state for h in run.history] == HAPPY_PATH
    assert run.history[0].from_state == S.PENDING


def test_approval_path():
    run = EngineeringRun(task_id="t")
    for dst in (S.ANALYZING, S.PLANNING, S.WAITING_FOR_APPROVAL, S.PREPARING_WORKSPACE):
        transition(run, dst)
    assert run.state == S.PREPARING_WORKSPACE


@pytest.mark.parametrize("src,dst", [
    (S.PENDING, S.IMPLEMENTING),          # skipping analysis/planning
    (S.PLANNING, S.IMPLEMENTING),         # skipping workspace preparation
    (S.IMPLEMENTING, S.REVIEWING),        # skipping validation
    (S.VALIDATING, S.READY_FOR_PR),       # skipping review
    (S.IMPLEMENTING, S.PR_CREATED),
    (S.WAITING_FOR_HUMAN, S.IMPLEMENTING),
    (S.READY_FOR_PR, S.COMPLETED),        # only a human completes a run
])
def test_invalid_transitions_raise(src, dst):
    run = EngineeringRun(task_id="t", state=src)
    with pytest.raises(InvalidTransition):
        transition(run, dst)
    assert run.state == src and not run.history


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_terminal_states_are_final(terminal):
    for dst in RunState:
        assert not can_transition(terminal, dst)


def test_failure_exit_records_reason():
    run = EngineeringRun(task_id="t", state=S.VALIDATING)
    transition(run, S.FAILED, "tests kept failing")
    assert run.failure_reason == "tests kept failing"


def test_restart_recovery_preserves_state_and_history(tmp_path):
    from engineer.models.task import EngineeringTask

    db = tmp_path / "e.db"
    store = Store(db)
    task = EngineeringTask(title="t", description="d", repository="/x")
    store.save_task(task)
    run = EngineeringRun(task_id=task.id, iteration=2)
    for dst in (S.ANALYZING, S.PLANNING, S.PREPARING_WORKSPACE, S.IMPLEMENTING):
        transition(run, dst, "r")
        store.save_run(run)
    run.usage.tool_calls = 7
    store.save_run(run)
    store.close()

    # Simulate a process restart.
    reopened = Store(db)
    loaded = reopened.get_run(run.id)
    assert loaded is not None
    assert loaded.state == S.IMPLEMENTING
    assert loaded.iteration == 2
    assert loaded.usage.tool_calls == 7
    assert [h.to_state for h in loaded.history] == [h.to_state for h in run.history]
    # State changes are also stored append-only, exactly once each.
    assert [c.to_state for c in reopened.state_changes(run.id)] == [
        S.ANALYZING, S.PLANNING, S.PREPARING_WORKSPACE, S.IMPLEMENTING]
    # The recovered run continues to obey the transition table.
    with pytest.raises(InvalidTransition):
        transition(loaded, S.COMPLETED)
    transition(loaded, S.VALIDATING)
    reopened.close()
