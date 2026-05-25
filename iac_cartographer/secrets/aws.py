"""AWS SecretsProvider — Secrets Manager for secrets, SSM Parameter Store
for opaque parameters. This is the legacy default; matches the production
deployment iac-cartographer was extracted from."""

from __future__ import annotations

import logging
from typing import Any

from iac_cartographer.aws import DEFAULT_REGION, get_secret, get_ssm_parameter
from iac_cartographer.secrets.base import SecretsProvider

logger = logging.getLogger("iac_cartographer.secrets.aws")


class AwsSecretsProvider(SecretsProvider):
    """Reads secrets from AWS Secrets Manager and parameters from SSM."""

    def __init__(self, region: str = DEFAULT_REGION) -> None:
        self._region = region
        self.name = f"aws@{region}"

    def get_secret(self, name: str) -> dict[str, Any]:
        return get_secret(name, region=self._region)

    def get_parameter(self, name: str) -> str:
        return get_ssm_parameter(name, region=self._region)
