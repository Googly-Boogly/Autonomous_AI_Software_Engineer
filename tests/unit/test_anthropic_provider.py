"""The Anthropic adapter, exercised against a fake SDK client (no network)."""

from types import SimpleNamespace

import pytest

anthropic = pytest.importorskip("anthropic")

from engineer.providers.anthropic_provider import AnthropicSession, estimate_cost  # noqa: E402
from engineer.providers.base import ProviderError, ToolResultMessage, ToolSpec  # noqa: E402


def _resp(content, stop_reason="end_turn", inp=100, out=50):
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=10,
                              cache_creation_input_tokens=0),
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        create = self._create
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    def _create(self, **kwargs):
        # Snapshot the messages list: the session appends to it after we return.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


TOOLS = [ToolSpec(name="read_file", description="d", input_schema={"type": "object"})]


def _session(client, model="claude-opus-5"):
    return AnthropicSession(client, model, "high", "SYSTEM", TOOLS)


def test_tool_use_round_trip_and_request_shape():
    tool_block = SimpleNamespace(type="tool_use", id="tu_1", name="read_file",
                                 input={"path": "a.py"})
    client = FakeClient([
        _resp([SimpleNamespace(type="text", text="Reading."), tool_block], "tool_use"),
        _resp([SimpleNamespace(type="text", text="Done.")]),
    ])
    s = _session(client)
    turn = s.send(user_text="hi")
    assert turn.text == "Reading."
    assert [(c.id, c.name, c.arguments) for c in turn.tool_calls] == [
        ("tu_1", "read_file", {"path": "a.py"})]
    assert turn.usage.input_tokens == 110 and turn.usage.output_tokens == 50
    assert turn.usage.cost_usd == pytest.approx(estimate_cost("claude-opus-5", 110, 50))

    first = client.calls[0]
    assert first["model"] == "claude-opus-5"
    assert first["system"] == "SYSTEM"
    assert first["thinking"] == {"type": "adaptive"}
    assert first["output_config"] == {"effort": "high"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["tools"][0]["name"] == "read_file"

    s.send(tool_results=[ToolResultMessage(call_id="tu_1", content="x = 1")])
    second = client.calls[1]["messages"]
    # History is append-only: user, assistant (full content), user(tool_result).
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[1]["content"][1] is tool_block
    assert second[2]["content"] == [{"type": "tool_result", "tool_use_id": "tu_1",
                                     "content": "x = 1", "is_error": False}]


def test_non_fallback_model_uses_plain_endpoint():
    client = FakeClient([_resp([SimpleNamespace(type="text", text="ok")])])
    _session(client, model="claude-sonnet-5").send(user_text="hi")
    assert "fallbacks" not in client.calls[0] and "betas" not in client.calls[0]


def test_refusal_raises():
    client = FakeClient([_resp([], "refusal")])
    with pytest.raises(ProviderError, match="refusal"):
        _session(client).send(user_text="hi")


def test_max_tokens_drops_possibly_truncated_tool_calls():
    block = SimpleNamespace(type="tool_use", id="t", name="read_file", input={"pa": ""})
    client = FakeClient([_resp([block], "max_tokens")])
    turn = _session(client).send(user_text="hi")
    assert turn.tool_calls == [] and turn.stop_reason == "max_tokens"


def test_empty_turn_rejected():
    with pytest.raises(ProviderError):
        _session(FakeClient([])).send()


def _status_error(cls, code):
    import httpx2

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(code, request=req), body=None)


@pytest.mark.parametrize("exc_factory,retryable,match", [
    (lambda: _status_error(anthropic.RateLimitError, 429), True, "rate limited"),
    (lambda: _status_error(anthropic.AuthenticationError, 401), False, "authentication"),
    (lambda: _status_error(anthropic.InternalServerError, 500), True, "500"),
    (lambda: _status_error(anthropic.BadRequestError, 400), False, "400"),
])
def test_api_errors_are_mapped(exc_factory, retryable, match):
    client = FakeClient([exc_factory()])
    with pytest.raises(ProviderError, match=match) as info:
        _session(client).send(user_text="hi")
    assert info.value.retryable is retryable


def test_missing_credentials_is_a_clear_provider_error():
    client = FakeClient([TypeError('"Could not resolve authentication method. Expected one of '
                                   'api_key, auth_token, or credentials to be set."')])
    with pytest.raises(ProviderError, match="no Anthropic credentials"):
        _session(client).send(user_text="hi")


def test_failed_request_does_not_corrupt_history():
    ok = _resp([SimpleNamespace(type="text", text="ok")])
    client = FakeClient([_status_error(anthropic.InternalServerError, 500), ok])
    s = _session(client)
    with pytest.raises(ProviderError):
        s.send(user_text="first")
    s.send(user_text="retry")
    assert [m["role"] for m in client.calls[1]["messages"]] == ["user"]


def test_cost_estimate():
    assert estimate_cost("claude-opus-5", 1_000_000, 1_000_000) == pytest.approx(30.0)
    assert estimate_cost("unknown-model", 0, 0) == 0.0
