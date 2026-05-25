"""Phase 2 tests for iac_cartographer.aws — boto3 wrappers exercised against moto."""

from __future__ import annotations

import json
from io import BytesIO

import boto3
import pytest
from moto import mock_aws

from iac_cartographer.aws import (
    DEFAULT_REGION,
    get_secret,
    get_ssm_parameter,
    invoke_bedrock_model,
    put_metric_data,
)


@pytest.fixture
def _aws_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the test environment is region-pinned (moto needs this for some
    services, and prod runs always set AWS_DEFAULT_REGION via the task env)."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", DEFAULT_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@mock_aws
def test_get_secret_parses_json(_aws_region: None) -> None:
    sm = boto3.client("secretsmanager", region_name=DEFAULT_REGION)
    sm.create_secret(
        Name="iac-cartographer/test",
        SecretString=json.dumps({"token": "glpat-AAAA"}),
    )
    parsed = get_secret("iac-cartographer/test")
    assert parsed == {"token": "glpat-AAAA"}


@mock_aws
def test_get_secret_raises_on_missing(_aws_region: None) -> None:
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        get_secret("iac-cartographer/does-not-exist")


@mock_aws
def test_get_ssm_parameter_returns_raw_string(_aws_region: None) -> None:
    ssm = boto3.client("ssm", region_name=DEFAULT_REGION)
    ssm.put_parameter(
        Name="/iac-cartographer/config",
        Value="discovery:\n  gitlab_group_ids: [1,2]\n",
        Type="SecureString",
    )
    val = get_ssm_parameter("/iac-cartographer/config")
    assert "gitlab_group_ids" in val


@mock_aws
def test_get_ssm_parameter_raises_on_missing(_aws_region: None) -> None:
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        get_ssm_parameter("/iac-cartographer/missing")


def test_invoke_bedrock_model_builds_correct_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """moto doesn't model bedrock-runtime invoke_model — we mock the boto3
    client directly. This test pins the wire shape of what we send."""
    captured: dict[str, object] = {}

    class _FakeClient:
        def invoke_model(self, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {
                "body": BytesIO(json.dumps({"content": [{"text": "ok"}]}).encode("utf-8")),
            }

    monkeypatch.setattr("iac_cartographer.aws.boto3.client", lambda *_a, **_kw: _FakeClient())

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }
    out = invoke_bedrock_model("eu.anthropic.claude-sonnet-4-6", body)
    assert out == {"content": [{"text": "ok"}]}
    assert captured["modelId"] == "eu.anthropic.claude-sonnet-4-6"
    assert captured["contentType"] == "application/json"
    assert captured["accept"] == "application/json"
    # Body was JSON-encoded to bytes
    assert isinstance(captured["body"], bytes)
    assert json.loads(captured["body"]) == body


def test_invoke_bedrock_model_propagates_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import ClientError

    class _FakeClient:
        def invoke_model(self, **_kw: object) -> dict[str, object]:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "InvokeModel")

    monkeypatch.setattr("iac_cartographer.aws.boto3.client", lambda *_a, **_kw: _FakeClient())
    with pytest.raises(ClientError):
        invoke_bedrock_model("eu.anthropic.claude-sonnet-4-6", {})


@mock_aws
def test_put_metric_data_success(_aws_region: None) -> None:
    # Just verify no exception bubbles up — moto records it; we don't assert
    # contents because the wrapper is best-effort.
    put_metric_data("IacCartographer", "BedrockTokensIn", 12345)


def test_put_metric_data_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def put_metric_data(self, **_kw: object) -> None:
            raise RuntimeError("offline")

    monkeypatch.setattr("iac_cartographer.aws.boto3.client", lambda *_a, **_kw: _Boom())
    # Must not raise — best-effort metric.
    put_metric_data("IacCartographer", "BedrockTokensIn", 12345)
