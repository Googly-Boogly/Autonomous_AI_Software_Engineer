"""Create standalone example git repositories from the test fixtures.

    python -m engineer.examples            # -> ./examples/<name>/ (each its own git repo)
    python -m engineer.examples --force    # recreate

The fixtures in tests/fixtures/repos are plain directories (not nested git
repos). Each example is a fresh copy with a single initial commit, so runs
against it never touch this project's own repository.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from engineer.git import cli

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "repos"


def materialize(fixture: Path, dest: Path) -> Path:
    """Copy a fixture directory to ``dest`` and make it a git repo with one commit."""
    shutil.copytree(fixture, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    cli.git(dest, "init", "-q", "-b", "main")
    cli.git(dest, "add", "-A")
    cli.git(dest, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
            "-c", "commit.gpgsign=false", "commit", "-q", "-m", "Initial commit")
    return dest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m engineer.examples")
    p.add_argument("--dest", type=Path, default=PROJECT_ROOT / "examples")
    p.add_argument("--force", action="store_true", help="delete and recreate existing examples")
    args = p.parse_args(argv)
    args.dest.mkdir(parents=True, exist_ok=True)
    for fixture in sorted(d for d in FIXTURES.iterdir() if d.is_dir()):
        target = args.dest / fixture.name
        if target.exists():
            if not args.force:
                print(f"exists (use --force to recreate): {target}")
                continue
            # Only ever delete directories we created: they must be inside --dest.
            if target.resolve().parent != args.dest.resolve():
                raise SystemExit(f"refusing to delete {target}")
            shutil.rmtree(target)
        materialize(fixture, target)
        print(f"created {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
