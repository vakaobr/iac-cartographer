"""Tests for the generic webhook notification channel.

Covers payload schema correctness, error swallowing (HTTP 4xx/5xx +
network exceptions), lazy httpx-client initialization, and the
`extra_headers` config field. End-to-end behaviour mirrors the existing
SlackChannel tests; the differences are payload-shape and the lack of
a Slack-specific ok-false case.
"""

from __future__ import annotations

import json

import httpx
import respx

from iac_cartographer.models import WebhookCredentials
from iac_cartographer.notifications import NotificationLevel
from iac_cartographer.notifications.webhook import (
    PAYLOAD_SCHEMA,
    GenericWebhookChannel,
)

URL = "https://hook.example.com/notify"


def _channel(**kwargs: object) -> GenericWebhookChannel:
    return GenericWebhookChannel(WebhookCredentials(url=URL), **kwargs)  # type: ignore[arg-type]


@respx.mock
async def test_payload_schema_and_fields() -> None:
    """The body MUST follow the v1 schema with level/message/ts/source."""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    n = _channel()
    try:
        await n.notify(NotificationLevel.WARN, "be careful")
    finally:
        await n.close()

    body = json.loads(route.calls[0].request.read())
    assert body["schema"] == PAYLOAD_SCHEMA
    assert body["level"] == "warn"
    assert body["message"] == "be careful"
    assert body["source"] == "iac-cartographer"
    # `ts` is an ISO 8601 UTC string — the suffix is Z (not +00:00).
    assert body["ts"].endswith("Z")


@respx.mock
async def test_levels_serialise_to_their_string_value() -> None:
    """info/warn/error → the matching enum string in the payload."""
    route = respx.post(URL).mock(return_value=httpx.Response(200))
    n = _channel()
    try:
        await n.notify(NotificationLevel.INFO, "i")
        await n.notify(NotificationLevel.WARN, "w")
        await n.notify(NotificationLevel.ERROR, "e")
    finally:
        await n.close()

    bodies = [json.loads(call.request.read()) for call in route.calls]
    assert [b["level"] for b in bodies] == ["info", "warn", "error"]


@respx.mock
async def test_extra_headers_are_attached_to_every_request() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200))
    n = _channel(extra_headers={"Authorization": "Bearer my-token", "X-Source": "ci"})
    try:
        await n.notify(NotificationLevel.INFO, "hi")
    finally:
        await n.close()

    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer my-token"
    assert req.headers["X-Source"] == "ci"
    # Default content-type stays applied.
    assert req.headers["Content-Type"] == "application/json"


@respx.mock
async def test_post_swallows_4xx() -> None:
    respx.post(URL).mock(return_value=httpx.Response(403, text="forbidden"))
    n = _channel()
    try:
        await n.notify(NotificationLevel.INFO, "hi")  # must not raise
    finally:
        await n.close()


@respx.mock
async def test_post_swallows_5xx() -> None:
    respx.post(URL).mock(return_value=httpx.Response(502, text="upstream gone"))
    n = _channel()
    try:
        await n.notify(NotificationLevel.ERROR, "hi")  # must not raise
    finally:
        await n.close()


@respx.mock
async def test_post_swallows_network_exception() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("network down"))
    n = _channel()
    try:
        await n.notify(NotificationLevel.INFO, "hi")  # must not raise
    finally:
        await n.close()


def test_constructor_does_not_open_httpx_client() -> None:
    n = _channel()
    assert n._client is None


async def test_close_without_use_is_safe() -> None:
    n = _channel()
    await n.close()
    assert n._client is None
