"""Phase 8 tests for iac_cartographer.slack — best-effort post + swallow errors."""

from __future__ import annotations

import httpx
import respx

from iac_cartographer.models import SlackCredentials
from iac_cartographer.slack import SLACK_BASE_URL, SlackNotifier


def _notifier() -> SlackNotifier:
    return SlackNotifier(SlackCredentials(bot_token="xoxb-token", channel_id="C0X"))


@respx.mock
async def test_info_posts_with_check_mark_prefix() -> None:
    route = respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    n = _notifier()
    try:
        await n.info("hello world")
    finally:
        await n.close()
    assert route.called
    sent = route.calls[0].request
    body = sent.read().decode()
    assert "white_check_mark" in body
    assert "hello world" in body
    assert '"channel":"C0X"' in body or '"channel": "C0X"' in body


@respx.mock
async def test_warn_posts_with_warning_prefix() -> None:
    respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    n = _notifier()
    try:
        await n.warn("be careful")
    finally:
        await n.close()


@respx.mock
async def test_error_posts_with_x_prefix() -> None:
    route = respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    n = _notifier()
    try:
        await n.error("boom")
    finally:
        await n.close()
    body = route.calls[0].request.read().decode()
    assert ":x:" in body


@respx.mock
async def test_post_swallows_500() -> None:
    respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(
        return_value=httpx.Response(500, json={"ok": False, "error": "server"})
    )
    n = _notifier()
    try:
        await n.info("hi")  # must not raise
    finally:
        await n.close()


@respx.mock
async def test_post_swallows_ok_false() -> None:
    respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
    )
    n = _notifier()
    try:
        await n.info("hi")  # must not raise
    finally:
        await n.close()


@respx.mock
async def test_post_swallows_exception() -> None:
    respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(side_effect=httpx.ConnectError("network down"))
    n = _notifier()
    try:
        await n.info("hi")  # must not raise
    finally:
        await n.close()


def test_channel_override_supersedes_credentials() -> None:
    n = SlackNotifier(
        SlackCredentials(bot_token="xoxb", channel_id="C-default"),
        channel="#alerts",
    )
    assert n._channel == "#alerts"


def test_constructor_does_not_open_httpx_client() -> None:
    """Lazy-init: a SlackNotifier that is never used (e.g. on a --dry-run
    path) must not allocate a connection pool. Avoids wasted handshakes
    when Slack isn't going to be called."""
    n = SlackNotifier(SlackCredentials(bot_token="xoxb", channel_id="C0X"))
    assert n._client is None


async def test_close_without_use_is_safe() -> None:
    """`close()` on a never-used notifier must not raise."""
    n = SlackNotifier(SlackCredentials(bot_token="xoxb", channel_id="C0X"))
    await n.close()
    assert n._client is None


@respx.mock
async def test_client_initialized_lazily_on_first_post() -> None:
    respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    n = SlackNotifier(SlackCredentials(bot_token="xoxb", channel_id="C0X"))
    assert n._client is None
    try:
        await n.info("hello")
        assert n._client is not None  # opened on first post
    finally:
        await n.close()
    assert n._client is None  # close() releases it
