"""LLM backend abstraction.

The narrator only needs to ask the model "given this system prompt and
these user blocks, give me back text + token counts". Anything more
specific to a particular provider (auth, endpoint, response shape) lives
behind the `LLMBackend` ABC defined here.

Two implementations ship by default:

  * `BedrockBackend` — invokes Claude models via AWS Bedrock's
    InvokeModel API. Auth comes from boto3's standard credential chain
    (env, instance profile, etc.); no API key handling.
  * `AnthropicBackend` — invokes Claude models against the Anthropic API
    directly (https://api.anthropic.com/v1/messages). Auth via an API
    key passed at construction.

Both speak the Anthropic Messages API request format internally; the
backends translate to/from the provider-specific quirks (Bedrock embeds
the version in the body and elides `model`; Anthropic direct uses a
header version and includes `model`).

Adding a new backend (OpenAI, Ollama, etc.) means subclassing
`LLMBackend` and overriding `invoke()`.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from iac_cartographer.aws import invoke_bedrock_model

logger = logging.getLogger("iac_cartographer.llm")


@dataclass(frozen=True)
class LLMResponse:
    """Normalised response shape every backend must produce.

    `text` is the concatenation of every text block in the assistant
    response (most models emit one block; we join multiples for robustness).
    Returns empty string if the model returned no text content.
    """

    text: str
    input_tokens: int
    output_tokens: int


class LLMBackend(ABC):
    """Interface every LLM provider must implement.

    Implementations should:
      * Translate the system prompt + user blocks into the provider's
        native request shape.
      * Handle transport-level errors by raising — the narrator's
        retry-once-then-skip orchestration assumes a raised exception
        means "this attempt failed; try again or give up".
      * Return token counts when the provider supplies them, or zeros
        when it doesn't (don't lie / estimate).
    """

    @abstractmethod
    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_blocks: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        """Send one prompt to the model and return the parsed response.

        `user_blocks` is a list of `{"type": "text", "text": "..."}` dicts
        in Anthropic Messages API shape — same format Bedrock and the
        Anthropic API both accept, so most backends can pass it through
        verbatim. `system_prompt` is a single string; the backend is
        responsible for wrapping it appropriately (e.g. with
        `cache_control` for prompt caching where supported).
        """


def _extract_text(content: list[dict[str, Any]] | None) -> str:
    """Concatenate text blocks from an Anthropic-style `content` array."""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)


# ─── AWS Bedrock ──────────────────────────────────────────────────────


class BedrockBackend(LLMBackend):
    """Invoke Claude models via AWS Bedrock's InvokeModel API.

    Authentication comes from boto3's standard credential chain. The
    request body uses Bedrock's wrapped Anthropic format:
    `anthropic_version: "bedrock-2023-05-31"` in the body, no `model`
    field (that's encoded in the `modelId` parameter)."""

    def __init__(self, region: str = "eu-central-1") -> None:
        self._region = region

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_blocks: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            # `cache_control: ephemeral` on the system block lets Bedrock
            # serve repeated system-prompt tokens from cache at ~10% of
            # the input-token price — a big saver when the run hits N
            # repos with the same system prompt.
            "system": [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [{"role": "user", "content": user_blocks}],
        }
        response = invoke_bedrock_model(model_id, body, region=self._region)

        if not isinstance(response, dict):
            return LLMResponse(text="", input_tokens=0, output_tokens=0)

        usage = response.get("usage", {}) if isinstance(response.get("usage"), dict) else {}
        return LLMResponse(
            text=_extract_text(response.get("content")),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
        )


# ─── Anthropic direct ─────────────────────────────────────────────────


class AnthropicBackend(LLMBackend):
    """Invoke Claude models against the Anthropic API directly.

    Authentication via `x-api-key` header. Request body is the Messages
    API format: `model` is included in the body (not the URL), version
    is sent as a header, and `cache_control` works the same way as on
    Bedrock. See https://docs.anthropic.com/claude/reference/messages_post.
    """

    DEFAULT_BASE_URL = "https://api.anthropic.com"
    ANTHROPIC_VERSION = "2023-06-01"
    DEFAULT_TIMEOUT_S = 60.0

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not api_key:
            raise ValueError("AnthropicBackend: api_key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_blocks: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        body = {
            "model": model_id,
            "max_tokens": max_tokens,
            "system": [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [{"role": "user", "content": user_blocks}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        # Synchronous httpx call — the narrator already lives off the
        # asyncio loop (it's invoked from `asyncio.to_thread`).
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{self._base_url}/v1/messages",
                content=json.dumps(body),
                headers=headers,
            )
        resp.raise_for_status()
        data = resp.json()

        usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
        # Anthropic's usage shape: input_tokens, output_tokens,
        # cache_creation_input_tokens, cache_read_input_tokens. We only
        # report the headline two for parity with Bedrock; consumers can
        # extend later if cache analytics matter.
        return LLMResponse(
            text=_extract_text(data.get("content")),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
        )


__all__ = [
    "AnthropicBackend",
    "BedrockBackend",
    "LLMBackend",
    "LLMResponse",
]
