"""Anthropic Claude adapter (Messages API, manual tool loop).

We run the tool loop ourselves rather than using the SDK tool runner because
every tool call must pass through deterministic policy, be recorded, and be
counted against budgets before it executes.

Credentials are resolved by the SDK from the environment (ANTHROPIC_API_KEY,
or an ``ant auth login`` profile). They never enter prompts or logs.
"""

from __future__ import annotations

from typing import Any

from engineer.providers.base import (
    ModelTurn,
    ProviderError,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    TurnUsage,
)

# USD per million tokens (input, output). Approximate; used for budget tracking only.
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_NO_CREDENTIALS = (
    "no Anthropic credentials found: set ANTHROPIC_API_KEY (or run `ant auth login`), "
    "or use --provider scripted for an offline demo"
)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = PRICING.get(model, (5.0, 25.0))
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


class AnthropicSession:
    def __init__(self, client: Any, model: str, effort: str, system: str,
                 tools: list[ToolSpec]) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._system = system
        self._tools = [t.model_dump() for t in tools]
        self._messages: list[dict[str, Any]] = []

    def send(
        self,
        *,
        user_text: str | None = None,
        tool_results: list[ToolResultMessage] | None = None,
    ) -> ModelTurn:
        import anthropic

        content: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content,
             "is_error": r.is_error}
            for r in (tool_results or [])
        ]
        if user_text:
            content.append({"type": "text", "text": user_text})
        if not content:
            raise ProviderError("empty user turn")
        self._messages.append({"role": "user", "content": content})

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 16000,
            "system": self._system,
            "messages": self._messages,
            "tools": self._tools,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self._effort},
            # The tool loop resends the whole (append-only) history every turn;
            # automatic prompt caching makes those repeated prefixes cheap.
            "cache_control": {"type": "ephemeral"},
        }
        # Server-side refusal fallback (routes by refusal category).
        if self._model in ("claude-opus-5", "claude-fable-5-1", "claude-opus-5-5"):
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            try:
                if "betas" in kwargs:
                    resp = self._client.beta.messages.create(**kwargs)
                else:
                    resp = self._client.messages.create(**kwargs)
            except BaseException:
                self._messages.pop()  # keep history valid (alternating) if the caller retries
                raise
        except anthropic.RateLimitError as e:
            raise ProviderError(f"rate limited: {e.message}", retryable=True) from None
        except anthropic.APITimeoutError:
            raise ProviderError("model request timed out", retryable=True) from None
        except anthropic.APIConnectionError:
            raise ProviderError("could not connect to the model API", retryable=True) from None
        except anthropic.AuthenticationError:
            raise ProviderError("model API authentication failed") from None
        except anthropic.APIStatusError as e:
            raise ProviderError(f"model API error {e.status_code}: {e.message}",
                                retryable=e.status_code >= 500) from None
        except TypeError as e:
            # The SDK raises TypeError when no credentials can be resolved.
            if "authentication" in str(e).lower():
                raise ProviderError(_NO_CREDENTIALS) from None
            raise

        # Append the full content (including thinking blocks) unchanged.
        self._messages.append({"role": "assistant", "content": resp.content})
        usage = TurnUsage(
            input_tokens=(resp.usage.input_tokens or 0)
            + (getattr(resp.usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(resp.usage, "cache_creation_input_tokens", 0) or 0),
            output_tokens=resp.usage.output_tokens or 0,
        )
        usage.cost_usd = estimate_cost(self._model, usage.input_tokens, usage.output_tokens)

        if resp.stop_reason == "refusal":
            raise ProviderError("the model declined the request (refusal)")
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls: list[ToolCall] = []
        if resp.stop_reason != "max_tokens":  # tool inputs may be truncated at max_tokens
            calls = [ToolCall(id=b.id, name=b.name, arguments=dict(b.input or {}))
                     for b in resp.content if b.type == "tool_use"]
        return ModelTurn(text=text, tool_calls=calls, stop_reason=str(resp.stop_reason),
                         usage=usage, model=str(resp.model))


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5", effort: str = "high") -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ProviderError(
                "the 'anthropic' package is not installed: pip install -e '.[anthropic]'"
            ) from exc
        self._client = anthropic.Anthropic(max_retries=3)
        self.model = model
        self.effort = effort

    def start_session(self, *, role: str, system: str, tools: list[ToolSpec]) -> AnthropicSession:
        return AnthropicSession(self._client, self.model, self.effort, system, tools)
