# Configuration schema

Every field, what it means, what it defaults to. The canonical schema
lives in [`iac_cartographer/models.py`](https://github.com/vakaobr/iac-cartographer/blob/main/iac_cartographer/models.py);
this page tracks it.

For a working example with every field commented, see
[`examples/config.example.yaml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/config.example.yaml).

## 1.0 API stability

**Every YAML key and CLI flag documented on this page is part of the
1.0 stable surface unless explicitly listed in the [Deprecations](#deprecations-pre-10--10)
table below.** That means:

- Renaming any of them post-1.0 requires a deliberate major version
  bump (2.0), with an alias kept for at least the full 1.x line.
- Adding new YAML keys / CLI flags / `notifications[].kind` values
  is additive and lands in any minor (1.x) release.
- Validation is strict (Pydantic `extra="forbid"`): an unknown key in
  config fails loud rather than silently ignored — so you can't
  accidentally rely on a key the schema doesn't actually expose.

The pre-1.0 aliases below still work through every 1.x release; they
are **removed in 2.0**. Migrate at any time during the 1.x window —
all aliases emit a `DeprecationWarning` so you can grep your logs for
them.

## Top-level shape

```yaml
discovery:     {...}   # where to find repos
secrets:       {...}   # where credentials + opaque parameters come from
llm:           {...}   # which LLM provider narrates
publisher:     {...}   # which publisher writes the output
confluence:    {...}   # publisher.kind == "confluence" specifics
notion:        {...}   # publisher.kind == "notion" specifics
github_wiki:   {...}   # publisher.kind == "github_wiki" specifics
markdown:      {...}   # publisher.kind == "markdown" specifics
html:          {...}   # publisher.kind == "html" specifics
json_output:   {...}   # publisher.kind == "json" specifics (pre-1.0 key: `json:`, deprecated)
slack:         {...}   # legacy single-Slack notification config (deprecated — prefer notifications:)
notifications: [...]   # modern multi-channel notification list (preferred)
```

All sections are optional — every field has a default. The minimal
valid config is `{}` (assuming you also export the right secrets and
have an LLM backend reachable).

## Deprecations (pre-1.0 → 1.0)

These pre-1.0 names still work but emit a `DeprecationWarning` and will
be **removed in 2.0**. Rename them now; each old form is accepted via an
alias so the migration is non-breaking.

| Deprecated form | Replace with | Where | Deprecated since | Removed in |
|---|---|---|---|---|
| `json:` (YAML key) | `json_output:` | top-level config section | pre-1.0 | 2.0 |
| `confluence.parent_page_id_ssm_path` | `confluence.parent_page_id_ref` | `confluence` section | pre-1.0 | 2.0 |
| legacy `slack:` block with empty `notifications:` | a `notifications:` list with a `kind: slack` entry | notification dispatch | pre-1.0 | 2.0 |
| `--no-bedrock` (CLI flag) | `--no-llm` | command line | pre-1.0 | 2.0 |

### CLI ↔ config naming intentionally kept distinct

A note on `--model` and `llm.model_id`: the CLI flag is short
(matches the conventional `--model` shorthand other tools use); the
YAML key is the field shape the schema documents
(`llm.model_id: str`). They name the same value but live in different
surfaces — neither is "wrong" or "deprecated". `--model` overrides
`llm.model_id` for the current invocation only and applies to whichever
backend is active, not just Bedrock.

The pre-1.0 `bedrock.model_id` YAML key was renamed to `llm.model_id`
when the codebase grew beyond Bedrock — the `bedrock:` block name had
already been renamed to `llm:` pre-1.0, so anyone migrating from an
ancient config touches both renames in the same pass. No alias for
the section name; `extra="forbid"` rejects a stray `bedrock:` block
with a clear error.

## `discovery`

| Field | Type | Default | What it does |
|---|---|---|---|
| `gitlab_group_ids` | `list[int]` | `[]` | GitLab group IDs (numeric). Subgroups are scanned automatically. Empty = skip GitLab. |
| `gitlab_base_url` | `str` | `"https://gitlab.com"` | Override for self-hosted GitLab. Don't include `/api/v4` — the source adds it. |
| `github_orgs` | `list[str]` | `[]` | GitHub organisation slugs. Empty = skip GitHub. |
| `github_base_url` | `str` | `"https://api.github.com"` | GitHub REST API base. Override for self-hosted GitHub Enterprise Server (include the `/api/v3` suffix; it is NOT auto-added). Public GitHub + GitHub Enterprise Cloud use the default. |
| `bitbucket_workspaces` | `list[str]` | `[]` | Bitbucket Cloud workspace slugs. Empty = skip Bitbucket. |
| `gitea_orgs` | `list[str]` | `[]` | Gitea / Forgejo organisation names. Empty = skip Gitea. |
| `gitea_base_url` | `str` | `""` | Required when `gitea_orgs` is non-empty — every Gitea / Forgejo deployment is self-hosted, no default. |
| `repos_file` | `str \| None` | `None` | Path to a curated YAML/JSON list of `RepoMetadata` records. |
| `deny_repos` | `list[str]` | `[]` | Glob patterns (fnmatch) matched against `full_name`. Applied to the merged-and-deduped result. |
| `owner_overrides` | `dict[str, str]` | `{}` | `full_name → team` overrides for the owning-team guess when the LLM's guess is wrong. |

At least one source must be configured (one of `gitlab_group_ids`,
`github_orgs`, `bitbucket_workspaces`, `gitea_orgs`, `repos_file`
must be non-empty / set). The orchestrator fails loud if all five
are empty.

## `secrets`

| Field | Type | Default | What it does |
|---|---|---|---|
| `backend` | `"aws" \| "env" \| "vault"` | `"aws"` | Picks the `SecretsProvider`. |
| `aws_region` | `str` | `"eu-central-1"` | boto3 region for Secrets Manager + SSM. AWS-only. |
| `env_dotenv_path` | `str \| None` | `None` | Path to a `.env` file to autoload before reading env vars. env-only. |
| `vault_addr` | `str` | `""` | Vault server URL. Required when `backend == "vault"`. |
| `vault_mount` | `str` | `"secret"` | KV v2 mount path. Vault-only. |
| `vault_path_prefix` | `str` | `"iac-cartographer/"` | Logical prefix under the mount. Vault-only. |
| `vault_namespace` | `str \| None` | `None` | Vault Enterprise namespace header. Vault-only. |

## `llm`

`backend` picks the LLM; the rest of the fields are interpreted in
that backend's namespace. Six backends ship today.

| Field | Type | Default | What it does |
|---|---|---|---|
| `backend` | `"bedrock" \| "anthropic" \| "vertex" \| "azure_openai" \| "openai" \| "ollama"` | `"bedrock"` | Picks the `LLMBackend`. |
| `model_id` | `str` | `"eu.anthropic.claude-sonnet-4-5-20250929-v1:0"` | Backend-specific. Bedrock: inference-profile ID. Anthropic / Vertex / OpenAI / Ollama: model name. Ignored for `azure_openai` (uses `azure_openai_deployment` instead). |
| `max_tokens` | `int` | `4096` | Max output tokens per invocation. |
| `system_prompt_version` | `str` | `"v1"` | Bump to force-republish every page on the next run (invalidates banner-SHA). |

### Backend-specific fields

| Field | Type | Default | Applies to | What it does |
|---|---|---|---|---|
| `bedrock_region` | `str` | `"eu-central-1"` | `bedrock` | AWS region. |
| `anthropic_base_url` | `str` | `"https://api.anthropic.com"` | `anthropic` | API base — override for an internal proxy. |
| `vertex_project_id` | `str \| None` | `None` | `vertex` | GCP project hosting the Claude endpoint. Required. |
| `vertex_region` | `str` | `"europe-west1"` | `vertex` | Vertex region. Must support the chosen Claude variant. |
| `azure_openai_endpoint` | `str \| None` | `None` | `azure_openai` | Azure OpenAI resource endpoint URL. Required. |
| `azure_openai_deployment` | `str \| None` | `None` | `azure_openai` | Deployment NAME from the Studio. Required (replaces `model_id`). |
| `azure_openai_api_version` | `str` | `"2024-10-21"` | `azure_openai` | Azure REST API version. Bump as new versions ship. |
| `azure_openai_use_aad` | `bool` | `false` | `azure_openai` | Skip the secret; authenticate via Azure AD / managed identity. |
| `openai_base_url` | `str` | `"https://api.openai.com/v1"` | `openai` | API base — override for OpenAI-compatible gateways (LiteLLM, vLLM, …). |
| `openai_organization` | `str \| None` | `None` | `openai` | Org ID for multi-org accounts. |
| `ollama_base_url` | `str` | `"http://localhost:11434"` | `ollama` | Ollama server URL. |
| `ollama_timeout_seconds` | `float` | `300.0` | `ollama` | HTTP timeout — local CPU inference is slow on cold starts. |
| `ollama_extra_headers` | `dict[str, str]` | `{}` | `ollama` | Headers attached to every request (for reverse-proxy auth). |

### Optional dependency groups

| Backend | Install |
|---|---|
| `bedrock` | (base install — `boto3` is already pinned) |
| `anthropic` | (base install — `anthropic` SDK is already pinned) |
| `vertex` | `pip install iac-cartographer[gcp]` |
| `azure_openai` | `pip install iac-cartographer[azure]` |
| `openai` | `pip install iac-cartographer[openai]` |
| `ollama` | (base install — uses raw `httpx`) |

## `publisher`

| Field | Type | Default | What it does |
|---|---|---|---|
| `kind` | `"confluence" \| "notion" \| "github_wiki" \| "markdown" \| "html" \| "json"` | `"confluence"` | Picks the `Publisher`. |

## `confluence`

Only honoured when `publisher.kind == "confluence"`.

| Field | Type | Default | What it does |
|---|---|---|---|
| `site` | `str` | `"your-org.atlassian.net"` | Atlassian Cloud site (no protocol, no trailing slash). |
| `space_key` | `str` | `"DOCS"` | Confluence space key (the parent page must already exist there). |
| `parent_page_id_ref` | `str` | `"/iac-cartographer/confluence-parent-id"` | Logical name of the parameter holding the parent page's numeric ID. Resolved via the active `SecretsProvider.get_parameter()` (AWS SSM path / env var / Vault path — backend-agnostic). The pre-1.0 name `parent_page_id_ssm_path` still works (deprecated; emits a warning) and will be removed in 2.0. |
| `parent_page_id` | `str \| None` | `None` | Direct override. When set, the parameter-store lookup is skipped entirely. |

## `notion`

Only honoured when `publisher.kind == "notion"`. Requires
`pip install iac-cartographer[notion]`.

| Field | Type | Default | What it does |
|---|---|---|---|
| `parent_page_id` | `str` | `""` | UUID of the parent Notion page. Operator pre-creates it and shares with the integration via the Connections menu. Required when active. |

## `github_wiki`

Only honoured when `publisher.kind == "github_wiki"`. Reuses the
existing `iac-cartographer/github` secret (no extra credential).
The wiki must already exist — visit `/wiki` and create one page
to bootstrap `<owner>/<repo>.wiki.git`.

| Field | Type | Default | What it does |
|---|---|---|---|
| `owner` | `str` | `""` | GitHub repo owner (org or user). Required when active. |
| `repo` | `str` | `""` | GitHub repo name (without the `.wiki` suffix). Required when active. |
| `commit_author_name` | `str` | `"iac-cartographer"` | Author identity baked into each commit. Override for service-account deployments. |
| `commit_author_email` | `str` | `"iac-cartographer@noreply"` | Author email. |

## `markdown`

Only honoured when `publisher.kind == "markdown"`.

| Field | Type | Default |
|---|---|---|
| `output_dir` | `str` | `"./iac-inventory"` |

`output_dir` can be overridden per-run via the `--output-dir PATH` CLI
flag — useful for ad-hoc local runs without editing the config file.

## `html`

Only honoured when `publisher.kind == "html"`.

| Field | Type | Default |
|---|---|---|
| `output_dir` | `str` | `"./iac-inventory-html"` |

Override per-run with `--output-dir PATH` on the CLI.

## `json_output`

Only honoured when `publisher.kind == "json"`. The canonical YAML key is
`json_output:` (also the Python attribute on `AppConfig`). The pre-1.0
key `json:` still works (deprecated; emits a warning) and will be
removed in 2.0 — it was renamed because the bare `json` key collided
awkwardly with the publisher-format concept and forced an alias hack.

| Field | Type | Default |
|---|---|---|
| `output_dir` | `str` | `"./iac-inventory-json"` |

Override per-run with `--output-dir PATH` on the CLI.

## `graph`

Controls the Mermaid resource-dependency diagram embedded on each
child page. Confluence (via its native Mermaid extension) and
GitHub-flavoured Markdown both render Mermaid inline; the HTML
publisher loads the Mermaid CDN bundle in `<head>` so opened-from-disk
files render too.

| Field | Type | Default | Description |
|---|---|---|---|
| `max_nodes_per_graph` | `int` | `25` | Per-diagram resource-node cap. A single Mermaid diagram with hundreds of nodes is unreadable; when the total resource count exceeds the threshold, the renderer splits into chunks of `<= max_nodes_per_graph`, keeping whole providers together within a chunk (a single oversized provider ships as its own chunk rather than splitting). |

```yaml
graph:
  max_nodes_per_graph: 25
```

Changing the threshold invalidates banner-SHAs (the rendered chunk
count is part of the page input), so the next run republishes every
page. Tune it once and leave it.

## `live_state`

Read-only overlay that layers external workspace info (Terraform Cloud
/ HCP Terraform / Terraform Enterprise) onto each repo's rendered
page — current run status, last successful apply, drift, live
resource count — plus a `warn`-level notification for any workspace
that's been in `errored` state longer than the staleness threshold.

| Field | Type | Default | Description |
|---|---|---|---|
| `backend` | `"none" \| "tfc"` | `"none"` | No-op default; flip to `tfc` to enable the TFC / HCP / TFE overlay. |
| `organization` | `str` | `""` | TFC / HCP / TFE organisation name. Required when `backend != "none"`. |
| `hostname` | `str` | `"app.terraform.io"` | API hostname. Covers TFC + HCP Terraform with the default; override for self-hosted Terraform Enterprise (e.g. `tfe.acme.internal`). |
| `workspace_mapping` | `list[{repo, workspace}]` | `[]` | Explicit per-repo → per-workspace mappings; both fields are `fnmatch`-style patterns, first-match wins. Empty falls back to the default heuristic (workspace name = last `/` segment of `repo.full_name`). |
| `staleness.enabled` | `bool` | `true` | Toggle stale failed-apply alerts. |
| `staleness.threshold_days` | `int` | `2` | Days a workspace must sit in `errored` state before an alert fires. |
| `staleness.acknowledged_stale` | `list[str]` | `[]` | `fnmatch` patterns matched against workspace names to mute alerts (deferred work, decommissioning queue). |

```yaml
live_state:
  backend: tfc
  organization: acme-org
  workspace_mapping:
    - repo: "acme-org/prod-*"
      workspace: "prod-app"
  staleness:
    enabled: true
    threshold_days: 2
    acknowledged_stale:
      - "legacy-*"
```

Requires the `iac-cartographer/tfc` secret as `{"token": "..."}` — a
read-scoped team or user API token; the overlay only ever issues GETs.

The overlay's data is **excluded from the banner-SHA** by design —
workspace state changes between iac-cartographer runs without any
change to the repo being indexed, and we don't want every page
republished on every run for that reason. The page reads as
ephemeral status, not "did the repo's structural facts change?".

Stale-apply alerts route through the configured `notifications:`
channels at `warn` level; an alert fires only when the most-recent
apply attempt errored, no newer apply is in flight (operator is on
it), and the workspace isn't matched by `acknowledged_stale`.

## `slack` (legacy single-channel — deprecated)

| Field | Type | Default |
|---|---|---|
| `channel` | `str` | `"#alerts"` |

When `notifications:` is empty (default), the dispatcher uses this
block + the `iac-cartographer/slack` secret as the sole destination
at all three severities. Existing single-Slack deployments keep
working — but this path is **deprecated** as of the 1.0 track and
emits a `DeprecationWarning`; it will be removed in 2.0. Migrate to an
explicit `notifications:` list with a `kind: slack` entry:

```yaml
notifications:
  - kind: slack
    channel: "#alerts"   # same channel, now explicit
```

## `notifications` (multi-channel — preferred)

A list of channel entries. Each entry has a `kind` discriminator
and channel-specific config; every entry carries an optional
`levels:` filter (default: all three severities).

| Channel `kind` | Credentials needed | Extra config |
|---|---|---|
| `slack` | `iac-cartographer/slack` bot token | `channel: str \| None` *(falls back to top-level `slack.channel`)* |
| `slack_webhook` | `iac-cartographer/slack_webhook` URL | none |
| `webhook` | `iac-cartographer/webhook` URL | `extra_headers: dict[str, str]` |
| `teams` | `iac-cartographer/teams` URL | none |
| `email` | `iac-cartographer/email` `{username, password}` | `smtp_host`, `smtp_port` *(default 587)*, `from_address`, `to_addresses: list[str]`, `use_tls` *(default true)*, `subject_prefix` *(default `"[iac-cartographer]"`)*. Requires `pip install iac-cartographer[email]`. |
| `sns` | none — uses the AWS credential chain | `topic_arn`, `region: str \| None` |
| `pagerduty` | `iac-cartographer/pagerduty` `{routing_key}` | none |
| `opsgenie` | `iac-cartographer/opsgenie` `{api_key}` | `region: "us" \| "eu"` *(default `"us"`)* |
| `discord` | `iac-cartographer/discord` URL | `username: str \| None`, `avatar_url: str \| None`, `thread_id: str \| None` *(post into a specific thread)* |
| `stdout` | none | `stream: "stdout" \| "stderr"` *(default `"stdout"`)*, `format: "jsonl" \| "text"` *(default `"jsonl"`)* |

Example with three channels:

```yaml
notifications:
  - kind: slack
    channel: "#infra-info"
    # levels defaults to [info, warn, error]
  - kind: teams
    levels: [error]                    # pager-style escalation
  - kind: stdout
    stream: stderr                     # CI log capture
```

Dispatcher behaviour:

- **Concurrent fanout** — every channel runs in parallel via
  `asyncio.gather(..., return_exceptions=True)`. A slow webhook doesn't
  block a fast Slack post.
- **Per-level filter** — applied before each channel's `notify()`
  runs.
- **Per-channel failure isolation** — a raising channel is logged with
  its name and the run continues. One broken destination doesn't
  sink the pipeline.
- **Empty list + no Slack secret** → silent dispatcher; safe for CI
  / air-gapped / `--dry-run` runs.
