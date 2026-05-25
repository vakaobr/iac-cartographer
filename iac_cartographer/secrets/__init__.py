"""Secrets backends — credential + parameter lookup, pluggable.

Public surface:

  * `SecretsProvider`        — ABC every backend extends.
  * `AwsSecretsProvider`     — Secrets Manager + SSM (legacy default).
  * `EnvSecretsProvider`     — process env vars, optional `.env` autoload.
  * `VaultSecretsProvider`   — HashiCorp Vault KV v2 over HTTP.
  * `build_provider`         — factory that picks the right provider from
                               a `SecretsConfig`.

Adding a new backend: subclass `SecretsProvider` and add a branch to
`build_provider`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from iac_cartographer.constants import ConfigError
from iac_cartographer.secrets.aws import AwsSecretsProvider
from iac_cartographer.secrets.base import SecretsProvider
from iac_cartographer.secrets.env import EnvSecretsError, EnvSecretsProvider
from iac_cartographer.secrets.vault import VaultSecretsError, VaultSecretsProvider

if TYPE_CHECKING:
    from iac_cartographer.models import SecretsConfig


def build_provider(config: SecretsConfig) -> SecretsProvider:
    """Instantiate the right `SecretsProvider` for `config.backend`."""
    backend = config.backend
    if backend == "aws":
        return AwsSecretsProvider(region=config.aws_region)
    if backend == "env":
        return EnvSecretsProvider(dotenv_path=config.env_dotenv_path)
    if backend == "vault":
        if not config.vault_addr:
            raise ConfigError("secrets.backend=vault but secrets.vault_addr is empty")
        return VaultSecretsProvider(
            addr=config.vault_addr,
            mount=config.vault_mount,
            path_prefix=config.vault_path_prefix,
            namespace=config.vault_namespace,
        )
    raise ConfigError(f"unknown secrets.backend: {backend!r}")


__all__ = [
    "AwsSecretsProvider",
    "EnvSecretsError",
    "EnvSecretsProvider",
    "SecretsProvider",
    "VaultSecretsError",
    "VaultSecretsProvider",
    "build_provider",
]
