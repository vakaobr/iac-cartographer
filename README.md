<p align="center">
  <img src="banner.png" alt="iac-cartographer banner" width="100%">
</p>

# iac-cartographer

[![CI](https://github.com/vakaobr/iac-cartographer/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/vakaobr/iac-cartographer/actions/workflows/ci.yml)
[![coverage](https://raw.githubusercontent.com/vakaobr/iac-cartographer/badges/coverage.svg)](https://github.com/vakaobr/iac-cartographer/actions/workflows/ci.yml)
[![docs](https://img.shields.io/badge/docs-vakaobr.github.io-blue)](https://vakaobr.github.io/iac-cartographer/)

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

**Just want to see what it produces?** Clone this repo and run the
zero-credentials demo — it shallow-clones three small public Terraform
repositories and writes the rendered Markdown inventory under
`./demo-output/`:

```bash
git clone https://github.com/vakaobr/iac-cartographer.git
cd iac-cartographer
pip install -e .
./examples/demo/run.sh
# Open demo-output/index.md
```

See [`examples/demo/README.md`](examples/demo/README.md) for the demo
walkthrough + variations (swap publisher to HTML, plug in a real LLM, …).

---

The fastest path from zero to a running scaffold:

```bash
pip install iac-cartographer            # or pip install -e . from a checkout
iac-cartographer --init                 # scaffolds config.yaml + .env
# edit the two files; replace `REPLACE_ME-...` placeholders
set -a; . ./iac-cartographer.env; set +a
iac-cartographer --once --dry-run --config ./iac-cartographer.config.yaml
```

`iac-cartographer --init` accepts flags to scaffold for any backend combination:

```bash
iac-cartographer --init \
  --secrets-backend env \                                # or `aws` | `vault`
  --publisher markdown \                                 # or `confluence`
  --llm anthropic \                                      # or `bedrock`
  --config-path ./iac-cartographer.config.yaml \
  --env-path    ./iac-cartographer.env
```

The longer-form quick start below explains each piece — every section maps to one or two flags on `--init`.

### 1. Install

```bash
pip install iac-cartographer            # from PyPI (recommended)
# or from a checkout, for hacking on the source:
pip install -e .

# or as a container image, no Python install needed:
docker pull ghcr.io/vakaobr/iac-cartographer:latest
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

### 3. Seed credentials

Default backend is AWS Secrets Manager — for env-var or HashiCorp Vault deployments see **Secrets backends** further down. Logical secret names (used by every backend):

| Secret name | When required | JSON shape |
|---|---|---|
| `iac-cartographer/confluence` | always | `{"email": "bot@example.com", "api_token": "ATATT..."}` |
| `iac-cartographer/gitlab` | always | `{"token": "glpat-..."}` |
| `iac-cartographer/github` | always | `{"token": "ghp_..."}` |
| `iac-cartographer/slack` | always | `{"bot_token": "xoxb-..."}` |
| `iac-cartographer/anthropic` | only when `llm.backend == "anthropic"` | `{"api_key": "sk-ant-..."}` |
| `iac-cartographer/bitbucket` | only when `discovery.bitbucket_workspaces` is non-empty | `{"access_token": "bbat-..."}` *(or `{"username": "...", "app_password": "..."}` for the legacy form)* |

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
  bitbucket_workspaces: ["acme"]                  # Bitbucket workspaces (optional)
  # repos_file: "./repos.yaml"                    # extra curated source (optional)
  deny_repos:                                     # glob patterns to skip
    - "acme-org/*-archived"
    - "acme-org/examples-*"

llm:
  # backend: bedrock (default) or anthropic
  backend: "bedrock"
  # Inference-profile ID for Bedrock, or model name for the Anthropic API.
  model_id: "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

publisher:
  # "confluence" (default) or "markdown"
  kind: "confluence"

confluence:
  site: "acme.atlassian.net"
  space_key: "DOCS"
  parent_page_id_ssm_path: "/iac-cartographer/confluence-parent-id"

# Only used when publisher.kind == "markdown"
markdown:
  output_dir: "./iac-inventory"

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
and exits. Drop-in deployment scaffolding for the three most common schedulers
lives under [`examples/runtime/`](examples/runtime/):

| File | Scheduler | When to use |
|---|---|---|
| [Helm chart](charts/iac-cartographer/) | Kubernetes `CronJob` (templated) | The recommended path for k8s. Values for schedule, namespace, image tag, secrets backend, resources, workload-identity binding. |
| [`kubernetes-cronjob.yaml`](examples/runtime/kubernetes-cronjob.yaml) | Kubernetes `CronJob` (raw manifest) | Read-and-copy reference for the raw shape — useful for learning what the Helm chart renders to, or for clusters where Helm isn't available. |
| [`github-actions.yml`](examples/runtime/github-actions.yml) | GitHub Actions `schedule` | Lightweight setup with no infrastructure to own; secrets live in the GitHub repo settings. |
| [`cron.sh`](examples/runtime/cron.sh) | Plain `cron` / `systemd-timer` | A single VM you already own. Docker-based, so no Python install needed on the host. |

For the **ECS Fargate + EventBridge Scheduler** path the original deployment
uses, the Terraform module is on the roadmap — for now the container image
([`Dockerfile`](Dockerfile)) is the canonical artefact and you wire it up
with the existing AWS recipes.

## Publishing locally instead of Confluence

Four publisher backends ship today — pick with `publisher.kind`:

| Backend | When to use |
|---|---|
| `confluence` *(default)* | You already have Confluence; you want the inventory cross-linked with the rest of your wiki. |
| `markdown` | You run a static-site generator (mkdocs / Hugo / Docusaurus / Jekyll) and want to feed the rendered Markdown into its build. Or you're committing the output to a docs repo so PRs show diffs. |
| `html` | You want **self-contained HTML files** with no build step — open them directly in a browser, zip-and-email to a stakeholder, upload to S3 + CloudFront / GitHub Pages, print to PDF for an audit. Embedded CSS, no JS, no external fonts. |
| `json` | You want a **machine-readable feed** for Backstage catalog imports, internal CMDBs, dashboards, or custom drift-detection tooling. `index.json` carries one row per repo + aggregates; per-repo files carry the full inventory. |

### Markdown layout

```
<markdown.output_dir>/
├── index.md                              # overview / index page
└── repos/
    ├── acme-org__main-cluster.md         # one file per discovered repo
    ├── acme-org__auth-service.md         # full_name slugged with "__"
    └── ...
```

Each file's first line is `<!-- iac-cartographer-sha: <sha> -->`.

### HTML layout

```
<html.output_dir>/
├── index.html
└── repos/
    ├── acme-org__main-cluster.html
    └── ...
```

Each file's head contains a `<meta name="iac-cartographer-sha" content="...">`
tag. Dark mode is automatic (CSS `prefers-color-scheme`); a `@media print`
block tightens the layout when printed.

### JSON layout

```
<json.output_dir>/
├── index.json                            # overview + aggregates
└── repos/
    ├── acme-org__main-cluster.json       # full RepoInventory per repo
    └── ...
```

`index.json` is sized for catalog-import use cases — a single fetch returns one row per repo with summary fields (`full_name`, `host`, `providers`, `environments`, `purpose`, `child_document` pointer, …) plus `aggregates.{repo_count,total_resources,top_providers}` for dashboards. Per-repo files carry the full Pydantic-serialised inventory. Top-level `iac_cartographer.sha` field carries the banner SHA.

All four publishers share the same banner-SHA idempotency contract as Confluence:
on the next run we compare the embedded SHA against the freshly-computed value
and skip the write when they match. Repos that change get rewritten; repos
that don't, don't.

## Discovery sources

Each non-empty field under `discovery:` activates one repository source.
They all run concurrently, the orchestrator dedupes by `full_name`
(first-seen wins), then `deny_repos` glob patterns are applied to the
merged result.

| Source | Activates when | What it does |
|---|---|---|
| GitLab | `gitlab_group_ids` non-empty | Blob-search `extension:tf` across each group (incl. subgroups). |
| GitHub | `github_orgs` non-empty | Code-search `extension:tf` across each org. |
| Bitbucket Cloud | `bitbucket_workspaces` non-empty | Enumerate every repo in each workspace. *(Bitbucket Cloud has no public code-search on free plans — narrow large workspaces with `deny_repos`.)* |
| Curated file | `repos_file` set | Load a YAML/JSON list of `RepoMetadata` records from disk. Useful for air-gapped runs, self-hosted VCS without an API this tool supports yet (Gitea, Forgejo, Codeberg, …), or to pin a focused subset. See [`examples/repos.example.yaml`](examples/repos.example.yaml) for the schema. |

Mix and match: configure GitLab + a curated file, or Bitbucket-only, or all four together. At least one source must be configured (the orchestrator fails loud if none are).

## Secrets backends

`secrets.backend` picks where credentials + opaque parameters (the
Confluence parent page ID, etc.) come from. Three backends ship today:

| Backend | Secrets from | Parameters from | When to use |
|---|---|---|---|
| `aws` *(default)* | AWS Secrets Manager | SSM Parameter Store | Production deployments on AWS — what the original deployment uses. |
| `env` | env var `IAC_CARTOGRAPHER_SECRET_<NAME>` (JSON) | env var `IAC_CARTOGRAPHER_PARAM_<NAME>` (plain) | CI/GitHub Actions, k8s with the secrets injected as env vars, local dev. Optional `.env` autoload. |
| `vault` | HashiCorp Vault KV v2 at `{mount}/data/{prefix}{name}` | Same path, payload must contain a `value` field | Multi-cloud / on-prem / regulated environments where Vault is already standard. |

Example `env` backend setup:

```bash
export IAC_CARTOGRAPHER_SECRET_CONFLUENCE='{"email":"bot@x.test","api_token":"ATATT..."}'
export IAC_CARTOGRAPHER_SECRET_GITLAB='{"token":"glpat-..."}'
export IAC_CARTOGRAPHER_SECRET_GITHUB='{"token":"ghp_..."}'
export IAC_CARTOGRAPHER_SECRET_SLACK='{"bot_token":"xoxb-..."}'
export IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID='123456789'
iac-cartographer --once --config /etc/iac-cartographer/config.yaml
```

`config.yaml` then declares the backend:

```yaml
secrets:
  backend: "env"
  env_dotenv_path: "/etc/iac-cartographer/.env"  # optional
```

Vault example:

```yaml
secrets:
  backend: "vault"
  vault_addr: "https://vault.example.com"
  vault_mount: "secret"
  vault_path_prefix: "iac-cartographer/"
```

```bash
export VAULT_TOKEN="$(vault login -method=oidc -token-only)"
vault kv put secret/iac-cartographer/gitlab token=glpat-...
vault kv put secret/iac-cartographer/confluence-parent-id value=123456789
iac-cartographer --once --config /etc/iac-cartographer/config.yaml
```

For the Confluence parent page ID specifically: when storing a non-secret integer in an external parameter store feels like overkill, set `confluence.parent_page_id` directly in the YAML and the parameter-store lookup is skipped entirely.

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

### Shipped

The four pluggable seams that were the original Phase 2 push:

* **Publishers** — Confluence, local Markdown, standalone HTML, machine-readable JSON.
* **LLM** — AWS Bedrock, Anthropic API direct.
* **Discovery** — GitLab groups, GitHub orgs, Bitbucket workspaces, curated YAML/JSON file.
* **Secrets** — AWS Secrets Manager + SSM, process env vars (with `.env` autoload), HashiCorp Vault KV v2.

Plus the Phase 3 distribution + onboarding wins:

* **PyPI release workflow** — OIDC trusted publishing, tag-driven, version-match guard. Cuts a release on `git tag v*`.
* **Container image** — `ghcr.io/vakaobr/iac-cartographer` with cosign keyless signing + SPDX SBOM on every tag push.
* **Helm chart** — [`charts/iac-cartographer/`](charts/iac-cartographer/) for k8s CronJob deployments with workload-identity bindings.
* **`iac-cartographer --init` scaffolder** — interactive starter `config.yaml` + `.env` for any backend combination.
* **Zero-credentials demo** — `./examples/demo/run.sh` clones three public Terraform repos and produces real Markdown output without any tokens.
* **Docs site** — mkdocs-material at [iac-cartographer.andersonleite.me](https://iac-cartographer.andersonleite.me/).

### Coming next

Open follow-ups, roughly ordered by user-impact / effort ratio. Issues welcome on any of these — pick one and open one to claim it before sending a PR.

* **New publishers** — Notion, GitHub Wiki.
* **New LLM backends** — OpenAI, Ollama (local).
* **New discovery sources** — Gitea / Forgejo native APIs (use the file source in the meantime).
* **Terraform module** — for the ECS Fargate + EventBridge deployment path the project was extracted from.
* **Multi-arch container image** — `linux/arm64` alongside `linux/amd64` for Apple Silicon devs + arm64 k8s nodes.
* **`--diff <prev-output>` mode** — between-run change summary ("3 new repos, 1 archived, AWS provider bumped to 6.5 in 2 repos") for richer Slack posts.
* **`lint` subcommand / pre-commit hook** — run the extractor against a single repo and fail on missing `required_providers`, unpinned versions, etc. Different mode from the scheduled publish.
* **Versioned docs** — `mike` plugin so the docs site shows per-release docs once releases start cutting.

## Contributing

Issues and PRs welcome. The codebase is intentionally small and well-tested
(see the coverage badge above); pick a roadmap item or open an issue describing the
shape of the change before sending a PR for anything non-trivial.

## License

MIT — see [LICENSE](LICENSE).
