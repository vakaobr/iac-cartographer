"""SecretsProvider ABC.

A `SecretsProvider` fetches two kinds of values from one upstream backend:

  * **Secrets** — JSON-shaped credential payloads (Confluence email +
    token, GitLab token, GitHub token, Slack bot token, …). Fetched via
    `get_secret(name) -> dict[str, Any]`. The caller validates each
    payload against a Pydantic model.
  * **Parameters** — opaque single-value strings (the Confluence parent
    page ID, a tenancy ID, a region name, …). Fetched via
    `get_parameter(name) -> str`.

The split mirrors what AWS deployments already do (Secrets Manager for
the former, SSM Parameter Store for the latter) and maps naturally onto
the env-var and Vault backends too.

Three providers ship today: AWS, env vars (with optional `.env`
autoload), and HashiCorp Vault KV v2. Add a new one by subclassing
`SecretsProvider` and wiring it into the factory in
`iac_cartographer.secrets.__init__`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SecretsProvider(ABC):
    """Abstract base class for credential + parameter lookups."""

    #: Human-readable label used in log lines (e.g. `"aws"`, `"env"`,
    #: `"vault@https://vault.example.com"`).
    name: str = "secrets"

    @abstractmethod
    def get_secret(self, name: str) -> dict[str, Any]:
        """Return a JSON-shaped secret payload (e.g. credential bundle).

        `name` is the backend-agnostic logical name — e.g.
        `iac-cartographer/confluence` or `iac-cartographer/gitlab`.
        Backends translate this into their native lookup path:

          * AWS:   `secretsmanager:GetSecretValue(SecretId=<name>)`
          * env:   `IAC_CARTOGRAPHER_SECRET_<NAME>` (uppercased, `/` → `_`)
          * vault: `{mount}/data/<name>` (KV v2)

        Raises a backend-specific exception when the secret is missing;
        the caller wraps that in `MissingSecretError` for the CLI's
        unified error reporting."""

    @abstractmethod
    def get_parameter(self, name: str) -> str:
        """Return an opaque string parameter (non-secret single value).

        Same naming rules as `get_secret`, but the value is a plain
        string (not JSON-decoded)."""
