# Backends

`iac-cartographer` is composed of five pluggable subsystems. Each
picks an implementation via a discriminator field in `config.yaml`;
configuration for the inactive implementations is ignored.

| Subsystem | Discriminator | Choices |
|---|---|---|
| [Discovery](discovery.md) | `discovery.{gitlab,github,bitbucket,gitea,repos_file}` *(all optional, ≥ 1 required)* | GitLab groups, GitHub orgs, Bitbucket workspaces, Gitea / Forgejo orgs, curated YAML/JSON file |
| [LLM provider](llm.md) | `llm.backend` | `bedrock` *(default)*, `anthropic`, `vertex`, `azure_openai`, `openai`, `ollama` |
| [Publisher](publishers.md) | `publisher.kind` | `confluence` *(default)*, `notion`, `github_wiki`, `markdown`, `html`, `json` |
| [Secrets provider](secrets.md) | `secrets.backend` | `aws` *(default)*, `env`, `vault` |
| [Notifications](notifications.md) | `notifications: [...]` *(list — empty falls back to the legacy single-Slack block)* | Slack (bot-token), Slack-incoming-webhook, Teams, RocketChat / Mattermost, generic webhook, email (SMTP), SNS, PagerDuty, Opsgenie, Discord, stdout/JSONL |

The five subsystems are orthogonal — every combination is valid. You
can run GitLab + GitHub discovery, Anthropic LLM, HTML publisher,
Vault secrets, and Teams + email notifications in one config; or AWS
Bedrock, file-only discovery, JSON publisher, env-var secrets, and a
single Slack post in another.

## Adding a new backend

Each subsystem has an ABC + a factory:

| Subsystem | ABC | Factory |
|---|---|---|
| Discovery | `DiscoverySource` (`iac_cartographer/discovery/base.py`) | `_build_sources` in `cli.py` |
| LLM | `LLMBackend` (`iac_cartographer/llm.py`) | `_build_llm_backend` in `cli.py` |
| Publisher | `Publisher` (`iac_cartographer/publishers/base.py`) | `_build_publisher` in `cli.py` |
| Secrets | `SecretsProvider` (`iac_cartographer/secrets/base.py`) | `build_provider` in `iac_cartographer/secrets/__init__.py` |
| Notifications | `NotificationChannel` (`iac_cartographer/notifications/base.py`) | `build_dispatcher` in `iac_cartographer/notifications/__init__.py` |

Adding a new implementation means subclassing the ABC, adding a literal
to the discriminator in `models.py`, and adding a branch to the factory.
See the existing implementations for the shape of each ABC.
