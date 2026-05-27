"""Secrets subsystem config models.

`SecretsConfig` selects which credential backend (`aws` / `env` / `vault`)
the run reads from; it lives beside the `SecretsProvider` implementations
in this package.

Re-exported from `iac_cartographer.models` for back-compat.
"""

from __future__ import annotations

from typing import Literal

from iac_cartographer.models import _Strict


class SecretsConfig(_Strict):
    """Selects WHERE credentials + opaque parameters come from.

    Most fields are backend-specific and ignored when `backend` doesn't
    match. Adding a new backend means: add a literal to the discriminator,
    implement the subclass in `secrets/`, and add a branch to
    `secrets.build_provider`.
    """

    # Which secrets backend to use.
    #   "aws"   → AWS Secrets Manager + SSM Parameter Store (default; what
    #             the production deployment iac-cartographer was extracted
    #             from uses).
    #   "env"   → Process environment variables. Naming convention:
    #             `IAC_CARTOGRAPHER_SECRET_<NAME>` for secrets (JSON value),
    #             `IAC_CARTOGRAPHER_PARAM_<NAME>` for opaque parameters.
    #             Optional `.env` autoload via `env_dotenv_path`.
    #   "vault" → HashiCorp Vault KV v2 over HTTP. Auth via VAULT_TOKEN env.
    backend: Literal["aws", "env", "vault"] = "aws"

    # AWS region for boto3 clients when backend == "aws". Ignored otherwise.
    aws_region: str = "eu-central-1"

    # Path to a `.env` file to autoload before reading env vars
    # (backend == "env" only). Pre-existing env vars take precedence.
    # `None` = don't autoload.
    env_dotenv_path: str | None = None

    # Vault server URL when backend == "vault" (e.g. `https://vault.example.com`).
    vault_addr: str = ""

    # KV v2 mount path (Vault terminology — see `vault read -mount`).
    vault_mount: str = "secret"

    # Logical prefix joined under the mount. Leave default if you mirror the
    # `iac-cartographer/...` naming convention; override to a flat path if
    # the operator strips the prefix at the Vault layer.
    vault_path_prefix: str = "iac-cartographer/"

    # Vault Enterprise namespace header. None = single-tenant Vault.
    vault_namespace: str | None = None
