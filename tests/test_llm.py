"""Tests for iac_cartographer.llm — Bedrock + Anthropic backends mocked."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from iac_cartographer.llm import AnthropicBackend, BedrockBackend, LLMResponse, _extract_text

# ─── _extract_text helper ─────────────────────────────────────────────


def test_extract_text_concatenates_text_blocks() -> None:
    content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert _extract_text(content) == "ab"


def test_extract_text_ignores_non_text_blocks() -> None:
    content = [{"type": "tool_use", "id": "x"}, {"type": "text", "text": "ok"}]
    assert _extract_text(content) == "ok"


def test_extract_text_returns_empty_string_when_empty() -> None:
    assert _extract_text([]) == ""
    assert _extract_text(None) == ""
    assert _extract_text("not a list") == ""  # type: ignore[arg-type]


# ─── BedrockBackend ────────────────────────────────────────────────────


def _bedrock_response(text: str, in_tokens: int = 100, out_tokens: int = 50) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
    }


def test_bedrock_backend_invokes_with_wrapped_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_invoke(model_id: str, body: dict[str, Any], region: str = "eu-central-1") -> dict[str, Any]:
        captured["model_id"] = model_id
        captured["body"] = body
        captured["region"] = region
        return _bedrock_response("hello world", in_tokens=1234, out_tokens=42)

    monkeypatch.setattr("iac_cartographer.llm.invoke_bedrock_model", fake_invoke)
    backend = BedrockBackend(region="us-east-1")
    response = backend.invoke(
        model_id="eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        system_prompt="be useful",
        user_blocks=[{"type": "text", "text": "tell me about this repo"}],
        max_tokens=4096,
    )

    assert response == LLMResponse(text="hello world", input_tokens=1234, output_tokens=42)
    assert captured["model_id"] == "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert captured["region"] == "us-east-1"
    body = captured["body"]
    # Bedrock requires `anthropic_version` in the body; no `model` field.
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert "model" not in body
    assert body["max_tokens"] == 4096
    # System block carries cache_control so Bedrock can serve repeated
    # system-prompt tokens from cache.
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["system"][0]["text"] == "be useful"
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == [{"type": "text", "text": "tell me about this repo"}]


def test_bedrock_backend_handles_missing_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some Bedrock responses lack `usage` (e.g. streaming partials,
    older response shapes). Token counts default to 0 in that case."""

    def fake_invoke(*_a: object, **_kw: object) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "no usage block"}]}

    monkeypatch.setattr("iac_cartographer.llm.invoke_bedrock_model", fake_invoke)
    response = BedrockBackend().invoke(
        model_id="x",
        system_prompt="s",
        user_blocks=[{"type": "text", "text": "u"}],
        max_tokens=100,
    )
    assert response == LLMResponse(text="no usage block", input_tokens=0, output_tokens=0)


def test_bedrock_backend_handles_non_dict_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: if boto3 returns something other than a dict (e.g. a
    typed object that doesn't quack right), the backend returns an empty
    LLMResponse rather than crashing."""

    def fake_invoke(*_a: object, **_kw: object) -> str:
        return "garbage"  # type: ignore[return-value]

    monkeypatch.setattr("iac_cartographer.llm.invoke_bedrock_model", fake_invoke)
    response = BedrockBackend().invoke(
        model_id="x",
        system_prompt="s",
        user_blocks=[],
        max_tokens=100,
    )
    assert response == LLMResponse(text="", input_tokens=0, output_tokens=0)


def test_bedrock_backend_propagates_invoke_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport-level errors (auth, throttling, ServiceQuotaExceeded)
    must propagate — the narrator's retry-once-then-skip orchestration
    catches them and decides whether to retry."""

    def fake_invoke(*_a: object, **_kw: object) -> None:
        raise RuntimeError("AccessDeniedException")

    monkeypatch.setattr("iac_cartographer.llm.invoke_bedrock_model", fake_invoke)
    with pytest.raises(RuntimeError, match="AccessDeniedException"):
        BedrockBackend().invoke(model_id="x", system_prompt="s", user_blocks=[], max_tokens=100)


# ─── AnthropicBackend ──────────────────────────────────────────────────


def test_anthropic_backend_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError, match="api_key is required"):
        AnthropicBackend(api_key="")


@respx.mock
def test_anthropic_backend_sends_messages_api_request() -> None:
    """The Anthropic-direct path uses `model` in the body (not the URL),
    sets `anthropic-version` as a header, and otherwise speaks the same
    Messages API as Bedrock."""
    captured_body: dict[str, Any] = {}
    captured_headers: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        captured_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "narrative json"}],
                "usage": {"input_tokens": 500, "output_tokens": 100},
            },
        )

    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=respond)

    backend = AnthropicBackend(api_key="sk-ant-test")
    response = backend.invoke(
        model_id="claude-sonnet-4-5-20250929",
        system_prompt="be useful",
        user_blocks=[{"type": "text", "text": "tell me"}],
        max_tokens=2048,
    )

    assert response == LLMResponse(text="narrative json", input_tokens=500, output_tokens=100)
    # Anthropic direct: model in the BODY, version in the HEADER.
    assert captured_body["model"] == "claude-sonnet-4-5-20250929"
    assert captured_body["max_tokens"] == 2048
    assert captured_body["system"][0]["text"] == "be useful"
    assert captured_body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured_body["messages"][0]["content"] == [{"type": "text", "text": "tell me"}]
    assert captured_headers["x-api-key"] == "sk-ant-test"
    assert captured_headers["anthropic-version"] == AnthropicBackend.ANTHROPIC_VERSION


@respx.mock
def test_anthropic_backend_raises_on_http_error() -> None:
    """4xx/5xx from the API must propagate (httpx.HTTPStatusError) so the
    narrator's retry path can decide what to do."""
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(401, json={"error": {"type": "authentication_error"}}),
    )
    with pytest.raises(httpx.HTTPStatusError):
        AnthropicBackend(api_key="sk-ant-bad").invoke(
            model_id="x",
            system_prompt="s",
            user_blocks=[],
            max_tokens=100,
        )


@respx.mock
def test_anthropic_backend_respects_custom_base_url() -> None:
    """Operators fronting the Anthropic API with a proxy can override the
    base URL. Trailing slashes are tolerated."""
    respx.post("https://proxy.example.com/anthropic/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "via proxy"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    )
    backend = AnthropicBackend(api_key="sk-ant-test", base_url="https://proxy.example.com/anthropic/")
    response = backend.invoke(model_id="x", system_prompt="s", user_blocks=[], max_tokens=100)
    assert response.text == "via proxy"


@respx.mock
def test_anthropic_backend_handles_missing_usage() -> None:
    """Anthropic responses normally include `usage`, but defensively we
    handle its absence the same way Bedrock does — return zeros."""
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "no usage"}]}),
    )
    response = AnthropicBackend(api_key="sk-ant-test").invoke(
        model_id="x",
        system_prompt="s",
        user_blocks=[],
        max_tokens=100,
    )
    assert response == LLMResponse(text="no usage", input_tokens=0, output_tokens=0)


# ─── VertexBackend ─────────────────────────────────────────────────────


def test_vertex_backend_rejects_empty_project_id() -> None:
    from iac_cartographer.llm import VertexBackend

    with pytest.raises(ValueError, match="project_id is required"):
        VertexBackend(project_id="")


def test_vertex_backend_raises_install_hint_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Anthropic SDK + [vertex] extra are optional. If the operator
    flips `llm.backend: vertex` without installing them, the first
    `invoke()` call should fail loud with a pip-install hint — not a
    confusing `ModuleNotFoundError` deep in a call stack."""
    import builtins

    from iac_cartographer.llm import LLMBackendImportError, VertexBackend

    real_import = builtins.__import__

    def _fail_anthropic(name: str, *args: Any, **kwargs: Any) -> Any:
        # Block only the anthropic.* imports; let everything else through.
        if name == "anthropic" or name.startswith("anthropic."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_anthropic)
    backend = VertexBackend(project_id="my-project")

    with pytest.raises(LLMBackendImportError, match=r"iac-cartographer\[gcp\]"):
        backend.invoke(model_id="claude-3-5-sonnet@20240620", system_prompt="s", user_blocks=[], max_tokens=100)


def test_vertex_backend_invokes_and_normalises_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: mock the `AnthropicVertex` client so the SDK isn't
    required to run the test. Verify the client is constructed with
    project_id + region, that `messages.create()` gets the
    iac-cartographer system/user shape, and that the SDK's response
    object is normalised into LLMResponse."""
    from iac_cartographer.llm import VertexBackend

    captured: dict[str, Any] = {}

    class _Block:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Usage:
        def __init__(self, input_tokens: int, output_tokens: int) -> None:
            self.input_tokens = input_tokens
            self.output_tokens = output_tokens

    class _Message:
        def __init__(self) -> None:
            self.content = [_Block("hello from vertex")]
            self.usage = _Usage(1234, 42)

    class _Messages:
        def create(self, **kwargs: Any) -> _Message:
            captured["create_kwargs"] = kwargs
            return _Message()

    class _FakeVertexClient:
        def __init__(self, project_id: str, region: str) -> None:
            captured["project_id"] = project_id
            captured["region"] = region
            self.messages = _Messages()

    # Lazy-import patch — VertexBackend pulls AnthropicVertex inside
    # `_get_client`. We pre-seed the attribute so the import path isn't
    # exercised at all.
    backend = VertexBackend(project_id="my-project", region="us-east5")
    backend._client = _FakeVertexClient("my-project", "us-east5")

    response = backend.invoke(
        model_id="claude-3-5-sonnet@20240620",
        system_prompt="you are a system prompt",
        user_blocks=[{"type": "text", "text": "user content"}],
        max_tokens=4096,
    )

    assert response == LLMResponse(text="hello from vertex", input_tokens=1234, output_tokens=42)
    # cache_control on the system prompt block (same idiom as the
    # other Claude-speaking backends — Vertex AI honours it too).
    assert captured["create_kwargs"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["create_kwargs"]["model"] == "claude-3-5-sonnet@20240620"
    assert captured["create_kwargs"]["messages"][0]["content"] == [
        {"type": "text", "text": "user content"},
    ]


def test_vertex_backend_handles_missing_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK responses normally have `usage`, but defensively we handle
    its absence the same way the other backends do — return zeros."""
    from iac_cartographer.llm import VertexBackend

    class _Block:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Message:
        def __init__(self) -> None:
            self.content = [_Block("no usage block")]
            self.usage = None

    class _Messages:
        def create(self, **kwargs: Any) -> _Message:
            return _Message()

    class _FakeVertexClient:
        def __init__(self, **_: Any) -> None:
            self.messages = _Messages()

    backend = VertexBackend(project_id="my-project")
    backend._client = _FakeVertexClient()

    response = backend.invoke(
        model_id="claude-3-5-sonnet@20240620",
        system_prompt="s",
        user_blocks=[],
        max_tokens=100,
    )
    assert response == LLMResponse(text="no usage block", input_tokens=0, output_tokens=0)
