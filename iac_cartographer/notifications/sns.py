"""AWS SNS notification channel.

Publishes pipeline events to an SNS topic. SNS handles the fanout
downstream — you can subscribe email, SMS, Lambda, SQS, HTTPS, and
mobile push endpoints to the same topic from one place. Fits the
AWS-first deployment story the project was originally extracted from.

Auth: the standard AWS credential chain (env vars, instance profile,
IRSA / workload identity on EKS, IAM role on the ECS task). No
`iac-cartographer/sns` secret — identity-based, like Bedrock.

Transport: `boto3` (already a base install dependency for AWS Secrets
Manager / SSM / Bedrock support). The SNS client is synchronous, so
`publish()` runs in a thread via `asyncio.to_thread()` to keep the
notification dispatcher's `asyncio.gather()` fanout from blocking on
the network round-trip.

Each message carries a `level` MessageAttribute so subscribers can
filter (e.g. an email subscription for ERROR only; a Lambda
subscription for all three). Subject is capped at 100 chars per the
SNS API limit.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel

logger = logging.getLogger("iac_cartographer.notifications.sns")

# SNS Subject field caps at 100 chars (and rejects unicode + control
# chars). We truncate aggressively because the full message lands in
# the body — Subject is just for inbox-preview-style scanning.
_MAX_SUBJECT_CHARS = 100
_SUBJECT_PREFIX = "[iac-cartographer]"


class SnsChannel(NotificationChannel):
    """Publish notifications to an SNS topic."""

    name = "sns"

    def __init__(
        self,
        *,
        topic_arn: str,
        region: str | None = None,
    ) -> None:
        self._topic_arn = topic_arn
        self._region = region
        self._client: Any | None = None  # lazy boto3 client

    def _ensure_client(self) -> Any:
        if self._client is None:
            # Lazy boto3 import keeps process startup fast when SNS isn't
            # used — `boto3.client()` has a non-trivial cold cost.
            import boto3

            kwargs: dict[str, Any] = {}
            if self._region:
                kwargs["region_name"] = self._region
            self._client = boto3.client("sns", **kwargs)
        return self._client

    async def notify(self, level: NotificationLevel, message: str) -> None:
        truncated = message if len(message) <= 60 else message[:57] + "…"
        subject = f"{_SUBJECT_PREFIX}[{level.value.upper()}] {truncated}"[:_MAX_SUBJECT_CHARS]

        def _publish() -> None:
            client = self._ensure_client()
            client.publish(
                TopicArn=self._topic_arn,
                Subject=subject,
                Message=message,
                MessageAttributes={
                    "level": {"DataType": "String", "StringValue": level.value},
                    "source": {"DataType": "String", "StringValue": "iac-cartographer"},
                },
            )

        try:
            await asyncio.to_thread(_publish)
        except Exception:
            logger.warning("sns: %s publish raised", level.value, exc_info=True)
