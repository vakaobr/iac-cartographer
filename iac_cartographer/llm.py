"""LLM backend abstraction.

The narrator only needs to ask the model "given this system prompt and
these user blocks, give me back text + token counts". Anything more
specific to a particular provider (auth, endpoint, response shape) lives
behind the `LLMBackend` ABC defined here.

Four implementations ship by default:

  * `BedrockBackend`     — Claude on AWS Bedrock. Auth via boto3
    credential chain; no API key handling.
  * `AnthropicBackend`   — Claude on api.anthropic.com. Auth via API key.
  * `VertexBackend`      — Claude on Vertex AI / Google Cloud. Auth via
    Google ADC. Requires the `[gcp]` optional dependency group.
  * `AzureOpenAIBackend` — GPT models on Azure OpenAI. First non-Claude
    backend. Auth via API key or Azure AD / managed identity. Requires
    the `[azure]` optional dependency group.

The three Claude backends (Bedrock, Anthropic, Vertex) speak the
Anthropic Messages API request format internally; the backends
translate to/from provider-specific quirks. AzureOpenAIBackend speaks
the OpenAI chat.completions format and flattens
`user_blocks` accordingly.

Adding a new backend (OpenAI direct, Ollama, etc.) means subclassing
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


# ─── Vertex AI (Claude on Google Cloud) ──────────────────────────────


class VertexBackend(LLMBackend):
    """Invoke Claude models on Vertex AI (Google Cloud).

    Auth: Google Application Default Credentials. On a workload identity
    binding (GKE, Cloud Run Job's runtime SA, Workload Identity Federation
    from another cloud) no extra config is needed — the SDK picks up the
    in-cluster token. For local dev, `gcloud auth application-default
    login` populates ADC.

    Implementation: thin wrapper around the official Anthropic SDK's
    `AnthropicVertex` client. The SDK handles GCP auth + endpoint
    construction + retry-on-5xx; the existing `AnthropicBackend` chose
    raw httpx for minimalism but Vertex's auth flow (OAuth2 token + URL
    composition) is too fiddly to redo by hand without leaning on
    `google-auth`.

    Requires the `[gcp]` optional dependency group:

        pip install 'iac-cartographer[gcp]'
    """

    DEFAULT_REGION = "europe-west1"

    def __init__(self, project_id: str, *, region: str = DEFAULT_REGION) -> None:
        if not project_id:
            raise ValueError("VertexBackend: project_id is required")
        self._project_id = project_id
        self._region = region
        self._client: Any | None = None  # lazy — only instantiated on first invoke

    def _get_client(self) -> Any:
        """Lazy-import the Anthropic Vertex SDK on first use.

        Failure here surfaces as a clean `LLMBackendImportError` so the
        operator gets a pip-install hint instead of a generic
        `ModuleNotFoundError` deep in the call stack."""
        if self._client is not None:
            return self._client
        try:
            from anthropic import AnthropicVertex  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LLMBackendImportError(
                "VertexBackend requires the [gcp] optional dependency group. "
                "Install with: pip install 'iac-cartographer[gcp]'"
            ) from exc
        self._client = AnthropicVertex(project_id=self._project_id, region=self._region)
        return self._client

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_blocks: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        client = self._get_client()
        # AnthropicVertex's `messages.create()` accepts the same
        # request shape as the direct Anthropic API; the SDK handles
        # the `vertex-2023-10-16` version header + URL composition.
        message = client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": user_blocks}],
        )

        # SDK returns a Pydantic-modelled response, not a raw dict —
        # extract via attributes. `content` is a list of typed blocks;
        # text lives on `.text` for TextBlock entries.
        parts: list[str] = []
        for block in getattr(message, "content", None) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        usage = getattr(message, "usage", None)
        return LLMResponse(
            text="".join(parts),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0) if usage else 0,
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
        )


# ─── Azure OpenAI (GPT family via Azure) ─────────────────────────────


class AzureOpenAIBackend(LLMBackend):
    """Invoke OpenAI models hosted on Azure (GPT-4o, GPT-4-Turbo, etc.).

    First non-Claude backend in iac-cartographer — Azure doesn't host
    Claude. The narrator prompt is currently tuned for Claude (XML tags,
    structured-output instructions); GPT-4 handles it but has slightly
    higher schema-validation failure rates than Claude. The
    `response_format={"type": "json_object"}` setting nudges the model
    into producing valid JSON, and the narrator's existing retry-once
    path catches the rest.

    Auth: two modes:
      * **API key** — operator stores it in `iac-cartographer/azure_openai`
        (matching the env / vault / aws secrets backend pattern). Default.
      * **Azure AD / Managed Identity** — set `use_aad=True`. The SDK
        uses `azure.identity.DefaultAzureCredential` which picks up
        workload identity in cluster, IMDS on Azure VMs, or `az login`
        ADC for local dev. Recommended for cloud-native deployments —
        no secret to rotate.

    Endpoint config:
      * `endpoint`    — `https://<your-resource>.openai.azure.com/`
      * `deployment`  — the deployment NAME you created in Azure OpenAI
        Studio (NOT the underlying model — Azure decouples them).
      * `api_version` — defaults to a recent stable; bump as Azure
        releases new versions with features.

    Requires the `[azure]` optional dependency group:

        pip install 'iac-cartographer[azure]'
    """

    DEFAULT_API_VERSION = "2024-10-21"

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        *,
        api_key: str | None = None,
        use_aad: bool = False,
        api_version: str = DEFAULT_API_VERSION,
    ) -> None:
        if not endpoint:
            raise ValueError("AzureOpenAIBackend: endpoint is required")
        if not deployment:
            raise ValueError("AzureOpenAIBackend: deployment is required")
        if not use_aad and not api_key:
            raise ValueError("AzureOpenAIBackend: either api_key or use_aad=True is required")
        self._endpoint = endpoint.rstrip("/")
        self._deployment = deployment
        self._api_key = api_key
        self._use_aad = use_aad
        self._api_version = api_version
        self._client: Any | None = None  # lazy

    def _get_client(self) -> Any:
        """Lazy-import `openai` + optionally `azure-identity`. Failures
        surface as `LLMBackendImportError` with a pip-install hint."""
        if self._client is not None:
            return self._client
        try:
            from openai import AzureOpenAI  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LLMBackendImportError(
                "AzureOpenAIBackend requires the [azure] optional dependency group. "
                "Install with: pip install 'iac-cartographer[azure]'"
            ) from exc

        if self._use_aad:
            try:
                from azure.identity import (  # type: ignore[import-not-found]
                    DefaultAzureCredential,
                    get_bearer_token_provider,
                )
            except ImportError as exc:
                raise LLMBackendImportError(
                    "AzureOpenAIBackend with use_aad=True requires azure-identity. "
                    "Install with: pip install 'iac-cartographer[azure]'"
                ) from exc
            # Bearer token provider refreshes the AAD token automatically;
            # the SDK calls it on every request.
            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(),
                "https://cognitiveservices.azure.com/.default",
            )
            self._client = AzureOpenAI(
                azure_endpoint=self._endpoint,
                api_version=self._api_version,
                azure_ad_token_provider=token_provider,
            )
        else:
            self._client = AzureOpenAI(
                azure_endpoint=self._endpoint,
                api_version=self._api_version,
                api_key=self._api_key,
            )
        return self._client

    def invoke(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_blocks: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        # `model_id` is ignored by Azure OpenAI in favour of the
        # deployment name passed at construction — Azure binds models
        # to deployments via the Studio UI, not via the request body.
        # Log the discrepancy so operators who set both notice.
        if model_id and model_id != self._deployment:
            logger.debug(
                "azure_openai: llm.model_id=%r ignored; deployment=%r drives the routing",
                model_id,
                self._deployment,
            )

        # Flatten Anthropic-style user_blocks into one OpenAI message.
        # Both formats are "list of text blocks" semantically; OpenAI
        # chat.completions just expects them concatenated under
        # `content: str`.
        user_content = "".join(b.get("text", "") for b in user_blocks if b.get("type") == "text")

        client = self._get_client()
        completion = client.chat.completions.create(
            model=self._deployment,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            # Nudge GPT-4 into emitting parseable JSON. The narrator's
            # Pydantic validation gives the second layer of safety —
            # `json_object` mode guarantees syntactic validity but not
            # schema compliance.
            response_format={"type": "json_object"},
        )

        # OpenAI SDK returns Pydantic-modelled response objects.
        choices = getattr(completion, "choices", None) or []
        text = ""
        if choices:
            message = getattr(choices[0], "message", None)
            text = getattr(message, "content", None) or ""

        usage = getattr(completion, "usage", None)
        # OpenAI's token field names differ from Anthropic's
        # (prompt_tokens vs input_tokens; completion_tokens vs
        # output_tokens). Normalise here so the narrator + outcome
        # reporting stay backend-agnostic.
        return LLMResponse(
            text=text,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
        )


class LLMBackendImportError(ImportError):
    """Raised when a backend's optional dependency group isn't installed.

    Carries a pip-install hint in the message so the operator knows how
    to fix it without spelunking through the source."""


__all__ = [
    "AnthropicBackend",
    "AzureOpenAIBackend",
    "BedrockBackend",
    "LLMBackend",
    "LLMBackendImportError",
    "LLMResponse",
    "VertexBackend",
]
