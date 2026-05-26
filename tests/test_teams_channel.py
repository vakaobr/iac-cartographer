"""Tests for the Microsoft Teams notification channel.

Verifies the Adaptive Card v1.4 envelope shape, severity → colour
mapping, unicode-emoji prefixes (Teams does NOT render Slack-style
shortcodes), and standard error-swallow / lazy-init behaviour.
"""

from __future__ import annotations

import json

import httpx
import respx

from iac_cartographer.models import TeamsCredentials
from iac_cartographer.notifications import NotificationLevel
from iac_cartographer.notifications.teams import TeamsChannel, _build_adaptive_card

URL = "https://prod-00.westeurope.logic.azure.com:443/workflows/xxx/triggers/manual"


def _channel() -> TeamsChannel:
    return TeamsChannel(TeamsCredentials(url=URL))


def test_build_adaptive_card_envelope_for_info() -> None:
    """Pure function — verifies the card shape without going through httpx."""
    card = _build_adaptive_card(NotificationLevel.INFO, "starting up")
    assert card["type"] == "message"
    attachments = card["attachments"]
    assert len(attachments) == 1  # type: ignore[arg-type]
    content = attachments[0]["content"]  # type: ignore[call-overload, index]
    assert content["type"] == "AdaptiveCard"
    assert content["version"] == "1.4"
    body = content["body"]
    assert len(body) == 1
    block = body[0]
    assert block["type"] == "TextBlock"
    assert "starting up" in block["text"]
    assert block["color"] == "good"
    assert block["wrap"] is True


def test_severity_colour_mapping() -> None:
    info_card = _build_adaptive_card(NotificationLevel.INFO, "i")
    warn_card = _build_adaptive_card(NotificationLevel.WARN, "w")
    error_card = _build_adaptive_card(NotificationLevel.ERROR, "e")

    def _colour(card: dict) -> str:
        return card["attachments"][0]["content"]["body"][0]["color"]

    assert _colour(info_card) == "good"
    assert _colour(warn_card) == "warning"
    assert _colour(error_card) == "attention"


def test_unicode_emoji_prefix_per_severity() -> None:
    """Teams ignores `:emoji:` shortcodes — we use actual unicode chars."""

    def _text(card: dict) -> str:
        return card["attachments"][0]["content"]["body"][0]["text"]

    assert _text(_build_adaptive_card(NotificationLevel.INFO, "m")).startswith("✅")
    assert _text(_build_adaptive_card(NotificationLevel.WARN, "m")).startswith("⚠️")
    assert _text(_build_adaptive_card(NotificationLevel.ERROR, "m")).startswith("❌")


@respx.mock
async def test_notify_posts_adaptive_card_payload() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(202))
    n = _channel()
    try:
        await n.notify(NotificationLevel.ERROR, "boom")
    finally:
        await n.close()

    body = json.loads(route.calls[0].request.read())
    assert body["type"] == "message"
    block = body["attachments"][0]["content"]["body"][0]
    assert "boom" in block["text"]
    assert block["color"] == "attention"


@respx.mock
async def test_post_swallows_4xx() -> None:
    respx.post(URL).mock(return_value=httpx.Response(400, text="malformed card"))
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
