from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from engineer.config import Settings, load_settings
from engineer.examples import FIXTURES, materialize
from engineer.git import cli
from engineer.models.task import EngineeringTask
from engineer.orchestrator.orchestrator import Orchestrator
from engineer.persistence.store import Store
from engineer.providers.scripted import ScriptedProvider

SCRIPTS = Path(__file__).parent / "fixtures" / "scripts"


def commit_all(repo: Path, message: str = "commit") -> str:
    cli.git(repo, "add", "-A")
    cli.git(repo, "-c", "user.name=T", "-c", "user.email=t@localhost", "-c",
            "commit.gpgsign=false", "commit", "-q", "-m", message)
    return cli.head_commit(repo)


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[[str], Path]:
    """Materialize a fixture repo (tests/fixtures/repos/<name>) as a fresh git repo."""
    def _make(name: str) -> Path:
        return materialize(FIXTURES / name, tmp_path / "repos" / name)
    return _make


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repos" / "plain"
    repo.mkdir(parents=True)
    cli.git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# plain\n")
    commit_all(repo, "init")
    return repo


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(data_dir=tmp_path / "data", provider="scripted",
                         command_timeout_seconds=120)


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    s = Store(settings.db_path)
    yield s
    s.close()


def load_script(name: str) -> dict[str, Any]:
    return json.loads((SCRIPTS / f"{name}.json").read_text())


@pytest.fixture
def run_task(settings: Settings, store: Store) -> Callable[..., Any]:
    """Run a task end-to-end with a scripted provider. Returns (report, provider)."""
    def _run(repo: Path, script: dict[str, Any], *, description: str = "Do the task.",
             settings_override: Settings | None = None, approver: Any = None,
             **task_kwargs: Any) -> Any:
        provider = ScriptedProvider(script)
        task = EngineeringTask(title=task_kwargs.pop("title", description[:80]),
                               description=description, repository=str(repo), **task_kwargs)
        orch = Orchestrator(settings_override or settings, provider, store, approver=approver)
        return orch.execute(task), provider
    return _run
