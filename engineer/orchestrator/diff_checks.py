"""Deterministic checks on the final diff.

This is the Phase 1 stand-in for the review agent (Phase 4). These checks are
cheap, objective, and catch the most common ways an agent "passes" without
doing the work: empty diffs, missing tests, and deleted/weakened tests.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from engineer.git import cli
from engineer.git.worktree import Workspace
from engineer.models.plan import EngineeringPlan
from engineer.models.report import DiffCheck
from engineer.policy.paths import PathDenied, PathPolicy

_TEST_FN = re.compile(r"^\s*(?:async\s+)?def\s+test_\w*\s*\(", re.MULTILINE)
MAX_CHANGED_LINES = 800


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix == ".py" and (
        p.name.startswith("test_") or p.name.endswith("_test.py") or "tests" in p.parts
        or p.name == "conftest.py"
    )


def _count_test_functions(ws: Workspace, ref: str | None) -> int:
    """Count `def test_*` in test files at ``ref`` (None = working tree)."""
    total = 0
    if ref is None:
        files = cli.tracked_and_untracked_files(ws.worktree)
        for f in files:
            if is_test_file(f) and (ws.worktree / f).is_file():
                total += len(_TEST_FN.findall(
                    (ws.worktree / f).read_text(encoding="utf-8", errors="replace")))
        return total
    out = cli.git(ws.worktree, "ls-tree", "-r", "--name-only", "-z", ref)
    for f in (x for x in out.split("\0") if x and is_test_file(x)):
        total += len(_TEST_FN.findall(cli.git(ws.worktree, "show", f"{ref}:{f}")))
    return total


def permitted(files: list[str], write_policy: PathPolicy) -> tuple[list[str], list[str]]:
    """Split changed paths into (permitted by write policy, not permitted)."""
    ok, bad = [], []
    for f in files:
        try:
            write_policy.resolve(f, for_write=True)
            ok.append(f)
        except PathDenied:
            bad.append(f)
    return ok, bad


def run_diff_checks(ws: Workspace, plan: EngineeringPlan, write_policy: PathPolicy,
                    all_changed: list[str]) -> list[DiffCheck]:
    files, outside = permitted(all_changed, write_policy)
    checks: list[DiffCheck] = []

    checks.append(DiffCheck(name="non_empty_diff", passed=bool(files),
                            detail=f"{len(files)} file(s) changed" if files else
                            "no changes were made"))

    checks.append(DiffCheck(name="changes_within_policy", passed=not outside,
                            detail=(f"{len(outside)} changed path(s) are not permitted by policy"
                                    " and will not be committed") if outside else
                            "all changed paths are permitted"))

    tests_changed = [f for f in files if is_test_file(f)]
    tests_expected = bool(plan.tests_to_add_or_modify)
    checks.append(DiffCheck(
        name="tests_added_or_modified",
        passed=bool(tests_changed) or not tests_expected,
        detail=(", ".join(tests_changed) if tests_changed else
                "plan listed tests to add, but no test files changed" if tests_expected else
                "plan listed no tests"),
    ))

    before = _count_test_functions(ws, ws.base_commit)
    after = _count_test_functions(ws, None)
    checks.append(DiffCheck(
        name="no_tests_removed", passed=after >= before,
        detail=f"test functions: {before} before, {after} after",
    ))

    numstat = cli.git(ws.worktree, "diff", "--numstat", ws.base_commit, "--", *files) if files \
        else ""
    changed_lines = 0
    for line in numstat.splitlines():
        a, d, *_ = line.split("\t")
        changed_lines += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    checks.append(DiffCheck(
        name="diff_size", passed=changed_lines <= MAX_CHANGED_LINES, blocking=False,
        detail=f"{changed_lines} lines changed (advisory limit {MAX_CHANGED_LINES})",
    ))
    unplanned = [f for f in files if f not in plan.files_expected_to_change
                 and f not in plan.tests_to_add_or_modify and not is_test_file(f)]
    checks.append(DiffCheck(
        name="only_planned_files", passed=not unplanned, blocking=False,
        detail=("unplanned changes: " + ", ".join(unplanned)) if unplanned else
        "all non-test changes were in the plan",
    ))
    return checks


def digest(checks: list[DiffCheck]) -> str:
    return "\n".join(f"- {c.name}: {'ok' if c.passed else 'FAILED'} — {c.detail}"
                     for c in checks if not c.passed)
