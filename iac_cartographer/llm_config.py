"""LLM subsystem config + credential models.

`llm.py` is a single module (not a package) with many import sites and
test monkeypatch targets (`iac_cartographer.llm.invoke_bedrock_model`).
Converting it to a package would churn every one of those, so the LLM
config + credential models live in this sibling `llm_config.py` instead —
same "config beside the seam" intent, minimal blast radius.

Re-exported from `iac_cartographer.models` for back-compat.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from iac_cartographer.models import _Strict


class LLMConfig(_Strict):
    """Configuration for the LLM that writes the per-repo narrative.

    `backend` picks the provider; the rest of the fields are interpreted in
    that backend's namespace. Adding a new backend means:
      * Add a literal to the `backend` discriminator below.
      * Add an `LLMBackend` subclass in `llm.py`.
      * Wire the cli's secrets-loading + backend instantiation in `cli.py`.

    `BedrockConfig` is preserved as an alias of this class for back-compat
    with code that referenced the old name during the internal phase.
    """

    # Which LLM provider to use.
    #   "bedrock"      → AWS Bedrock InvokeModel (auth via the standard
    #                    AWS credential chain — env vars, instance
    #                    profile, IRSA, etc.). Default.
    #   "anthropic"    → Anthropic API direct (auth via an API key in
    #                    the `iac-cartographer/anthropic` secret).
    #   "vertex"       → Claude on Vertex AI / Google Cloud (auth via
    #                    GCP Application Default Credentials).
    #                    Requires `pip install iac-cartographer[gcp]`.
    #   "azure_openai" → GPT family on Azure OpenAI (auth via API key
    #                    in `iac-cartographer/azure_openai` secret, OR
    #                    Azure AD / managed identity when
    #                    azure_openai_use_aad is true).
    #                    Requires `pip install iac-cartographer[azure]`.
    #   "openai"       → GPT family via api.openai.com (or any
    #                    OpenAI-compatible gateway via openai_base_url).
    #                    Auth via API key in `iac-cartographer/openai`.
    #                    Requires `pip install iac-cartographer[openai]`.
    #   "ollama"       → Local LLM via Ollama's native /api/chat
    #                    endpoint. Zero auth by default (server bound
    #                    to localhost). No extra optional dependency.
    backend: Literal["bedrock", "anthropic", "vertex", "azure_openai", "openai", "ollama"] = "bedrock"

    # Model identifier — meaning is backend-specific.
    #   bedrock: an inference-profile ID (e.g. `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`)
    #   anthropic: a model name (e.g. `claude-sonnet-4-5-20250929`)
    # The default here is a Bedrock inference-profile that works on the
    # default backend; override when you flip backends.
    model_id: str = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

    # Max output tokens per invocation. Same meaning across backends.
    max_tokens: int = 4096

    # Increments when the system prompt changes — invalidates banner-SHA
    # history so all pages get a forced republish on the next run.
    system_prompt_version: str = "v1"

    # Bedrock-only: AWS region for the boto3 client.
    bedrock_region: str = "eu-central-1"

    # Anthropic-only: API base URL. Override to point at a proxy (e.g.
    # `https://api.anthropic.example/v1` if you front the Anthropic API
    # with an internal gateway).
    anthropic_base_url: str = "https://api.anthropic.com"

    # Vertex-only: GCP project ID hosting the Vertex AI Claude endpoint.
    # Default empty so the model still validates with default settings
    # (matching the `bedrock` + `anthropic` shape); required when
    # backend=vertex — the cli's `_build_llm_backend` raises a clean
    # ConfigError if it's missing.
    vertex_project_id: str = ""

    # Vertex-only: Vertex AI region (e.g. `europe-west1`, `us-east5`).
    # Pick a region where Claude is available — see
    # https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-claude
    # for the current list.
    vertex_region: str = "europe-west1"

    # Azure OpenAI-only: resource endpoint, e.g.
    # `https://my-resource.openai.azure.com/`. Required when
    # backend=azure_openai.
    azure_openai_endpoint: str = ""

    # Azure OpenAI-only: deployment NAME (NOT the underlying model —
    # Azure decouples them via the Studio UI). Required when
    # backend=azure_openai. `model_id` is ignored for this backend
    # because Azure routes by deployment, not by model name.
    azure_openai_deployment: str = ""

    # Azure OpenAI-only: API version. Bump as Azure releases new
    # versions with features (structured outputs, etc.).
    azure_openai_api_version: str = "2024-10-21"

    # Azure OpenAI-only: skip the `iac-cartographer/azure_openai` secret
    # and authenticate via Azure AD / managed identity instead. Picks up
    # workload identity in cluster, IMDS on Azure VMs, or `az login` ADC
    # for local dev. Recommended for cloud-native deployments — no
    # secret to rotate.
    azure_openai_use_aad: bool = False

    # OpenAI-only: API base URL. Override to point at an OpenAI-compatible
    # gateway / proxy (LiteLLM, Azure API Management routes, internal
    # LLM gateway). The SDK defaults to `https://api.openai.com/v1`.
    openai_base_url: str = "https://api.openai.com/v1"

    # OpenAI-only: org ID. Most accounts don't need this; set when your
    # billing routes through a specific org and the default doesn't.
    openai_organization: str | None = None

    # Ollama-only: server URL. Defaults to Ollama's standard local
    # bind. Set to a remote host (`http://ollama.internal:11434`) for
    # shared deployments, optionally with `ollama_extra_headers` for
    # reverse-proxy auth.
    ollama_base_url: str = "http://localhost:11434"

    # Ollama-only: per-invocation timeout. Local CPU inference can be
    # slow on big models; default is 5 min, override for tighter SLOs
    # or much longer ones.
    ollama_timeout_seconds: float = 300.0

    # Ollama-only: extra request headers (e.g. for a reverse-proxy
    # bearer token). Plain map of strings; merged into the request
    # headers as-is.
    ollama_extra_headers: dict[str, str] = Field(default_factory=dict)


# Back-compat alias. The original internal code used `BedrockConfig`; new
# code should use `LLMConfig`. Remove this alias after a release cycle.
BedrockConfig = LLMConfig


# ─── LLM credentials (one model per Secrets Manager entry) ─────────────────


class AnthropicCredentials(_Strict):
    """Anthropic API key for the `anthropic` LLM backend. Loaded only when
    `llm.backend == "anthropic"` — Bedrock deployments don't need it."""

    api_key: str


class AzureOpenAICredentials(_Strict):
    """Azure OpenAI API key for the `azure_openai` LLM backend. Loaded only
    when `llm.backend == "azure_openai"` AND `llm.azure_openai_use_aad` is
    false. AAD-authenticated deployments skip this secret entirely (auth
    flows through workload identity / managed identity instead)."""

    api_key: str


class OpenAICredentials(_Strict):
    """OpenAI API key for the `openai` LLM backend. Loaded only when
    `llm.backend == "openai"`."""

    api_key: str
