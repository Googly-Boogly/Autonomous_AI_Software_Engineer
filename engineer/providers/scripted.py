"""Deterministic scripted provider.

Replays pre-written model turns. Used by the test suite and for offline
demonstrations of the orchestration flow. It contains no intelligence: every
file edit it makes is written in the script. It exists to prove that the
*deterministic* parts of the system (policy, worktrees, validation, the
repair loop, reporting) behave correctly regardless of what a model says.

Script format (JSON or Python dict)::

    {
      "planner":  [turn, turn, ...],            # one session
      "engineer": [[turn, ...], [turn, ...]]    # one list per iteration/session
    }

    turn = {"text": "...", "tool_calls": [{"name": "write_file", "arguments": {...}}]}

A turn may also be a callable ``(tool_results) -> ModelTurn`` in Python tests.
When a session's script is exhausted it returns an empty end_turn.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from engineer.providers.base import (
    ModelTurn,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    TurnUsage,
)

TurnLike = dict[str, Any] | ModelTurn | Callable[[list[ToolResultMessage]], ModelTurn]


def _to_turn(raw: TurnLike, received: list[ToolResultMessage], counter: list[int]) -> ModelTurn:
    if callable(raw) and not isinstance(raw, dict | ModelTurn):
        turn = raw(received)
    elif isinstance(raw, ModelTurn):
        turn = raw.model_copy(deep=True)
    else:
        calls = []
        for c in raw.get("tool_calls", []):
            counter[0] += 1
            calls.append(ToolCall(id=c.get("id") or f"call_{counter[0]}", name=c["name"],
                                  arguments=c.get("arguments", {})))
        turn = ModelTurn(text=raw.get("text", ""), tool_calls=calls,
                         stop_reason="tool_use" if calls else "end_turn")
    turn.usage = turn.usage or TurnUsage()
    turn.model = turn.model or "scripted"
    return turn


class ScriptedSession:
    def __init__(self, role: str, turns: list[TurnLike], counter: list[int],
                 tools: list[ToolSpec]) -> None:
        self.role = role
        self.tool_names = [t.name for t in tools]
        self._turns = list(turns)
        self._counter = counter
        self.received: list[dict[str, Any]] = []

    def send(
        self,
        *,
        user_text: str | None = None,
        tool_results: list[ToolResultMessage] | None = None,
    ) -> ModelTurn:
        results = tool_results or []
        self.received.append({"user_text": user_text, "tool_results": results})
        if not self._turns:
            return ModelTurn(text="(script exhausted)", model="scripted")
        return _to_turn(self._turns.pop(0), results, self._counter)


class ScriptedProvider:
    name = "scripted"

    def __init__(self, script: dict[str, Any]) -> None:
        self._planner: list[TurnLike] = list(script.get("planner", []))
        self._engineer: list[list[TurnLike]] = [list(s) for s in script.get("engineer", [])]
        self._counter = [0]
        self.sessions: list[ScriptedSession] = []

    @classmethod
    def from_file(cls, path: Path) -> ScriptedProvider:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def start_session(self, *, role: str, system: str, tools: list[ToolSpec]) -> ScriptedSession:
        if role == "planner":
            turns, self._planner = self._planner, []
        else:
            turns = self._engineer.pop(0) if self._engineer else []
        session = ScriptedSession(role, turns, self._counter, tools)
        self.sessions.append(session)
        return session
