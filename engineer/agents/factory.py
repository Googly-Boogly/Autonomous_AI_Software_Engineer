"""Choose the agent backend from settings."""

from __future__ import annotations

from pathlib import Path

from engineer.agents.backends import AgentBackend, ProviderBackend
from engineer.config import Settings
from engineer.providers import build_provider

DEFAULT_API_MODEL = "claude-opus-5"


def build_agents(settings: Settings, *, script: Path | None = None,
                 max_budget_usd: float | None = None) -> AgentBackend:
    """``claude-code`` (default): Claude Code writes the code, gated by our policy.
    ``anthropic``: our own tool loop on the Messages API. ``scripted``: offline replay."""
    if settings.provider == "claude-code":
        from engineer.agents.claude_code import ClaudeCodeBackend

        return ClaudeCodeBackend(model=settings.model, max_budget_usd=max_budget_usd,
                                 cli_path=settings.claude_code_cli,
                                 tool_timeout_s=settings.command_timeout_seconds + 60)
    provider = build_provider(settings.provider, model=settings.model or DEFAULT_API_MODEL,
                              effort=settings.effort, script=script)
    return ProviderBackend(provider)
