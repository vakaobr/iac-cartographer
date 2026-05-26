"""Phase 8 tests for the Slack notification channel.

The Slack module moved from `iac_cartographer.slack` into the
`iac_cartographer.notifications` package as part of the multi-channel
refactor. The old `SlackNotifier.info()` / `.warn()` / `.error()`
methods collapsed into a single `SlackChannel.notify(level, message)`
matching the new `NotificationChannel` ABC — these tests adapt by
calling `notify(NotificationLevel.X, ...)` directly. End-to-end
behaviour (best-effort post, error swallowing, lazy client init) is
unchanged.
"""

from __future__ import annotations

import httpx
import respx

from iac_cartographer.models import SlackCredentials
from iac_cartographer.notifications import NotificationLevel
from iac_cartographer.notifications.slack import SLACK_BASE_URL, SlackChannel


def _channel() -> SlackChannel:
    return SlackChannel(SlackCredentials(bot_token="xoxb-token", channel_id="C0X"))


@respx.mock
async def test_info_posts_with_check_mark_prefix() -> None:
    route = respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    n = _channel()
    try:
        await n.notify(NotificationLevel.INFO, "hello world")
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
    route = respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    n = _channel()
    try:
        await n.notify(NotificationLevel.WARN, "be careful")
    finally:
        await n.close()
    body = route.calls[0].request.read().decode()
    assert ":warning:" in body


@respx.mock
async def test_error_posts_with_x_prefix() -> None:
    route = respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    n = _channel()
    try:
        await n.notify(NotificationLevel.ERROR, "boom")
    finally:
        await n.close()
    body = route.calls[0].request.read().decode()
    assert ":x:" in body


@respx.mock
async def test_post_swallows_500() -> None:
    respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(
        return_value=httpx.Response(500, json={"ok": False, "error": "server"})
    )
    n = _channel()
    try:
        await n.notify(NotificationLevel.INFO, "hi")  # must not raise
    finally:
        await n.close()


@respx.mock
async def test_post_swallows_ok_false() -> None:
    respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
    )
    n = _channel()
    try:
        await n.notify(NotificationLevel.INFO, "hi")  # must not raise
    finally:
        await n.close()


@respx.mock
async def test_post_swallows_exception() -> None:
    respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(side_effect=httpx.ConnectError("network down"))
    n = _channel()
    try:
        await n.notify(NotificationLevel.INFO, "hi")  # must not raise
    finally:
        await n.close()


def test_channel_override_supersedes_credentials() -> None:
    n = SlackChannel(
        SlackCredentials(bot_token="xoxb", channel_id="C-default"),
        channel="#alerts",
    )
    assert n._channel == "#alerts"


def test_constructor_does_not_open_httpx_client() -> None:
    """Lazy-init: a SlackChannel that is never used (e.g. on a --dry-run
    path) must not allocate a connection pool. Avoids wasted handshakes
    when Slack isn't going to be called."""
    n = SlackChannel(SlackCredentials(bot_token="xoxb", channel_id="C0X"))
    assert n._client is None


async def test_close_without_use_is_safe() -> None:
    """`close()` on a never-used channel must not raise."""
    n = SlackChannel(SlackCredentials(bot_token="xoxb", channel_id="C0X"))
    await n.close()
    assert n._client is None


@respx.mock
async def test_client_initialized_lazily_on_first_post() -> None:
    respx.post(f"{SLACK_BASE_URL}/chat.postMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    n = SlackChannel(SlackCredentials(bot_token="xoxb", channel_id="C0X"))
    assert n._client is None
    try:
        await n.notify(NotificationLevel.INFO, "hello")
        assert n._client is not None  # opened on first post
    finally:
        await n.close()
    assert n._client is None  # close() releases it
