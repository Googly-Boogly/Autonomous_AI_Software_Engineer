"""Runtime settings, read from environment variables.

Settings are loaded once by the operator-facing entrypoint and passed down
explicitly. Nothing the model produces can alter them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials.json",
    "credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "secrets/",
    ".aws/",
    ".ssh/",
    ".gnupg/",
    ".git/",
)


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_dir: Path = Field(default=Path("data"))
    # claude-code (default) | anthropic | scripted
    provider: str = "claude-code"
    # None => Claude Code's own configured model; claude-opus-5 for the anthropic provider.
    model: str | None = None
    claude_code_cli: str | None = None  # path to the `claude` binary; None => SDK default
    effort: str = "high"
    max_iterations: int = 5
    max_tool_calls_per_iteration: int = 40
    max_planner_turns: int = 15
    command_timeout_seconds: int = 300
    max_file_bytes: int = 256_000
    output_tail_chars: int = 4000
    python_executable: str = Field(default_factory=lambda: sys.executable)
    require_plan_approval: bool = False
    sensitive_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS
    git_author_name: str = "Autonomous Engineer"
    git_author_email: str = "autonomous-engineer@localhost"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "engineer.db"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(**overrides: object) -> Settings:
    env: dict[str, object] = {}
    mapping: dict[str, tuple[str, type]] = {
        "ENGINEER_DATA_DIR": ("data_dir", Path),
        "ENGINEER_PROVIDER": ("provider", str),
        "ENGINEER_MODEL": ("model", str),
        "ENGINEER_EFFORT": ("effort", str),
        "MAX_AGENT_ITERATIONS": ("max_iterations", int),
        "MAX_TOOL_CALLS_PER_ITERATION": ("max_tool_calls_per_iteration", int),
        "ENGINEER_COMMAND_TIMEOUT": ("command_timeout_seconds", int),
        "ENGINEER_PYTHON": ("python_executable", str),
        "ENGINEER_CLAUDE_CLI": ("claude_code_cli", str),
    }
    for var, (field, typ) in mapping.items():
        if var in os.environ:
            env[field] = typ(os.environ[var])
    env["require_plan_approval"] = _env_bool("REQUIRE_PLAN_APPROVAL", False)
    extra = os.environ.get("ENGINEER_EXTRA_SENSITIVE_PATTERNS")
    if extra:
        env["sensitive_patterns"] = DEFAULT_SENSITIVE_PATTERNS + tuple(
            p.strip() for p in extra.split(",") if p.strip()
        )
    env.update({k: v for k, v in overrides.items() if v is not None})
    return Settings.model_validate(env)
