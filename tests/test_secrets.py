"""Tests for the SecretsProvider implementations + factory."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import boto3
import httpx
import pytest
import respx
from moto import mock_aws

from iac_cartographer.aws import DEFAULT_REGION
from iac_cartographer.constants import ConfigError
from iac_cartographer.models import SecretsConfig
from iac_cartographer.secrets import (
    AwsSecretsProvider,
    EnvSecretsError,
    EnvSecretsProvider,
    VaultSecretsError,
    VaultSecretsProvider,
    build_provider,
)

if TYPE_CHECKING:
    from pathlib import Path


# ─── AwsSecretsProvider ───────────────────────────────────────────────────


@pytest.fixture
def _aws_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", DEFAULT_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@mock_aws
def test_aws_provider_get_secret(_aws_region: None) -> None:
    sm = boto3.client("secretsmanager", region_name=DEFAULT_REGION)
    sm.create_secret(Name="iac-cartographer/gitlab", SecretString=json.dumps({"token": "glpat-abc"}))
    p = AwsSecretsProvider()
    assert p.get_secret("iac-cartographer/gitlab") == {"token": "glpat-abc"}
    assert p.name == f"aws@{DEFAULT_REGION}"


@mock_aws
def test_aws_provider_get_parameter(_aws_region: None) -> None:
    ssm = boto3.client("ssm", region_name=DEFAULT_REGION)
    ssm.put_parameter(
        Name="/iac-cartographer/confluence-parent-id",
        Value="123456789",
        Type="String",
    )
    p = AwsSecretsProvider()
    assert p.get_parameter("/iac-cartographer/confluence-parent-id") == "123456789"


# ─── EnvSecretsProvider ───────────────────────────────────────────────────


def test_env_provider_get_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "IAC_CARTOGRAPHER_SECRET_CONFLUENCE",
        json.dumps({"email": "bot@x.test", "api_token": "ATATT"}),
    )
    p = EnvSecretsProvider()
    assert p.get_secret("iac-cartographer/confluence") == {
        "email": "bot@x.test",
        "api_token": "ATATT",
    }


def test_env_provider_get_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID", "999")
    p = EnvSecretsProvider()
    assert p.get_parameter("/iac-cartographer/confluence-parent-id") == "999"


def test_env_provider_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IAC_CARTOGRAPHER_SECRET_NOPE", raising=False)
    p = EnvSecretsProvider()
    with pytest.raises(EnvSecretsError, match="IAC_CARTOGRAPHER_SECRET_NOPE"):
        p.get_secret("iac-cartographer/nope")


def test_env_provider_invalid_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CARTOGRAPHER_SECRET_BAD", "not-json{")
    p = EnvSecretsProvider()
    with pytest.raises(EnvSecretsError, match="not valid JSON"):
        p.get_secret("iac-cartographer/bad")


def test_env_provider_non_object_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAC_CARTOGRAPHER_SECRET_LIST", json.dumps(["a", "b"]))
    p = EnvSecretsProvider()
    with pytest.raises(EnvSecretsError, match="JSON object"):
        p.get_secret("iac-cartographer/list")


def test_env_provider_dotenv_autoload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nIAC_CARTOGRAPHER_SECRET_GITLAB={"token":"glpat-from-dotenv"}\n'
        'IAC_CARTOGRAPHER_PARAM_FOO="quoted-value"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("IAC_CARTOGRAPHER_SECRET_GITLAB", raising=False)
    monkeypatch.delenv("IAC_CARTOGRAPHER_PARAM_FOO", raising=False)
    p = EnvSecretsProvider(dotenv_path=env_file)
    assert p.get_secret("iac-cartographer/gitlab") == {"token": "glpat-from-dotenv"}
    assert p.get_parameter("/iac-cartographer/foo") == "quoted-value"


def test_env_provider_dotenv_does_not_override_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('IAC_CARTOGRAPHER_SECRET_GITLAB={"token":"from-dotenv"}\n', encoding="utf-8")
    monkeypatch.setenv("IAC_CARTOGRAPHER_SECRET_GITLAB", json.dumps({"token": "from-env"}))
    p = EnvSecretsProvider(dotenv_path=env_file)
    # Pre-existing env var wins.
    assert p.get_secret("iac-cartographer/gitlab") == {"token": "from-env"}


# ─── VaultSecretsProvider ─────────────────────────────────────────────────


def test_vault_provider_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    with pytest.raises(VaultSecretsError, match="no token available"):
        VaultSecretsProvider(addr="https://vault.test")


@respx.mock
def test_vault_provider_get_secret() -> None:
    respx.get("https://vault.test/v1/secret/data/iac-cartographer/gitlab").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"data": {"token": "glpat-from-vault"}, "metadata": {"version": 1}}},
        )
    )
    p = VaultSecretsProvider(addr="https://vault.test", token="root")
    assert p.get_secret("iac-cartographer/gitlab") == {"token": "glpat-from-vault"}


@respx.mock
def test_vault_provider_get_parameter_value_field() -> None:
    respx.get("https://vault.test/v1/secret/data/iac-cartographer/confluence-parent-id").mock(
        return_value=httpx.Response(200, json={"data": {"data": {"value": "987654"}, "metadata": {}}})
    )
    p = VaultSecretsProvider(addr="https://vault.test", token="root")
    assert p.get_parameter("/iac-cartographer/confluence-parent-id") == "987654"


@respx.mock
def test_vault_provider_404_raises() -> None:
    respx.get("https://vault.test/v1/secret/data/iac-cartographer/missing").mock(
        return_value=httpx.Response(404, json={"errors": []})
    )
    p = VaultSecretsProvider(addr="https://vault.test", token="root")
    with pytest.raises(VaultSecretsError, match="not found"):
        p.get_secret("iac-cartographer/missing")


@respx.mock
def test_vault_provider_403_raises() -> None:
    respx.get("https://vault.test/v1/secret/data/iac-cartographer/forbidden").mock(
        return_value=httpx.Response(403, json={"errors": ["permission denied"]})
    )
    p = VaultSecretsProvider(addr="https://vault.test", token="root")
    with pytest.raises(VaultSecretsError, match="forbidden"):
        p.get_secret("iac-cartographer/forbidden")


@respx.mock
def test_vault_provider_parameter_without_value_field_raises() -> None:
    respx.get("https://vault.test/v1/secret/data/iac-cartographer/wrong-shape").mock(
        return_value=httpx.Response(200, json={"data": {"data": {"some_other_key": "x"}, "metadata": {}}})
    )
    p = VaultSecretsProvider(addr="https://vault.test", token="root")
    with pytest.raises(VaultSecretsError, match="must be stored as"):
        p.get_parameter("iac-cartographer/wrong-shape")


@respx.mock
def test_vault_provider_custom_mount_and_prefix() -> None:
    respx.get("https://vault.test/v1/kv/data/gitlab").mock(
        return_value=httpx.Response(200, json={"data": {"data": {"token": "x"}, "metadata": {}}})
    )
    p = VaultSecretsProvider(addr="https://vault.test", token="root", mount="kv", path_prefix="")
    assert p.get_secret("iac-cartographer/gitlab") == {"token": "x"}


@respx.mock
def test_vault_provider_sends_namespace_header() -> None:
    route = respx.get("https://vault.test/v1/secret/data/iac-cartographer/x").mock(
        return_value=httpx.Response(200, json={"data": {"data": {"k": "v"}, "metadata": {}}})
    )
    p = VaultSecretsProvider(addr="https://vault.test", token="root", namespace="prod/team-alpha")
    p.get_secret("iac-cartographer/x")
    assert route.calls.last.request.headers["X-Vault-Namespace"] == "prod/team-alpha"


# ─── build_provider factory ───────────────────────────────────────────────


def test_factory_builds_aws_provider() -> None:
    p = build_provider(SecretsConfig(backend="aws"))
    assert isinstance(p, AwsSecretsProvider)


def test_factory_builds_env_provider() -> None:
    p = build_provider(SecretsConfig(backend="env"))
    assert isinstance(p, EnvSecretsProvider)


def test_factory_builds_vault_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_TOKEN", "root")
    p = build_provider(SecretsConfig(backend="vault", vault_addr="https://vault.test"))
    assert isinstance(p, VaultSecretsProvider)


def test_factory_rejects_vault_without_addr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_TOKEN", "root")
    with pytest.raises(ConfigError, match="vault_addr"):
        build_provider(SecretsConfig(backend="vault"))
