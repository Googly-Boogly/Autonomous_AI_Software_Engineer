"""The orchestrator owns a run from PENDING to a terminal / hand-off state.

It is the only component that changes run state, decides whether to iterate,
and decides whether the work is ready for a human. Agents only propose.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from engineer.agents.backends import AgentBackend, ProviderBackend
from engineer.agents.planner import PlanningFailed
from engineer.analysis.analyzer import RepositoryAnalyzer
from engineer.config import Settings
from engineer.git import cli
from engineer.git.views import CommitView, FileTooLarge, WorktreeView
from engineer.git.worktree import (
    Workspace,
    WorkspaceError,
    commit_paths,
    create_workspace,
    diff_paths,
    make_branch_name,
    pending_changes,
    validate_repository,
)
from engineer.models.plan import EngineeringPlan
from engineer.models.repo_context import RepositoryContext
from engineer.models.report import DiffCheck, RunReport
from engineer.models.run import TERMINAL_STATES, EngineeringRun, RunState
from engineer.models.task import EngineeringTask, TaskStatus
from engineer.models.tools import PolicyDecision, ToolAction
from engineer.models.validation import ValidationSummary
from engineer.orchestrator import diff_checks
from engineer.orchestrator.report import render_markdown
from engineer.orchestrator.state_machine import transition
from engineer.persistence.store import Store
from engineer.policy.commands import CommandRegistry
from engineer.policy.paths import PathDenied, PathPolicy
from engineer.providers.base import ModelProvider, ModelTurn, ProviderError
from engineer.tools.toolbox import Toolbox
from engineer.validation.engine import ValidationEngine, failure_digest

PlanApprover = Callable[[EngineeringPlan], bool]
ProgressFn = Callable[[str], None]


class BudgetExceeded(Exception):
    pass


PHASE1_LIMITATIONS = [
    "Phase 1 MVP: validation commands execute repository code as the operator's OS user "
    "without container isolation; only run against repositories you trust.",
    "No LLM diff review yet (Phase 4); deterministic diff checks are used instead.",
    "No GitHub integration yet (Phase 3); the branch is local only and was not pushed.",
]


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        agents: AgentBackend | ModelProvider,
        store: Store,
        *,
        approver: PlanApprover | None = None,
        progress: ProgressFn | None = None,
    ) -> None:
        self.settings = settings
        # A bare ModelProvider means "run our own tool loops on it".
        self.backend: AgentBackend = (
            cast(AgentBackend, agents) if hasattr(agents, "engineer")
            else ProviderBackend(agents)
        )
        self.store = store
        self.approver = approver
        self.progress = progress or (lambda _msg: None)

    # ------------------------------------------------------------------ run

    def execute(self, task: EngineeringTask) -> RunReport:
        started = time.monotonic()
        run = EngineeringRun(
            task_id=task.id,
            max_iterations=min(task.max_iterations, self.settings.max_iterations),
        )
        self.store.save_task(task)
        self.store.save_run(run)
        self._denials = 0
        self._summaries: list[str] = []
        self._artifacts: dict[str, str] = {}

        plan: EngineeringPlan | None = None
        ws: Workspace | None = None
        final_validation: ValidationSummary | None = None
        checks: list[DiffCheck] = []
        write_policy: PathPolicy | None = None

        try:
            # ---------------- analysis ----------------
            self._move(run, RunState.ANALYZING, "starting repository analysis")
            repo = Path(task.repository).expanduser()
            top, repo_warnings = validate_repository(repo)
            base = cli.head_commit(top)
            read_policy = PathPolicy(top, self.settings.sensitive_patterns,
                                     tuple(task.forbidden_paths))
            commit_view = CommitView(top, base, read_policy)
            ctx = RepositoryAnalyzer(commit_view).analyze(
                f"{task.title}\n{task.description}\n" + "\n".join(task.acceptance_criteria),
                root=str(top), head_commit=base, current_branch=cli.current_branch(top),
            )
            ctx.warnings.extend(repo_warnings)
            self._artifacts["repository_context.json"] = ctx.model_dump_json(indent=2)
            for w in ctx.warnings:
                self.progress(f"warning: {w}")
            registry = CommandRegistry.for_repository(self.settings.python_executable,
                                                      ctx.available_commands)
            if not any(s.required and s.kind.value == "test" for s in registry.specs()):
                self._move(run, RunState.BLOCKED,
                           "no supported test command was discovered (Phase 1 requires pytest); "
                           "refusing to run without objective validation")
                return self._finish(task, run, started, plan, ws, None, checks)
            self.progress(f"analysis: {ctx.total_files} files, languages={ctx.languages}, "
                          f"checks={registry.ids()}")

            # ---------------- planning ----------------
            self._move(run, RunState.PLANNING, "requesting implementation plan")
            planner_tools = Toolbox(agent="planner", view=commit_view,
                                    max_file_bytes=self.settings.max_file_bytes,
                                    recorder=self._recorder(run), run_id=run.id)
            planner = self.backend.planner(planner_tools,
                                           max_turns=self.settings.max_planner_turns,
                                           on_turn=self._usage_hook(run, task))
            plan = planner.plan(task, ctx, self._excerpts(commit_view, ctx))
            self.store.save_plan(run.id, plan)
            self.progress(f"plan: {len(plan.implementation_steps)} steps, "
                          f"files={plan.files_expected_to_change}")

            if self.settings.require_plan_approval:
                self._move(run, RunState.WAITING_FOR_APPROVAL, "plan approval required")
                if self.approver is None:
                    self._move(run, RunState.BLOCKED,
                               "plan approval is required but no approver is available")
                    return self._finish(task, run, started, plan, ws, None, checks)
                if not self.approver(plan):
                    self._move(run, RunState.CANCELLED, "operator rejected the plan")
                    return self._finish(task, run, started, plan, ws, None, checks)

            # ---------------- workspace ----------------
            self._move(run, RunState.PREPARING_WORKSPACE, "creating isolated worktree")
            branch = make_branch_name(task.id, task.slug())
            ws = create_workspace(top, self.settings.runs_dir, run.id, branch)
            run.branch, run.base_commit, run.worktree_path = branch, ws.base_commit, str(
                ws.worktree)
            self.store.save_run(run)
            self.progress(f"workspace: {ws.worktree} on branch {branch}")

            write_policy = PathPolicy(ws.worktree, self.settings.sensitive_patterns,
                                      tuple(task.forbidden_paths), tuple(task.allowed_paths))
            validator = ValidationEngine(registry, ws.worktree, ws.sandbox_home,
                                         timeout=self.settings.command_timeout_seconds,
                                         tail_chars=self.settings.output_tail_chars)
            eng_tools = Toolbox(agent="engineer", view=WorktreeView(ws.worktree, write_policy),
                                max_file_bytes=self.settings.max_file_bytes,
                                recorder=self._recorder(run), run_id=run.id,
                                write_policy=write_policy, registry=registry,
                                validator=validator,
                                output_tail_chars=self.settings.output_tail_chars)
            eng_tools.enable_engineering_tools()
            engineer = self.backend.engineer(
                eng_tools, max_tool_calls=self.settings.max_tool_calls_per_iteration,
                on_turn=self._usage_hook(run, task))

            # ---------------- implement / validate / debug loop ----------------
            feedback: str | None = None
            for iteration in range(1, run.max_iterations + 1):
                run.iteration = iteration
                self._move(run, RunState.IMPLEMENTING, f"iteration {iteration}")
                outcome = engineer.run_iteration(task, plan, ctx, iteration=iteration,
                                                 max_iterations=run.max_iterations,
                                                 feedback=feedback)
                self._summaries.append(outcome.summary)
                self.progress(f"iteration {iteration}: {outcome.tool_calls} tool calls, "
                              f"finished={outcome.finished}")

                self._move(run, RunState.VALIDATING, "running allowlisted checks")
                final_validation = validator.run_all(iteration)
                self.store.record_validation(run.id, final_validation)
                self.progress(f"validation: {final_validation.one_line()}")

                if not final_validation.passed:
                    feedback = failure_digest(final_validation)
                    if iteration == run.max_iterations:
                        self._move(run, RunState.FAILED,
                                   f"validation still failing after {iteration} iteration(s): "
                                   f"{final_validation.one_line()}")
                        break
                    self._move(run, RunState.DEBUGGING, final_validation.one_line())
                    continue

                self._move(run, RunState.REVIEWING, "validation passed; running diff checks")
                checks = diff_checks.run_diff_checks(
                    ws, plan, write_policy, pending_changes(ws.worktree, ws.base_commit))
                blocking = [c for c in checks if c.blocking and not c.passed]
                if not blocking:
                    self._move(run, RunState.READY_FOR_PR, "validation and diff checks passed")
                    self._move(run, RunState.WAITING_FOR_HUMAN,
                               "local mode: branch ready for human review")
                    break
                feedback = ("Validation passed, but these deterministic diff checks failed:\n"
                            + diff_checks.digest(checks))
                if iteration == run.max_iterations:
                    self._move(run, RunState.FAILED,
                               "diff checks failing after final iteration: "
                               + ", ".join(c.name for c in blocking))
                    break
                self.progress("diff checks failed: " + ", ".join(c.name for c in blocking))

        except WorkspaceError as exc:
            self._fail(run, RunState.BLOCKED, str(exc))
        except PlanningFailed as exc:
            self._fail(run, RunState.FAILED, f"planning failed: {exc}")
        except ProviderError as exc:
            self._fail(run, RunState.FAILED, f"model provider error: {exc}")
        except BudgetExceeded as exc:
            self._fail(run, RunState.BUDGET_EXCEEDED, str(exc))
        except KeyboardInterrupt:
            self._fail(run, RunState.CANCELLED, "interrupted by operator")
        except Exception as exc:  # never fabricate success; record and stop
            self._fail(run, RunState.FAILED, f"internal error: {type(exc).__name__}: {exc}")

        return self._finish(task, run, started, plan, ws, final_validation, checks,
                            write_policy)

    # ------------------------------------------------------------ helpers

    def _move(self, run: EngineeringRun, dst: RunState, reason: str) -> None:
        transition(run, dst, reason)
        self.store.save_run(run)
        self.progress(f"[{dst.value}] {reason}")

    def _fail(self, run: EngineeringRun, dst: RunState, reason: str) -> None:
        if run.state in TERMINAL_STATES:
            return
        self._move(run, dst, reason)

    def _recorder(self, run: EngineeringRun) -> Callable[[ToolAction], None]:
        def record(action: ToolAction) -> None:
            run.usage.tool_calls += 1
            if action.decision == PolicyDecision.DENY:
                self._denials += 1
            self.store.record_tool_action(action)
        return record

    def _usage_hook(self, run: EngineeringRun,
                    task: EngineeringTask) -> Callable[[str, ModelTurn], None]:
        def hook(agent: str, turn: ModelTurn) -> None:
            u = run.usage
            u.model_calls += 1
            u.input_tokens += turn.usage.input_tokens
            u.output_tokens += turn.usage.output_tokens
            u.cost_usd += turn.usage.cost_usd
            self.store.record_model_call(run.id, agent, turn.model, turn.usage.input_tokens,
                                         turn.usage.output_tokens, turn.usage.cost_usd,
                                         len(turn.tool_calls), turn.stop_reason)
            if u.cost_usd > task.max_model_cost_usd:
                raise BudgetExceeded(f"model cost ${u.cost_usd:.2f} exceeded the task budget "
                                     f"${task.max_model_cost_usd:.2f}")
        return hook

    def _excerpts(self, view: CommitView, ctx: RepositoryContext,
                  max_files: int = 5, max_chars: int = 6000) -> dict[str, str]:
        out: dict[str, str] = {}
        for rf in ctx.relevant_files[:max_files]:
            try:
                out[rf.path] = view.read_text(rf.path, self.settings.max_file_bytes)[:max_chars]
            except (PathDenied, FileNotFoundError, FileTooLarge):
                continue
        return out

    def _finish(
        self,
        task: EngineeringTask,
        run: EngineeringRun,
        started: float,
        plan: EngineeringPlan | None,
        ws: Workspace | None,
        validation: ValidationSummary | None,
        checks: list[DiffCheck],
        write_policy: PathPolicy | None = None,
    ) -> RunReport:
        files: list[str] = []
        stat = ""
        diff = ""
        limitations = list(PHASE1_LIMITATIONS)
        if ws is not None:
            try:
                changed = pending_changes(ws.worktree, ws.base_commit)
                policy = write_policy or PathPolicy(ws.worktree, self.settings.sensitive_patterns)
                files, excluded = diff_checks.permitted(changed, policy)
                if excluded:
                    limitations.append(
                        f"{len(excluded)} changed path(s) were outside policy and were NOT "
                        "committed or included in the diff; inspect the worktree manually.")
                stat = diff_paths(ws.worktree, ws.base_commit, files, stat=True)
                diff = diff_paths(ws.worktree, ws.base_commit, files)
                ok = run.state == RunState.WAITING_FOR_HUMAN
                prefix = "" if ok else "[INCOMPLETE] "
                msg = (f"{prefix}{task.title}\n\nGenerated by autonomous engineer run {run.id}.\n"
                       f"Final state: {run.state.value}.\n"
                       "Requires human review before merge.")
                sha = commit_paths(ws, files, msg, self.settings.git_author_name,
                                   self.settings.git_author_email)
                run.head_commit = sha or ws.base_commit
                if not ok and files:
                    limitations.append(f"Run did not succeed; partial work was committed to "
                                       f"{ws.branch} for inspection only.")
            except Exception as exc:
                limitations.append(f"could not finalize workspace: {type(exc).__name__}: {exc}")

        task.status = TaskStatus.IN_PROGRESS if run.state == RunState.WAITING_FOR_HUMAN else (
            TaskStatus.OPEN)
        self.store.save_task(task)
        self.store.save_run(run)

        report = RunReport(
            run_id=run.id, task_id=task.id, task_title=task.title, final_state=run.state,
            failure_reason=run.failure_reason, branch=run.branch, base_commit=run.base_commit,
            head_commit=run.head_commit, worktree_path=run.worktree_path,
            iterations_used=run.iteration, max_iterations=run.max_iterations, plan=plan,
            files_changed=files, diff_stat=stat, final_validation=validation,
            diff_checks=checks, engineer_summaries=self._summaries, usage=run.usage,
            policy_denials=self._denials, known_limitations=limitations,
            duration_seconds=time.monotonic() - started,
        )
        artifacts = self.settings.runs_dir.resolve() / run.id / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        for name, content in self._artifacts.items():
            (artifacts / name).write_text(content, encoding="utf-8")
        (artifacts / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        (artifacts / "report.md").write_text(render_markdown(report), encoding="utf-8")
        if plan is not None:
            (artifacts / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        if diff:
            (artifacts / "diff.patch").write_text(diff, encoding="utf-8")
        (artifacts / "state_history.json").write_text(
            json.dumps([h.model_dump(mode="json") for h in run.history], indent=2),
            encoding="utf-8")
        return report
