# Configuration schema

Every field, what it means, what it defaults to. The canonical schema
lives in [`iac_cartographer/models.py`](https://github.com/vakaobr/iac-cartographer/blob/main/iac_cartographer/models.py);
this page tracks it.

For a working example with every field commented, see
[`examples/config.example.yaml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/config.example.yaml).

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

| Deprecated | Replace with | Where |
|---|---|---|
| `json:` (YAML key) | `json_output:` | top-level config section |
| `confluence.parent_page_id_ssm_path` | `confluence.parent_page_id_ref` | `confluence` section |
| legacy `slack:` block + empty `notifications:` | a `notifications:` list with a `kind: slack` entry | notifications |
| `--no-bedrock` (CLI flag) | `--no-llm` | command line |

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

## `html`

Only honoured when `publisher.kind == "html"`.

| Field | Type | Default |
|---|---|---|
| `output_dir` | `str` | `"./iac-inventory-html"` |

## `json_output`

Only honoured when `publisher.kind == "json"`. The canonical YAML key is
`json_output:` (also the Python attribute on `AppConfig`). The pre-1.0
key `json:` still works (deprecated; emits a warning) and will be
removed in 2.0 — it was renamed because the bare `json` key collided
awkwardly with the publisher-format concept and forced an alias hack.

| Field | Type | Default |
|---|---|---|
| `output_dir` | `str` | `"./iac-inventory-json"` |

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
| `discord` | `iac-cartographer/discord` URL | `username: str \| None`, `avatar_url: str \| None` |
| `stdout` | none | `stream: "stdout" \| "stderr"` *(default `"stdout"`)* |

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
