# Backends

`iac-cartographer` is composed of four pluggable subsystems. Each picks an
implementation via a discriminator field in `config.yaml`; configuration
for the inactive implementations is ignored.

| Subsystem | Discriminator | Choices |
|---|---|---|
| [Discovery](discovery.md) | `discovery.{gitlab,github,bitbucket,repos_file}` *(all optional, ≥ 1 required)* | GitLab groups, GitHub orgs, Bitbucket workspaces, curated YAML/JSON file |
| [LLM provider](llm.md) | `llm.backend` | `bedrock` *(default)*, `anthropic` |
| [Publisher](publishers.md) | `publisher.kind` | `confluence` *(default)*, `markdown`, `html`, `json` |
| [Secrets provider](secrets.md) | `secrets.backend` | `aws` *(default)*, `env`, `vault` |

The four are orthogonal — every combination is valid. You can run
GitLab + GitHub discovery, Anthropic LLM, HTML publisher, and Vault
secrets in one config; or AWS Bedrock, file-only discovery, JSON
publisher, and env-var secrets in another.

## Adding a new backend

Each subsystem has an ABC + a factory:

| Subsystem | ABC | Factory |
|---|---|---|
| Discovery | `DiscoverySource` (`iac_cartographer/discovery/base.py`) | `_build_sources` in `cli.py` |
| LLM | `LLMBackend` (`iac_cartographer/llm.py`) | `_build_llm_backend` in `cli.py` |
| Publisher | `Publisher` (`iac_cartographer/publishers/base.py`) | `_build_publisher` in `cli.py` |
| Secrets | `SecretsProvider` (`iac_cartographer/secrets/base.py`) | `build_provider` in `iac_cartographer/secrets/__init__.py` |

Adding a new implementation means subclassing the ABC, adding a literal
to the discriminator in `models.py`, and adding a branch to the factory.
See the existing implementations for the shape of each ABC.
