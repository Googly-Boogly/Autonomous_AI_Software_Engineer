"""SQLite persistence with a versioned schema.

Rows keep a few indexed columns plus the full Pydantic document as JSON, so
the schema stays small and portable (the DDL uses only types PostgreSQL also
accepts). Tool actions, validation results, model calls and state changes are
append-only.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from engineer.models.plan import EngineeringPlan
from engineer.models.run import EngineeringRun, StateChange
from engineer.models.task import EngineeringTask, utcnow
from engineer.models.tools import ToolAction
from engineer.models.validation import ValidationSummary

SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE engineering_tasks (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_id TEXT,
        repository TEXT NOT NULL,
        created_at TEXT NOT NULL,
        doc TEXT NOT NULL
    );
    CREATE TABLE engineering_runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES engineering_tasks(id),
        state TEXT NOT NULL,
        iteration INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        doc TEXT NOT NULL
    );
    CREATE INDEX idx_runs_state ON engineering_runs(state);
    CREATE TABLE engineering_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES engineering_runs(id),
        created_at TEXT NOT NULL,
        doc TEXT NOT NULL
    );
    CREATE TABLE run_state_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES engineering_runs(id),
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        reason TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE TABLE tool_actions (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES engineering_runs(id),
        iteration INTEGER NOT NULL,
        agent TEXT NOT NULL,
        tool TEXT NOT NULL,
        decision TEXT NOT NULL,
        ok INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        doc TEXT NOT NULL
    );
    CREATE INDEX idx_tool_actions_run ON tool_actions(run_id);
    CREATE TABLE validation_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES engineering_runs(id),
        iteration INTEGER NOT NULL,
        passed INTEGER NOT NULL,
        doc TEXT NOT NULL
    );
    CREATE TABLE model_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES engineering_runs(id),
        agent TEXT NOT NULL,
        model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cost_usd REAL NOT NULL,
        tool_calls INTEGER NOT NULL,
        stop_reason TEXT NOT NULL,
        at TEXT NOT NULL
    );
    """,
}


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # Tool calls may be recorded from worker threads (e.g. Claude Code MCP handlers),
        # so the connection is shared across threads and every access is serialized.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, isolation_level=None,  # autocommit; explicit txns
                                     check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _read(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _migrate(self) -> None:
        self._conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{current} is newer than this code "
                               f"(v{SCHEMA_VERSION})")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            with self._tx() as c:
                for stmt in filter(str.strip, _MIGRATIONS[version].split(";")):
                    c.execute(stmt)
                c.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))

    def schema_version(self) -> int:
        return int(str(self._read("SELECT MAX(version) FROM schema_version")[0][0]))

    # -- writes --------------------------------------------------------------

    def save_task(self, task: EngineeringTask) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO engineering_tasks (id, source, source_id, repository, created_at, doc)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET doc = excluded.doc",
                (task.id, task.source.value, task.source_id, task.repository,
                 task.created_at.isoformat(), task.model_dump_json()),
            )

    def save_run(self, run: EngineeringRun) -> None:
        """Upsert the run and append any state changes not yet recorded."""
        run.updated_at = utcnow()
        with self._tx() as c:
            c.execute(
                "INSERT INTO engineering_runs (id, task_id, state, iteration, updated_at, doc)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state = excluded.state,"
                " iteration = excluded.iteration, updated_at = excluded.updated_at,"
                " doc = excluded.doc",
                (run.id, run.task_id, run.state.value, run.iteration, run.updated_at.isoformat(),
                 run.model_dump_json()),
            )
            recorded = c.execute("SELECT COUNT(*) FROM run_state_changes WHERE run_id = ?",
                                 (run.id,)).fetchone()[0]
            for ch in run.history[recorded:]:
                c.execute(
                    "INSERT INTO run_state_changes (run_id, from_state, to_state, reason, at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (run.id, ch.from_state.value, ch.to_state.value, ch.reason,
                     ch.at.isoformat()),
                )

    def save_plan(self, run_id: str, plan: EngineeringPlan) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO engineering_plans (run_id, created_at, doc) VALUES (?, ?, ?)",
                      (run_id, utcnow().isoformat(), plan.model_dump_json()))

    def record_tool_action(self, action: ToolAction) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO tool_actions (id, run_id, iteration, agent, tool, decision, ok,"
                " started_at, doc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (action.id, action.run_id, action.iteration, action.agent, action.tool,
                 action.decision.value, int(action.ok), action.started_at.isoformat(),
                 action.model_dump_json()),
            )

    def record_validation(self, run_id: str, summary: ValidationSummary) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO validation_results (run_id, iteration, passed, doc)"
                      " VALUES (?, ?, ?, ?)",
                      (run_id, summary.iteration, int(summary.passed),
                       summary.model_dump_json()))

    def record_model_call(self, run_id: str, agent: str, model: str, input_tokens: int,
                          output_tokens: int, cost_usd: float, tool_calls: int,
                          stop_reason: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO model_calls (run_id, agent, model, input_tokens, output_tokens,"
                " cost_usd, tool_calls, stop_reason, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, agent, model, input_tokens, output_tokens, cost_usd, tool_calls,
                 stop_reason, utcnow().isoformat()),
            )

    # -- reads ---------------------------------------------------------------

    def get_task(self, task_id: str) -> EngineeringTask | None:
        rows = self._read("SELECT doc FROM engineering_tasks WHERE id = ?", (task_id,))
        return EngineeringTask.model_validate_json(str(rows[0][0])) if rows else None

    def get_run(self, run_id: str) -> EngineeringRun | None:
        rows = self._read("SELECT doc FROM engineering_runs WHERE id = ?", (run_id,))
        return EngineeringRun.model_validate_json(str(rows[0][0])) if rows else None

    def list_runs(self, limit: int = 50) -> list[EngineeringRun]:
        rows = self._read("SELECT doc FROM engineering_runs ORDER BY updated_at DESC LIMIT ?",
                          (limit,))
        return [EngineeringRun.model_validate_json(str(r[0])) for r in rows]

    def latest_plan(self, run_id: str) -> EngineeringPlan | None:
        rows = self._read(
            "SELECT doc FROM engineering_plans WHERE run_id = ? ORDER BY id DESC LIMIT 1",
            (run_id,))
        return EngineeringPlan.model_validate_json(str(rows[0][0])) if rows else None

    def state_changes(self, run_id: str) -> list[StateChange]:
        rows = self._read(
            "SELECT from_state, to_state, reason, at FROM run_state_changes WHERE run_id = ?"
            " ORDER BY id", (run_id,))
        return [StateChange.model_validate(
            {"from_state": r[0], "to_state": r[1], "reason": r[2], "at": r[3]}) for r in rows]

    def tool_actions(self, run_id: str) -> list[ToolAction]:
        rows = self._read(
            "SELECT doc FROM tool_actions WHERE run_id = ? ORDER BY started_at, rowid", (run_id,))
        return [ToolAction.model_validate_json(str(r[0])) for r in rows]

    def validations(self, run_id: str) -> list[ValidationSummary]:
        rows = self._read(
            "SELECT doc FROM validation_results WHERE run_id = ? ORDER BY id", (run_id,))
        return [ValidationSummary.model_validate_json(str(r[0])) for r in rows]

    def model_call_count(self, run_id: str) -> int:
        rows = self._read("SELECT COUNT(*) FROM model_calls WHERE run_id = ?", (run_id,))
        return int(str(rows[0][0]))
