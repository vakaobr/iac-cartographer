"""AWS service wrappers — Secrets Manager + SSM + Bedrock + CloudWatch.

Intentionally thin: one function per AWS operation, no caching, no per-call
retry layer (boto3's built-in retries are sufficient at our scale). Clients
are constructed per call — the wrappers stay stateless and easy to mock with
moto.

Pattern cloned from konsoleh-monitor/konsoleh_monitor/aws.py. Region pinned
to eu-central-1 — Bedrock's EU cross-region inference profile lives there
(ADR-005) and our SSM/Secrets Manager state is colocated.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3

logger = logging.getLogger("iac_cartographer.aws")

DEFAULT_REGION = "eu-central-1"


def get_secret(name: str, region: str = DEFAULT_REGION) -> dict[str, Any]:
    """Fetch and JSON-decode a Secrets Manager secret.

    Returns the parsed JSON dict. Raises `botocore.exceptions.ClientError`
    on AWS-side failure (e.g. ResourceNotFoundException) — we deliberately
    do NOT swallow this; the caller (cli.main) maps it to exit code 2.
    """
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=name)
    secret_string = response["SecretString"]
    logger.info("aws: fetched secret %s", name)
    return json.loads(secret_string)


def get_ssm_parameter(name: str, region: str = DEFAULT_REGION) -> str:
    """Fetch a Systems Manager Parameter Store value as a raw string.

    No JSON decoding — the caller parses according to whatever schema it
    expects (YAML for config; opaque string for the Confluence parent-page ID).
    Raises `botocore.exceptions.ClientError` on missing parameter.
    """
    client = boto3.client("ssm", region_name=region)
    response = client.get_parameter(Name=name, WithDecryption=True)
    value: str = response["Parameter"]["Value"]
    logger.info("aws: fetched ssm parameter %s (length=%d)", name, len(value))
    return value


def invoke_bedrock_model(
    model_id: str,
    body: dict[str, Any],
    region: str = DEFAULT_REGION,
) -> dict[str, Any]:
    """Invoke a Bedrock foundation model and return the parsed response body.

    `body` is the model-specific request payload — for Claude on Bedrock that
    means the `anthropic_version` + `messages` + `system` shape. Caller owns
    prompt assembly; this wrapper is just JSON-encode → invoke → JSON-decode.

    Returns the full response body dict; the caller extracts
    `response["content"][0]["text"]` (or whichever shape the model produced).

    Raises `botocore.exceptions.ClientError` on throttling / quota / auth
    failures — the caller (narrator) decides whether to retry-once or
    skip-and-continue.
    """
    client = boto3.client("bedrock-runtime", region_name=region)
    response = client.invoke_model(
        modelId=model_id,
        body=json.dumps(body).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    raw = response["body"].read()
    parsed: dict[str, Any] = json.loads(raw)
    logger.info(
        "aws: bedrock invoke ok (model=%s, response_bytes=%d)",
        model_id,
        len(raw),
    )
    return parsed


def put_metric_data(
    namespace: str,
    metric_name: str,
    value: float,
    unit: str = "Count",
    region: str = DEFAULT_REGION,
) -> None:
    """Emit a single CloudWatch custom metric point.

    Best-effort: failures are logged but do not raise — emission of a metric
    is observational, never load-bearing for the run.
    """
    try:
        client = boto3.client("cloudwatch", region_name=region)
        client.put_metric_data(
            Namespace=namespace,
            MetricData=[{"MetricName": metric_name, "Value": value, "Unit": unit}],
        )
        logger.debug("aws: published metric %s/%s = %s %s", namespace, metric_name, value, unit)
    except Exception:
        logger.warning("aws: failed to publish metric %s/%s", namespace, metric_name, exc_info=True)
