from __future__ import annotations

from pathlib import Path

from engineer.providers.base import (
    ModelProvider,
    ModelSession,
    ModelTurn,
    ProviderError,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    TurnUsage,
)
from engineer.providers.scripted import ScriptedProvider


def build_provider(name: str, *, model: str, effort: str,
                   script: Path | None = None) -> ModelProvider:
    if name == "scripted":
        if script is None:
            raise ValueError("the scripted provider requires --script")
        return ScriptedProvider.from_file(script)
    if name == "anthropic":
        from engineer.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=model, effort=effort)
    raise ValueError(f"unknown provider: {name!r}")


__all__ = [
    "ModelProvider",
    "ModelSession",
    "ModelTurn",
    "ProviderError",
    "ScriptedProvider",
    "ToolCall",
    "ToolResultMessage",
    "ToolSpec",
    "TurnUsage",
    "build_provider",
]
