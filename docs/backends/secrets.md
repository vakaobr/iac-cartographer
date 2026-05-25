# Secrets providers

`secrets.backend` picks where credentials (Confluence, GitLab, GitHub,
Slack, Anthropic, Bitbucket) and opaque parameters (the Confluence
parent page ID, etc.) come from.

Three backends ship today.

| Backend | Secrets from | Parameters from | When to use |
|---|---|---|---|
| `aws` *(default)* | AWS Secrets Manager | SSM Parameter Store | Production on AWS — what the original deployment uses. |
| `env` | `IAC_CARTOGRAPHER_SECRET_<NAME>` env var (JSON) | `IAC_CARTOGRAPHER_PARAM_<NAME>` env var (plain) | CI / GitHub Actions; k8s with secrets injected as env vars; local dev. Optional `.env` autoload. |
| `vault` | HashiCorp Vault KV v2 at `{mount}/data/{prefix}{name}` | Same path; payload must contain a `value` field | Multi-cloud / on-prem / regulated environments where Vault is already standard. |

## Logical secret names

Every backend uses the same logical names. The backend's job is to map
the logical name to a backend-specific lookup path.

| Logical name | When required | JSON shape |
|---|---|---|
| `iac-cartographer/confluence` | `publisher.kind == "confluence"` | `{"email": "bot@example.com", "api_token": "ATATT..."}` |
| `iac-cartographer/gitlab` | always *(constructor-loaded)* | `{"token": "glpat-..."}` |
| `iac-cartographer/github` | always *(constructor-loaded)* | `{"token": "ghp_..."}` |
| `iac-cartographer/slack` | always *(constructor-loaded)* | `{"bot_token": "xoxb-..."}` |
| `iac-cartographer/anthropic` | `llm.backend == "anthropic"` | `{"api_key": "sk-ant-..."}` |
| `iac-cartographer/bitbucket` | `discovery.bitbucket_workspaces` non-empty | `{"access_token": "bbat-..."}` *or* `{"username": "...", "app_password": "..."}` |

The `gitlab` / `github` / `slack` secrets are loaded eagerly regardless
of which discovery sources or publishers are active; you can put
placeholder values there if a particular backend isn't used. Future
versions will make these lazy.

## AWS

```yaml
secrets:
  backend: aws
  aws_region: eu-central-1
```

Reads `iac-cartographer/<name>` from Secrets Manager and SSM Parameter
Store paths verbatim. Auth uses the standard AWS credential chain.

## Environment variables

```yaml
secrets:
  backend: env
  env_dotenv_path: "/etc/iac-cartographer/.env"   # optional
```

Logical name → env var translation:

| Logical | Env var |
|---|---|
| `iac-cartographer/confluence` | `IAC_CARTOGRAPHER_SECRET_CONFLUENCE` |
| `iac-cartographer/gitlab` | `IAC_CARTOGRAPHER_SECRET_GITLAB` |
| `/iac-cartographer/confluence-parent-id` | `IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID` |

Strip the `iac-cartographer/` prefix, uppercase, replace non-alphanumerics
with `_`. Secrets are JSON-decoded; parameters are used as-is.

Optional `.env` autoload — `env_dotenv_path` points at a `KEY=value`
file. Pre-existing env vars take precedence (docker-compose-compatible
semantics).

Example:

```bash
export IAC_CARTOGRAPHER_SECRET_GITLAB='{"token":"glpat-..."}'
export IAC_CARTOGRAPHER_SECRET_GITHUB='{"token":"ghp_..."}'
export IAC_CARTOGRAPHER_SECRET_CONFLUENCE='{"email":"bot@x","api_token":"ATATT..."}'
export IAC_CARTOGRAPHER_SECRET_SLACK='{"bot_token":"xoxb-..."}'
export IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID="123456789"
iac-cartographer --once --config /etc/iac-cartographer/config.yaml
```

## HashiCorp Vault

```yaml
secrets:
  backend: vault
  vault_addr: "https://vault.example.com"
  vault_mount: "secret"                       # KV v2 mount
  vault_path_prefix: "iac-cartographer/"      # logical prefix under mount
  vault_namespace: null                       # Vault Enterprise only
```

Auth uses the standard `VAULT_TOKEN` env var. Whatever wrote the token
there (a sidecar injector, an `vault login` shim, a CI workflow step)
takes care of rotation — iac-cartographer just reads it.

Path mapping:

- `iac-cartographer/confluence` → `secret/data/iac-cartographer/confluence`
- `/iac-cartographer/confluence-parent-id` → `secret/data/iac-cartographer/confluence-parent-id`

Set up Vault with:

```bash
vault kv put secret/iac-cartographer/gitlab token=glpat-...
vault kv put secret/iac-cartographer/confluence email=bot@x api_token=ATATT-...
vault kv put secret/iac-cartographer/confluence-parent-id value=123456789
```

The parameter convention (single `value` field) is intentional — same
KV mount, two payload shapes (the secret one is freeform, the
parameter one is single-string).
