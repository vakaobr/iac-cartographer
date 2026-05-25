# Configuration schema

Every field, what it means, what it defaults to. The canonical schema
lives in [`iac_cartographer/models.py`](https://github.com/vakaobr/iac-cartographer/blob/main/iac_cartographer/models.py);
this page tracks it.

For a working example with every field commented, see
[`examples/config.example.yaml`](https://github.com/vakaobr/iac-cartographer/blob/main/examples/config.example.yaml).

## Top-level shape

```yaml
discovery:    {...}   # where to find repos
secrets:      {...}   # where credentials + opaque parameters come from
llm:          {...}   # which LLM provider narrates
publisher:    {...}   # which publisher writes the output
confluence:   {...}   # publisher.kind == "confluence" specifics
markdown:     {...}   # publisher.kind == "markdown" specifics
html:         {...}   # publisher.kind == "html" specifics
json:         {...}   # publisher.kind == "json" specifics
slack:        {...}   # outcome notifications
```

All sections are optional — every field has a default. The minimal
valid config is `{}` (assuming you also export the right secrets and
have an LLM backend reachable).

## `discovery`

| Field | Type | Default | What it does |
|---|---|---|---|
| `gitlab_group_ids` | `list[int]` | `[]` | GitLab group IDs (numeric). Subgroups are scanned automatically. Empty = skip GitLab. |
| `gitlab_base_url` | `str` | `"https://gitlab.com"` | Override for self-hosted GitLab. Don't include `/api/v4` — the source adds it. |
| `github_orgs` | `list[str]` | `[]` | GitHub organisation slugs. Empty = skip GitHub. |
| `bitbucket_workspaces` | `list[str]` | `[]` | Bitbucket Cloud workspace slugs. Empty = skip Bitbucket. |
| `repos_file` | `str \| None` | `None` | Path to a curated YAML/JSON list of `RepoMetadata` records. |
| `deny_repos` | `list[str]` | `[]` | Glob patterns (fnmatch) matched against `full_name`. Applied to the merged-and-deduped result. |
| `owner_overrides` | `dict[str, str]` | `{}` | `full_name → team` overrides for the owning-team guess when the LLM's guess is wrong. |

At least one source must be configured (one of `gitlab_group_ids`,
`github_orgs`, `bitbucket_workspaces`, `repos_file` must be non-empty
/ set). The orchestrator fails loud if all four are empty.

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

| Field | Type | Default | What it does |
|---|---|---|---|
| `backend` | `"bedrock" \| "anthropic"` | `"bedrock"` | Picks the `LLMBackend`. |
| `model_id` | `str` | `"eu.anthropic.claude-sonnet-4-5-20250929-v1:0"` | Bedrock inference-profile ID, or Anthropic model name. |
| `max_tokens` | `int` | `4096` | Max output tokens per invocation. |
| `system_prompt_version` | `str` | `"v1"` | Bump to force-republish every page on the next run (invalidates banner-SHA). |
| `bedrock_region` | `str` | `"eu-central-1"` | AWS region. Bedrock-only. |
| `anthropic_base_url` | `str` | `"https://api.anthropic.com"` | API base. Anthropic-only — override for an internal proxy. |

## `publisher`

| Field | Type | Default | What it does |
|---|---|---|---|
| `kind` | `"confluence" \| "markdown" \| "html" \| "json"` | `"confluence"` | Picks the `Publisher`. |

## `confluence`

Only honoured when `publisher.kind == "confluence"`.

| Field | Type | Default | What it does |
|---|---|---|---|
| `site` | `str` | `"your-org.atlassian.net"` | Atlassian Cloud site (no protocol, no trailing slash). |
| `space_key` | `str` | `"DOCS"` | Confluence space key (the parent page must already exist there). |
| `parent_page_id_ssm_path` | `str` | `"/iac-cartographer/confluence-parent-id"` | Logical name of the parameter holding the parent page's numeric ID. Resolved via the active `SecretsProvider.get_parameter()`. |
| `parent_page_id` | `str \| None` | `None` | Direct override. When set, the parameter-store lookup is skipped entirely. |

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

## `json`

Only honoured when `publisher.kind == "json"`. The YAML key is `json:`;
the Python attribute on `AppConfig` is `json_output` (to avoid
shadowing Pydantic's `BaseModel.json()` method).

| Field | Type | Default |
|---|---|---|
| `output_dir` | `str` | `"./iac-inventory-json"` |

## `slack`

| Field | Type | Default |
|---|---|---|
| `channel` | `str` | `"#alerts"` |

Slack posts go out at the end of every non-dry-run regardless of
publisher choice. Failures are logged at WARN level and don't sink the
run, so a misconfigured channel can't break publishing.
