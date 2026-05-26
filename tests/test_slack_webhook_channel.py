"""Tests for the Slack-compatible incoming-webhook channel.

Verifies the Slack-shaped `{"text": "<emoji> <message>"}` payload that
makes this channel a drop-in for Slack incoming webhooks, RocketChat,
and Mattermost. Same error-swallowing + lazy-init contract as the
bot-token Slack channel.
"""

from __future__ import annotations

import json

import httpx
import respx

from iac_cartographer.models import SlackWebhookCredentials
from iac_cartographer.notifications import NotificationLevel
from iac_cartographer.notifications.slack_webhook import SlackWebhookChannel

URL = "https://hooks.slack.com/services/T000/B000/XYZ"


def _channel() -> SlackWebhookChannel:
    return SlackWebhookChannel(SlackWebhookCredentials(url=URL))


@respx.mock
async def test_payload_is_slack_shaped_text() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, text="ok"))
    n = _channel()
    try:
        await n.notify(NotificationLevel.INFO, "starting")
    finally:
        await n.close()

    body = json.loads(route.calls[0].request.read())
    # Only one field — Slack-compat endpoints expect exactly {text: ...}.
    assert set(body.keys()) == {"text"}
    assert "starting" in body["text"]


@respx.mock
async def test_emoji_prefix_per_severity() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, text="ok"))
    n = _channel()
    try:
        await n.notify(NotificationLevel.INFO, "i")
        await n.notify(NotificationLevel.WARN, "w")
        await n.notify(NotificationLevel.ERROR, "e")
    finally:
        await n.close()

    bodies = [json.loads(call.request.read())["text"] for call in route.calls]
    assert bodies[0].startswith(":white_check_mark:")
    assert bodies[1].startswith(":warning:")
    assert bodies[2].startswith(":x:")


@respx.mock
async def test_post_swallows_4xx() -> None:
    respx.post(URL).mock(return_value=httpx.Response(404, text="not found"))
    n = _channel()
    try:
        await n.notify(NotificationLevel.INFO, "hi")  # must not raise
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
