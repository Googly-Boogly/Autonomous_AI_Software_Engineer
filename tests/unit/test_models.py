import pytest
from pydantic import ValidationError

from engineer.git.cli import is_safe_branch_name
from engineer.git.worktree import make_branch_name
from engineer.models.plan import EngineeringPlan
from engineer.models.task import EngineeringTask


def _task(title: str) -> EngineeringTask:
    return EngineeringTask(title=title, description="d", repository="/r")


@pytest.mark.parametrize("title", [
    "Add a GET /health endpoint that returns {'status':'ok'} and add tests.",
    "Fix ../../etc/passwd; rm -rf / && `whoami` $(id)",
    "refs/heads/main",
    "!!!",
    "Ünïcødé ✨ title",
    "x" * 200,
])
def test_branch_names_are_always_safe(title):
    t = _task(title)
    name = make_branch_name(t.id, t.slug())
    assert name.startswith("ai-engineer/task_")
    assert is_safe_branch_name(name)
    assert ".." not in name and " " not in name


def test_slug_truncates_on_word_boundary():
    t = _task("Add a GET /health endpoint that returns status ok and add tests")
    assert t.slug() == "add-a-get-health-endpoint-that-returns"


@pytest.mark.parametrize("name,ok", [
    ("ai-engineer/task_1-fix", True),
    ("main", True),
    ("-starts-with-dash", False),
    ("has..dots", False),
    ("ends.lock", False),
    ("a/.hidden", False),
    ("with space", False),
    ("x@{y}", False),
    ("trailing/", False),
])
def test_is_safe_branch_name(name, ok):
    assert is_safe_branch_name(name) is ok


def test_plan_rejects_unknown_fields_and_empty_steps():
    base = {"task_summary": "s", "implementation_steps": ["do it"]}
    EngineeringPlan.model_validate(base)
    with pytest.raises(ValidationError):
        EngineeringPlan.model_validate({**base, "grant_permissions": True})
    with pytest.raises(ValidationError):
        EngineeringPlan.model_validate({**base, "implementation_steps": []})


def test_task_limits_are_bounded():
    with pytest.raises(ValidationError):
        EngineeringTask(title="t", description="d", repository="/r", max_iterations=1000)
    with pytest.raises(ValidationError):
        EngineeringTask(title="t", description="d", repository="/r", max_model_cost_usd=-1)
