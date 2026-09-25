"""CLI: run one local engineering task.

    python -m engineer.run --repo ./examples/sample-fastapi \\
        --task "Add a GET /health endpoint that returns {'status': 'ok'} and add tests."

Exit codes: 0 = work is ready for human review; 1 = the run did not succeed;
2 = usage/configuration error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from engineer.agents.factory import build_agents
from engineer.config import load_settings
from engineer.models.plan import EngineeringPlan
from engineer.models.run import RunState
from engineer.models.task import EngineeringTask, TaskSource
from engineer.orchestrator.orchestrator import Orchestrator
from engineer.persistence.store import Store
from engineer.providers import ProviderError


def _title_from(task_text: str) -> str:
    first = task_text.strip().splitlines()[0]
    for sep in (". ", "? ", "! "):
        if sep in first:
            first = first.split(sep, 1)[0]
    return first[:120].rstrip(".")


def _interactive_approver(plan: EngineeringPlan) -> bool:
    print("\n=== Proposed plan ===")
    print(plan.model_dump_json(indent=2))
    try:
        answer = input("\nApprove this plan? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m engineer.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", required=True, type=Path, help="path to a local git repository root")
    p.add_argument("--task", required=True, help="task description")
    p.add_argument("--title", help="short title (default: first sentence of --task)")
    p.add_argument("--acceptance", action="append", default=[], metavar="CRITERION",
                   help="acceptance criterion (repeatable)")
    p.add_argument("--allowed-path", action="append", default=[], metavar="PATH",
                   help="restrict writes to this repo-relative path (repeatable)")
    p.add_argument("--forbidden-path", action="append", default=[], metavar="PATTERN",
                   help="deny all access to matching paths (repeatable)")
    p.add_argument("--max-iterations", type=int, help="repair iterations (default: 5)")
    p.add_argument("--max-cost", type=float, default=2.0, help="model cost ceiling in USD")
    p.add_argument("--provider", choices=["claude-code", "anthropic", "scripted"],
                   help="who writes the code: claude-code (default; uses your Claude Code "
                        "login), anthropic (Messages API), or scripted (offline replay)")
    p.add_argument("--model", help="model id (default: Claude Code's configured model, or "
                                   "claude-opus-5 for --provider anthropic)")
    p.add_argument("--script", type=Path, help="script file for --provider scripted")
    p.add_argument("--data-dir", type=Path, help="where runs and the database live")
    p.add_argument("--approve-plan", action="store_true",
                   help="pause after planning and ask for interactive approval")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(
        data_dir=args.data_dir, provider=args.provider, model=args.model,
        max_iterations=args.max_iterations,
        require_plan_approval=True if args.approve_plan else None,
    )
    try:
        agents = build_agents(settings, script=args.script, max_budget_usd=args.max_cost)
    except (ValueError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    task = EngineeringTask(
        title=args.title or _title_from(args.task),
        description=args.task,
        acceptance_criteria=args.acceptance,
        repository=str(args.repo.expanduser().resolve()),
        source=TaskSource.LOCAL,
        allowed_paths=args.allowed_path,
        forbidden_paths=args.forbidden_path,
        max_iterations=args.max_iterations or settings.max_iterations,
        max_model_cost_usd=args.max_cost,
    )
    store = Store(settings.db_path)
    orch = Orchestrator(settings, agents, store,
                        approver=_interactive_approver if args.approve_plan else None,
                        progress=lambda m: print(f"  · {m}", file=sys.stderr, flush=True))
    print(f"Task {task.id}: {task.title}  [agents: {agents.name}]", file=sys.stderr)
    report = orch.execute(task)
    store.close()

    artifacts = settings.runs_dir.resolve() / report.run_id / "artifacts"
    print()
    print(f"Run:        {report.run_id}")
    print(f"State:      {report.final_state.value}")
    if report.failure_reason:
        print(f"Reason:     {report.failure_reason}")
    print(f"Iterations: {report.iterations_used} / {report.max_iterations}")
    if report.final_validation:
        print(f"Validation: {report.final_validation.one_line()}")
    print(f"Files:      {', '.join(report.files_changed) or '(none)'}")
    if report.branch:
        print(f"Branch:     {report.branch} (local, not pushed)")
        print(f"Worktree:   {report.worktree_path}")
    print(f"Cost:       ${report.usage.cost_usd:.4f} ({report.usage.model_calls} model calls)")
    print(f"Report:     {artifacts / 'report.md'}")
    if report.final_state == RunState.WAITING_FOR_HUMAN:
        print("\nReady for human review. Nothing was merged, pushed, or deployed.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
