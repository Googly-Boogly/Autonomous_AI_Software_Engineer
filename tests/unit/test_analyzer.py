from engineer.analysis.analyzer import RepositoryAnalyzer
from engineer.config import DEFAULT_SENSITIVE_PATTERNS
from engineer.git import cli
from engineer.git.views import CommitView
from engineer.policy.paths import PathPolicy
from tests.conftest import commit_all


def _analyze(repo, text):
    policy = PathPolicy(repo, DEFAULT_SENSITIVE_PATTERNS)
    head = cli.head_commit(repo)
    return RepositoryAnalyzer(CommitView(repo, head, policy)).analyze(
        text, root=str(repo), head_commit=head, current_branch=cli.current_branch(repo))


def test_fastapi_repo(make_repo):
    repo = make_repo("sample-fastapi")
    ctx = _analyze(repo, "Add a GET /health endpoint to the app")
    assert ctx.languages == {"Python": 3}
    assert "FastAPI" in ctx.frameworks
    assert ctx.test_frameworks == ["pytest"]
    assert ctx.available_commands == ["pytest"]
    assert "pyproject.toml" in ctx.config_files
    assert ctx.current_branch == "main"
    assert ctx.relevant_files[0].path == "app/main.py"
    assert [d.path for d in ctx.instruction_docs] == ["README.md"]


def test_calculator_relevance(make_repo):
    ctx = _analyze(make_repo("calculator"), "Fix subtraction in subtract and add regression test")
    top = [r.path for r in ctx.relevant_files[:2]]
    assert "calculator/ops.py" in top


def test_detects_ruff_mypy_ci_and_agent_docs(make_repo):
    repo = make_repo("inventory")
    (repo / "pyproject.toml").write_text(
        (repo / "pyproject.toml").read_text() + "\n[tool.ruff]\n\n[tool.mypy]\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    (repo / "AGENTS.md").write_text("Always add tests.\n")
    commit_all(repo)
    ctx = _analyze(repo, "anything")
    assert ctx.available_commands == ["pytest", "ruff_check", "ruff_format_check", "mypy"]
    assert ctx.ci_workflows == [".github/workflows/ci.yml"]
    assert "AGENTS.md" in [d.path for d in ctx.instruction_docs]


def test_sensitive_files_never_reach_context(make_repo):
    repo = make_repo("calculator")
    (repo / ".env").write_text("TOKEN=ghp_abcdefghijklmnop\n")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "prod.txt").write_text("hunter2\n")
    commit_all(repo, "oops, committed secrets")
    ctx = _analyze(repo, "subtract token hunter2")
    dump = ctx.model_dump_json()
    assert "ghp_abcdefghijklmnop" not in dump and "hunter2" not in dump.replace(
        "subtract token hunter2", "")
    assert ".env" not in ctx.directory_tree and "prod.txt" not in ctx.directory_tree


def test_repo_without_tests_has_no_test_command(empty_repo):
    ctx = _analyze(empty_repo, "anything")
    assert ctx.available_commands == []
