# iac-cartographer

> Fleet-level documentation for your Terraform / IaC estate.

`iac-cartographer` discovers every Terraform repository across your GitLab
groups and GitHub organisations, extracts structural facts with
[`terraform-docs`](https://terraform-docs.io) (plus an HCL parser fallback for
fields `terraform-docs` strips), asks a Claude model on AWS Bedrock to write a
short purpose summary for each repo, and publishes a parent + child page
hierarchy to Confluence Cloud. Pages republish only when the underlying content
changes (banner-SHA short-circuit), so it's safe to run as often as you like.

```
GitLab + GitHub APIs ──► clone shallow ──► terraform-docs per .tf dir ──►
                                                       │
                              ┌────────────────────────┴────────────────────────┐
                              ▼                                                 ▼
                          required_providers                          Claude on Bedrock
                          parsed from HCL                             (narrative summary)
                              │                                                 │
                              └───────────────► aggregate ◄────────────────────┘
                                                    │
                                                    ▼
                                              render ADF
                                                    │
                                                    ▼
                                          Confluence v2 API
                                                    │
                                                    ▼
                                          Slack #channel (info/warn/error)
```

## Why

* **Self-onboarding for engineers.** A new hire opens one Confluence page and
  sees the entire IaC estate — what each repo does, which providers, which
  modules, last commit and author.
* **Always current.** Re-runs are idempotent and refresh on a schedule of your
  choosing. The page never lies for long.
* **Fix-it signals are visible.** Repos missing a `required_providers` block
  render with a `(not declared)` marker; repos with unpinned versions get
  `(unpinned)`. The page surfaces problems instead of hiding them.
* **Cheap.** Single-shot Bedrock spend per run is typically well under €1 for
  a small fleet (30-ish repos with a Sonnet 4.5 default + prompt caching).

## Status

`v0.1.0` — extracted from a working production deployment at a single
organisation. Public-facing edges are still rough; expect some hardcoded
assumptions (AWS Bedrock for the LLM, AWS Secrets Manager + SSM Parameter
Store for credentials/config). Pluggable backends are on the roadmap.

## Quick start

### 1. Install

```bash
pip install -e .          # from a checkout
# or once published:
# pip install iac-cartographer
```

Requirements:
* Python 3.12+
* [`terraform-docs`](https://terraform-docs.io) on your PATH
* A Confluence Cloud space you can publish to
* One of:
  * **AWS credentials** with `bedrock:InvokeModel` on a Claude model (default — `llm.backend: bedrock`), or
  * **An Anthropic API key** (`llm.backend: anthropic` — for deployments without Bedrock access)

### 2. Pre-create a parent Confluence page

Create an empty Confluence page in your target space (e.g. `DOCS`). It will
become the overview / index. Note the numeric page ID from the URL
(`/wiki/spaces/DOCS/pages/123456789/...` → `123456789`).

### 3. Seed credentials in AWS Secrets Manager

Four required secrets, plus one optional depending on the LLM backend:

| Secret name | When required | JSON shape |
|---|---|---|
| `iac-cartographer/confluence` | always | `{"email": "bot@example.com", "api_token": "ATATT..."}` |
| `iac-cartographer/gitlab` | always | `{"token": "glpat-..."}` |
| `iac-cartographer/github` | always | `{"token": "ghp_..."}` |
| `iac-cartographer/slack` | always | `{"bot_token": "xoxb-..."}` |
| `iac-cartographer/anthropic` | only when `llm.backend == "anthropic"` | `{"api_key": "sk-ant-..."}` |

The Confluence token must be a **legacy unscoped** API token (the plain
"Create API token" form at id.atlassian.com, not "Create API token with
scopes" — the latter requires an installed OAuth app on the workspace).

### 4. Seed the config in AWS SSM Parameter Store

```yaml
# Path: /iac-cartographer/config (SecureString)
discovery:
  gitlab_group_ids: [15]                          # GitLab group IDs to scan
  gitlab_base_url: "https://gitlab.example.com"   # omit for gitlab.com
  github_orgs: ["acme-org"]                       # GitHub orgs to scan
  deny_repos:                                     # glob patterns to skip
    - "acme-org/*-archived"
    - "acme-org/examples-*"

llm:
  # backend: bedrock (default) or anthropic
  backend: "bedrock"
  # Inference-profile ID for Bedrock, or model name for the Anthropic API.
  model_id: "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

confluence:
  site: "acme.atlassian.net"
  space_key: "DOCS"
  parent_page_id_ssm_path: "/iac-cartographer/confluence-parent-id"

slack:
  channel: "#alerts"
```

See [`examples/config.example.yaml`](examples/config.example.yaml) for the
full set of fields with comments.

Also seed the parent page ID:

```bash
aws ssm put-parameter \
  --name "/iac-cartographer/confluence-parent-id" \
  --value "123456789" --type String
```

### 5. Run it

```bash
# Dry-run locally (no Confluence writes, no Slack messages, placeholder narratives)
iac-cartographer --once --dry-run --no-bedrock --config /path/to/config.yaml

# Production single shot (reads config from SSM by default)
iac-cartographer --once

# Restrict to a subset of repos
iac-cartographer --once --repos acme-org/main-cluster,acme-org/auth-service

# Use a cheaper model for validation
iac-cartographer --once --model eu.anthropic.claude-haiku-4-5-20251001-v1:0
```

## How to run it on a schedule

The CLI is a one-shot — `iac-cartographer --once` runs the whole pipeline once
and exits. Pair it with any scheduler:

* **ECS Fargate + EventBridge Scheduler** — what the original deployment uses.
  Container image is in this repo's `Dockerfile`.
* **Kubernetes CronJob** — the same image, a CronJob manifest with the
  appropriate IAM annotations (IRSA / Pod Identity).
* **GitHub Actions schedule** — `schedule:` workflow that installs the package
  and runs it.
* **Plain `cron`** — Docker + a `0 6 * * 1 docker run iac-cartographer --once`
  line.

Terraform examples for the ECS Fargate setup are on the roadmap; for now the
container image is the canonical artefact.

## Reading the output

On the Confluence pages you'll see a few placeholders worth knowing:

| Marker | Meaning |
|---|---|
| `<canonical> (not declared)` in Source | The repo provisions this provider without a matching `terraform { required_providers { ... } }` block. The canonical source is inferred from a curated map. **This is a fix-it signal** — modern Terraform fails `terraform init` for any non-Hashicorp namespace lacking the declaration. |
| `(not declared — unknown to inventory)` in Source | Same as above, except the provider isn't in our curated map. PRs adding new providers welcome. |
| `(unpinned)` in Version | No `version = "..."` constraint declared. Worth pinning. |
| `(Narrative summary unavailable for this run...)` in Purpose | Bedrock returned an error or invalid JSON for this repo. Structural facts (providers, resources, modules) are unaffected. Auto-retries once per run. |
| `:warning: Narrative review needed (AI-H1...)` on Slack | A repo's narrative contained a prompt-injection trigger phrase. Narrative is dropped from the page; structural facts publish unchanged. Inspect the source repo for unusual README content. |

## Roadmap

* **Pluggable publishers** — Confluence today; Notion, GitHub Wiki,
  local-Markdown next.
* **Pluggable LLM backend** — ✅ Bedrock + Anthropic-direct shipped; OpenAI
  and Ollama are next.
* **Pluggable discovery** — GitLab + GitHub today; Bitbucket and a
  `--repos-from-file` source.
* **Pluggable secrets/config** — AWS Secrets Manager + SSM today; environment
  variables, HashiCorp Vault, plain dotenv.
* **Terraform module** — for the ECS Fargate deployment path.
* **PyPI release** — once the pluggable interfaces stabilise.

## Contributing

Issues and PRs welcome. The codebase is intentionally small and well-tested
(214 tests, 87% coverage); pick a roadmap item or open an issue describing the
shape of the change before sending a PR for anything non-trivial.

## License

MIT — see [LICENSE](LICENSE).
