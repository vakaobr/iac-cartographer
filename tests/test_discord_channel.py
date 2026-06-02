"""Tests for the Discord webhook notification channel.

Verifies:
  * Payload shape (`content` with unicode-emoji prefix; optional
    `username` and `avatar_url` overrides).
  * Severity → unicode-emoji mapping (✅ / ⚠️ / ❌).
  * Content truncation at the 2000-char Discord webhook limit (with
    `…` truncation marker so operators see the cut).
  * Standard error-swallow + lazy-init + safe-close contract.
"""

from __future__ import annotations

import json

import httpx
import respx

from iac_cartographer.models import DiscordCredentials
from iac_cartographer.notifications import NotificationLevel
from iac_cartographer.notifications.discord import DiscordChannel

URL = "https://discord.com/api/webhooks/0/abc"


def _channel(**kwargs: object) -> DiscordChannel:
    return DiscordChannel(DiscordCredentials(url=URL), **kwargs)  # type: ignore[arg-type]


# ── Payload shape ─────────────────────────────────────────────────────


@respx.mock
async def test_payload_carries_content_with_emoji_prefix() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.WARN, "be careful")
    finally:
        await ch.close()

    body = json.loads(route.calls[0].request.read())
    assert "be careful" in body["content"]
    assert body["content"].startswith("⚠️")


@respx.mock
async def test_unicode_emoji_per_severity() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.INFO, "i")
        await ch.notify(NotificationLevel.WARN, "w")
        await ch.notify(NotificationLevel.ERROR, "e")
    finally:
        await ch.close()

    contents = [json.loads(call.request.read())["content"] for call in route.calls]
    assert contents[0].startswith("✅")
    assert contents[1].startswith("⚠️")
    assert contents[2].startswith("❌")


@respx.mock
async def test_username_and_avatar_overrides_appear_when_set() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    ch = _channel(
        username="iac-cartographer (prod)",
        avatar_url="https://example.com/avatar.png",
    )
    try:
        await ch.notify(NotificationLevel.INFO, "hi")
    finally:
        await ch.close()

    body = json.loads(route.calls[0].request.read())
    assert body["username"] == "iac-cartographer (prod)"
    assert body["avatar_url"] == "https://example.com/avatar.png"


@respx.mock
async def test_username_and_avatar_omitted_when_unset() -> None:
    """No keys → Discord uses the webhook's default identity."""
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.INFO, "hi")
    finally:
        await ch.close()

    body = json.loads(route.calls[0].request.read())
    assert "username" not in body
    assert "avatar_url" not in body


# ── thread_id (#78) ───────────────────────────────────────────────────


@respx.mock
async def test_thread_id_appears_as_query_param_when_set() -> None:
    """`thread_id` routes the message into a specific Discord thread —
    Discord's webhook API accepts it as a query parameter on the POST."""
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    ch = _channel(thread_id="1234567890123456789")
    try:
        await ch.notify(NotificationLevel.INFO, "into the thread")
    finally:
        await ch.close()

    assert route.calls.call_count == 1
    sent_url = route.calls[0].request.url
    # respx exposes the parsed query string via `.params`.
    assert sent_url.params.get("thread_id") == "1234567890123456789"
    # Payload itself is unchanged — `thread_id` is a query param, NOT a body field.
    body = json.loads(route.calls[0].request.read())
    assert "thread_id" not in body
    assert "into the thread" in body["content"]


@respx.mock
async def test_thread_id_absent_when_unset() -> None:
    """Default behaviour (no `thread_id` configured) posts to the
    channel's main feed — no query parameter on the URL."""
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.INFO, "main feed")
    finally:
        await ch.close()

    sent_url = route.calls[0].request.url
    assert sent_url.params.get("thread_id") is None
    # Belt-and-braces: the raw URL string has no `?thread_id=` segment either.
    assert "thread_id" not in str(sent_url)


@respx.mock
async def test_thread_id_combines_with_username_override() -> None:
    """`thread_id` (query param) and `username` (body field) are
    orthogonal — both should appear when both are configured."""
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    ch = _channel(thread_id="999", username="iac-cartographer (prod)")
    try:
        await ch.notify(NotificationLevel.WARN, "hi")
    finally:
        await ch.close()

    sent = route.calls[0].request
    assert sent.url.params.get("thread_id") == "999"
    assert json.loads(sent.read())["username"] == "iac-cartographer (prod)"


@respx.mock
async def test_content_truncates_at_2000_chars_with_marker() -> None:
    """Discord rejects content > 2000 chars with a 400. We truncate
    before sending and add `…` so operators know the message was cut."""
    route = respx.post(URL).mock(return_value=httpx.Response(204))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.ERROR, "x" * 5000)
    finally:
        await ch.close()

    body = json.loads(route.calls[0].request.read())
    assert len(body["content"]) == 2000
    assert body["content"].endswith("…")


# ── Error handling ────────────────────────────────────────────────────


@respx.mock
async def test_post_swallows_4xx() -> None:
    respx.post(URL).mock(return_value=httpx.Response(400, text="bad webhook"))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.ERROR, "hi")  # must not raise
    finally:
        await ch.close()


@respx.mock
async def test_post_swallows_rate_limit_429() -> None:
    """Discord's per-webhook rate limit returns 429. Log + skip."""
    respx.post(URL).mock(return_value=httpx.Response(429, text="rate limited"))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.INFO, "hi")  # must not raise
    finally:
        await ch.close()


@respx.mock
async def test_post_swallows_network_exception() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError("network down"))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.WARN, "hi")  # must not raise
    finally:
        await ch.close()


def test_constructor_does_not_open_httpx_client() -> None:
    ch = _channel()
    assert ch._client is None


async def test_close_without_use_is_safe() -> None:
    ch = _channel()
    await ch.close()
    assert ch._client is None
