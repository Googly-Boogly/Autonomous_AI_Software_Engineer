"""The run lifecycle transition table. Only Python code calls ``transition``;
no model output is ever mapped directly to a state."""

from __future__ import annotations

from engineer.models.run import TERMINAL_STATES, EngineeringRun, RunState, StateChange

S = RunState

_FAILURE_EXITS = {S.FAILED, S.CANCELLED, S.BLOCKED, S.BUDGET_EXCEEDED}

ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    S.PENDING: frozenset({S.ANALYZING, *_FAILURE_EXITS}),
    S.ANALYZING: frozenset({S.PLANNING, *_FAILURE_EXITS}),
    S.PLANNING: frozenset({S.WAITING_FOR_APPROVAL, S.PREPARING_WORKSPACE, *_FAILURE_EXITS}),
    S.WAITING_FOR_APPROVAL: frozenset({S.PREPARING_WORKSPACE, *_FAILURE_EXITS}),
    S.PREPARING_WORKSPACE: frozenset({S.IMPLEMENTING, *_FAILURE_EXITS}),
    S.IMPLEMENTING: frozenset({S.VALIDATING, *_FAILURE_EXITS}),
    S.VALIDATING: frozenset({S.DEBUGGING, S.REVIEWING, *_FAILURE_EXITS}),
    S.DEBUGGING: frozenset({S.IMPLEMENTING, *_FAILURE_EXITS}),
    # Reviewing can send work back through the bounded implementation loop.
    S.REVIEWING: frozenset({S.READY_FOR_PR, S.IMPLEMENTING, *_FAILURE_EXITS}),
    # Local mode has no PR: READY_FOR_PR hands straight to the human.
    S.READY_FOR_PR: frozenset({S.PR_CREATED, S.WAITING_FOR_HUMAN, *_FAILURE_EXITS}),
    S.PR_CREATED: frozenset({S.WAITING_FOR_HUMAN, *_FAILURE_EXITS}),
    # Only a human action moves a run out of WAITING_FOR_HUMAN.
    S.WAITING_FOR_HUMAN: frozenset({S.COMPLETED, S.CANCELLED}),
    **{t: frozenset() for t in TERMINAL_STATES},
}


class InvalidTransition(Exception):
    pass


def can_transition(src: RunState, dst: RunState) -> bool:
    return dst in ALLOWED_TRANSITIONS[src]


def transition(run: EngineeringRun, dst: RunState, reason: str = "") -> StateChange:
    src = run.state
    if not can_transition(src, dst):
        raise InvalidTransition(f"{src} -> {dst} is not an allowed transition")
    change = StateChange(from_state=src, to_state=dst, reason=reason)
    run.state = dst
    run.history.append(change)
    run.updated_at = change.at
    if dst in _FAILURE_EXITS and reason:
        run.failure_reason = reason
    return change
