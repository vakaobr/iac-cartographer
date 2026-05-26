# LLM providers

The narrator phase asks a Claude model to produce a one-paragraph purpose
summary + environment list + notable patterns for each repo. The
`llm.backend` discriminator picks where the API call goes.

## Bedrock (default)

```yaml
llm:
  backend: bedrock
  model_id: "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
  max_tokens: 4096
  bedrock_region: eu-central-1
```

Authentication uses the standard AWS credential chain — env vars,
instance profile, IRSA / Workload Identity on EKS, etc. No
`iac-cartographer/bedrock` secret needed.

`model_id` is a Bedrock **inference-profile ID**, not the raw model
name — the `eu.` prefix routes through the cross-region inference
profile that lives in `eu-central-1`. Other regions and Claude variants
work the same way; consult AWS Bedrock's
[inference profiles documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles.html)
for the catalog.

## Anthropic API direct

```yaml
llm:
  backend: anthropic
  model_id: "claude-sonnet-4-5-20250929"
  max_tokens: 4096
  anthropic_base_url: "https://api.anthropic.com"
```

Requires the `iac-cartographer/anthropic` secret:

```json
{"api_key": "sk-ant-..."}
```

`anthropic_base_url` defaults to `https://api.anthropic.com`. Override
to point at an internal proxy / gateway when your network policy
requires egress to be funnelled.

## Vertex AI (Claude on Google Cloud)

```yaml
llm:
  backend: vertex
  model_id: "claude-sonnet-4-5@20240620"   # publisher-prefixed Claude name
  max_tokens: 4096
  vertex_project_id: "my-gcp-project"
  vertex_region: "europe-west1"
```

**Requires the `[gcp]` optional dependency group:**

```bash
pip install 'iac-cartographer[gcp]'
```

That pulls in `anthropic[vertex]` (official SDK with GCP auth) — the
backend lazy-imports it on first invoke and fails loud with a
pip-install hint if it's missing.

Authentication uses **Google Application Default Credentials**:

- **In cluster (GKE, Cloud Run Job runtime SA)** — workload identity
  binding picks up the token automatically. No extra config.
- **Workload Identity Federation from other clouds** (EKS pod →
  GCP project, etc.) — same flow once federation is set up.
- **Local dev** — `gcloud auth application-default login` populates ADC.
- **Batch hosts** — service account key file mounted at the path
  pointed to by `GOOGLE_APPLICATION_CREDENTIALS`.

No `iac-cartographer/vertex` secret needed — auth is identity-based,
not API-key-based.

`model_id` uses Vertex AI's publisher-prefixed naming
(`claude-sonnet-4-5@20240620`, not the raw `claude-sonnet-4-5-20250929`).
See [the Vertex AI Claude docs](https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-claude)
for the current model catalog + supported regions.

## Azure OpenAI (GPT family)

First non-Claude backend. Azure doesn't host Claude — this backend
talks to GPT-4o / GPT-4-Turbo via Azure OpenAI Service.

```yaml
llm:
  backend: azure_openai
  azure_openai_endpoint: "https://my-resource.openai.azure.com/"
  azure_openai_deployment: "my-gpt4"          # NAME from Azure OpenAI Studio
  azure_openai_api_version: "2024-10-21"
  azure_openai_use_aad: false                 # see auth section
  max_tokens: 4096
```

**Requires the `[azure]` optional dependency group:**

```bash
pip install 'iac-cartographer[azure]'
```

That pulls in `openai` (the official SDK) and `azure-identity` (for
AAD auth, when enabled).

### Auth

Two modes:

- **API key** (default) — operator stores it in the
  `iac-cartographer/azure_openai` secret:

  ```json
  {"api_key": "..."}
  ```

  Same shape across all secrets backends (AWS Secrets Manager, env
  vars, Vault).

- **Azure AD / managed identity** — set `azure_openai_use_aad: true`.
  The SDK uses `azure.identity.DefaultAzureCredential` which picks up
  workload identity in cluster, IMDS on Azure VMs, or `az login` ADC
  for local dev. The `iac-cartographer/azure_openai` secret is skipped
  entirely — no key to rotate.

  AAD-bound identities need the **Cognitive Services OpenAI User**
  role assignment on the Azure OpenAI resource.

### `model_id` vs `azure_openai_deployment`

Azure binds models to deployments through the Studio UI — the
deployment name is what routes the request, not the underlying model
identifier. iac-cartographer's `llm.model_id` is ignored for this
backend; `llm.azure_openai_deployment` is what's used.

If both are set and they differ, a DEBUG-level log line surfaces the
discrepancy so operators who copy-paste configs from another backend
notice.

### Narrative quality vs Claude

The narrator prompt is currently Claude-tuned (XML tags, structured
output instructions). GPT-4 handles it but emits invalid JSON more
often than Claude. The backend sets
`response_format={"type": "json_object"}` to nudge GPT-4 into valid
JSON; the narrator's retry-once-then-skip path catches the rest.

If you see consistent schema-validation failures, consider switching
to one of the Claude backends (Bedrock / Anthropic / Vertex) for
better narrative quality. The publisher output is structurally
complete on schema failure (the page renders with `narrative=None`).

## OpenAI direct

Sibling of `azure_openai` — same SDK, same prompt scaffolding, same
`json_object` response_format. The differences are routing-only:
auth via API key (not AAD), `model_id` actually drives the request
(no deployment indirection), endpoint base URL overridable for
OpenAI-compatible gateways and proxies.

```yaml
llm:
  backend: openai
  model_id: "gpt-4o"                           # OpenAI model name
  max_tokens: 4096
  openai_base_url: "https://api.openai.com/v1"   # override for gateways
  # openai_organization: "org-..."             # rarely needed
```

**Requires the `[openai]` optional dependency group:**

```bash
pip install 'iac-cartographer[openai]'
```

Pulls in the same `openai` SDK that the `[azure]` extra uses — if you
already installed `[azure]` for the Azure OpenAI backend, you don't
need `[openai]` too.

Requires the `iac-cartographer/openai` secret:

```json
{"api_key": "..."}
```

### Custom endpoint / OpenAI-compatible gateways

The `openai_base_url` field defaults to `https://api.openai.com/v1`
but accepts any OpenAI-compatible endpoint. Useful for:

- **LiteLLM proxies** that aggregate multiple LLM providers behind a
  single OpenAI-shaped interface.
- **Internal LLM gateways** that add rate limiting, audit logging,
  or PII redaction in front of the upstream provider.
- **Self-hosted models with an OpenAI-compatible REST API** —
  vLLM, llama.cpp's server, OpenLLM, etc. (For Ollama specifically,
  use `llm.backend: ollama` once that ships — it has zero-auth
  defaults that fit Ollama better.)

The same narrative-quality caveat applies as for Azure OpenAI: GPT-4
emits invalid JSON slightly more often than Claude, and the narrator's
retry-once path catches the rest.

## When to use which

| Backend | Pick when |
|---|---|
| `bedrock` | You're on AWS, have IAM-bound compute (ECS task role, IRSA, Lambda role, etc.), and want zero-secret-rotation. |
| `vertex` | You're on GCP — workload identity gives the same zero-secret-rotation experience Bedrock does on AWS. Same Claude models, same prompt, no provider-shift in narrative quality. |
| `azure_openai` | You're on Azure and need everything to stay in-tenant. AAD mode gives the same zero-secret-rotation experience. |
| `openai` | You want GPT-4 without the Azure-specific deployment shape, or you're routing through a LiteLLM / internal gateway / self-hosted OpenAI-compatible endpoint. |
| `anthropic` | You're not on AWS, you don't have Bedrock model access (it's per-account opt-in), or you want lower latency from the EU. |

## Skipping the LLM entirely

`--no-bedrock` (named historically; it applies to either backend) swaps
in a placeholder narrative for every repo:

```bash
iac-cartographer --once --no-bedrock --config /path/to/config.yaml
```

Useful for:

- **Dry-run validation** of discovery + extraction + publishing without
  burning LLM spend.
- **Air-gapped runs** where no LLM is reachable. The published pages
  still carry every structural fact (providers, modules, resources,
  inputs, outputs) — just with a placeholder where the prose would be.
- **The bundled demo** (see [Quick start](../quickstart.md)) — runs
  `--no-bedrock` so no API keys are needed.

The narrator also short-circuits to a placeholder on a real Bedrock /
Anthropic failure (rate limit, validation error, prompt-injection
detection). Per-repo isolation: one bad narrative doesn't sink the run.

## Prompt-injection defense

Repo content is fundamentally untrusted — a `README.md` or commit
message can contain "ignore previous instructions" payloads. The
narrator runs a triggered-phrase scan over the model output before
accepting it; matches trigger a Slack-warned review queue entry
(`AI-H1 — possible prompt injection`) and the repo's narrative is
replaced with a placeholder.

The actual scan is in `narrator.detect_suspicious_phrases` —
deliberately conservative (curated against real production data) so
generic IaC vocabulary doesn't false-positive.
