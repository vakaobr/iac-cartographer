"""Tests for the AWS SNS notification channel.

Verifies:
  * `notify` publishes to the configured topic ARN with the
    expected Subject + Message + MessageAttributes shape.
  * Subject is capped at 100 chars (SNS API limit) and carries level
    + truncated message.
  * Each level surfaces in the `level` MessageAttribute so SNS filter
    policies can route per-severity.
  * Boto3 `publish` exceptions are swallowed (logged, not raised).
  * Region kwarg is honoured at client construction.

Tests use `moto`'s `@mock_aws` to spin up an in-memory SNS — same
fixture pattern the existing `tests/test_secrets.py` uses.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

from iac_cartographer.notifications import NotificationLevel
from iac_cartographer.notifications.sns import SnsChannel

# `@mock_aws` wraps a function in a sync context; applied directly to an
# `async def` test, pytest-asyncio never sees the coroutine. Workaround:
# use the context-manager form (`with mock_aws(): ...`) inside the async
# body so the AWS mock sits inside the awaited coroutine.


# ── notify shape ──────────────────────────────────────────────────────


async def test_notify_publishes_to_topic_arn() -> None:
    with mock_aws():
        client = boto3.client("sns", region_name="eu-central-1")
        topic_arn = client.create_topic(Name="iac-cartographer-test")["TopicArn"]
        ch = SnsChannel(topic_arn=topic_arn, region="eu-central-1")

        await ch.notify(NotificationLevel.INFO, "iac-cartographer: run starting")
        # No exception → publish succeeded. The SQS-subscriber test below
        # asserts the actual payload shape end-to-end.


async def test_notify_passes_subject_message_and_level_attribute() -> None:
    with mock_aws():
        region = "eu-central-1"
        sns = boto3.client("sns", region_name=region)
        sqs = boto3.client("sqs", region_name=region)
        topic_arn = sns.create_topic(Name="iac-cartographer-test")["TopicArn"]
        queue_url = sqs.create_queue(QueueName="iac-cartographer-test-q")["QueueUrl"]
        queue_attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
        queue_arn = queue_attrs["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)

        ch = SnsChannel(topic_arn=topic_arn, region=region)
        await ch.notify(NotificationLevel.ERROR, "kaboom")

        resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
        assert "Messages" in resp
        # SNS-formatted SQS messages embed the original publish payload
        # inside a JSON envelope under the `Message` field.
        envelope = json.loads(resp["Messages"][0]["Body"])
        assert envelope["Message"] == "kaboom"
        assert "ERROR" in envelope["Subject"]
        assert envelope["MessageAttributes"]["level"]["Value"] == "error"
        assert envelope["MessageAttributes"]["source"]["Value"] == "iac-cartographer"


# ── Subject formatting (via mocked boto3 client) ─────────────────────


async def test_subject_truncates_long_messages() -> None:
    """SNS Subject caps at 100 chars — exercise the truncation path."""
    ch = SnsChannel(topic_arn="arn:aws:sns:eu-central-1:000:fake", region="eu-central-1")

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"MessageId": "fake"}

    fake_client = MagicMock()
    fake_client.publish.side_effect = _capture
    ch._client = fake_client

    long_msg = "x" * 200
    await ch.notify(NotificationLevel.WARN, long_msg)

    subject = captured["Subject"]
    assert len(subject) <= 100
    assert "[iac-cartographer][WARN]" in subject


async def test_subject_carries_each_level() -> None:
    ch = SnsChannel(topic_arn="arn:aws:sns:eu-central-1:000:fake", region="eu-central-1")

    captured: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return {"MessageId": "fake"}

    fake_client = MagicMock()
    fake_client.publish.side_effect = _capture
    ch._client = fake_client

    await ch.notify(NotificationLevel.INFO, "i")
    await ch.notify(NotificationLevel.WARN, "w")
    await ch.notify(NotificationLevel.ERROR, "e")

    assert "[INFO]" in captured[0]["Subject"]
    assert "[WARN]" in captured[1]["Subject"]
    assert "[ERROR]" in captured[2]["Subject"]
    assert [c["MessageAttributes"]["level"]["StringValue"] for c in captured] == [
        "info",
        "warn",
        "error",
    ]


# ── Error handling ────────────────────────────────────────────────────


async def test_notify_swallows_boto3_exception() -> None:
    """A failing publish() must not propagate — log + return."""
    ch = SnsChannel(topic_arn="arn:aws:sns:eu-central-1:000:fake", region="eu-central-1")

    # Patch boto3 entirely so the call fails on the first publish.
    fake_client = MagicMock()
    fake_client.publish.side_effect = RuntimeError("network down")
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = fake_client

    with patch.dict("sys.modules", {"boto3": fake_boto3}):
        # Force re-import path inside _ensure_client.
        ch._client = None
        await ch.notify(NotificationLevel.ERROR, "hi")  # must not raise


# ── Construction ──────────────────────────────────────────────────────


def test_constructor_does_not_open_boto3_client() -> None:
    """Lazy: no boto3.client() call until first notify()."""
    ch = SnsChannel(topic_arn="arn:aws:sns:eu-central-1:000:fake", region="eu-central-1")
    assert ch._client is None


@mock_aws
def test_region_kwarg_is_threaded_into_boto3_client() -> None:
    """Constructor takes region; client is built lazily with region_name set."""
    sns = boto3.client("sns", region_name="us-east-1")
    topic_arn = sns.create_topic(Name="iac-cartographer-test")["TopicArn"]
    ch = SnsChannel(topic_arn=topic_arn, region="us-east-1")
    client = ch._ensure_client()
    # boto3 stashes region on the client meta. Verify it survived.
    assert client.meta.region_name == "us-east-1"
