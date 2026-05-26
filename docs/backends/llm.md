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

## When to use which

| Backend | Pick when |
|---|---|
| `bedrock` | You're on AWS, have IAM-bound compute (ECS task role, IRSA, Lambda role, etc.), and want zero-secret-rotation. |
| `vertex` | You're on GCP — workload identity gives the same zero-secret-rotation experience Bedrock does on AWS. Same Claude models, same prompt, no provider-shift in narrative quality. |
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
