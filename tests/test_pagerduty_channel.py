"""Tests for the PagerDuty Events API v2 notification channel.

Verifies payload shape (routing_key + event_action + payload subobject),
severity-mapping, summary truncation at the 1024-char API limit, the
swallow-errors + lazy-init contract, and the timestamp format.
"""

from __future__ import annotations

import json
import re

import httpx
import respx

from iac_cartographer.models import PagerDutyCredentials
from iac_cartographer.notifications import NotificationLevel
from iac_cartographer.notifications.pagerduty import (
    EVENTS_API_URL,
    PagerDutyChannel,
    _build_event,
)


def _channel() -> PagerDutyChannel:
    return PagerDutyChannel(PagerDutyCredentials(routing_key="r0utingk3y"))


# ── Pure payload shape ────────────────────────────────────────────────


def test_build_event_envelope_shape() -> None:
    body = _build_event(routing_key="abc", level=NotificationLevel.ERROR, message="kaboom")
    assert body["routing_key"] == "abc"
    assert body["event_action"] == "trigger"
    payload = body["payload"]
    assert payload["summary"] == "kaboom"
    assert payload["severity"] == "error"
    assert payload["source"] == "iac-cartographer"
    # ISO 8601 UTC with the trailing Z (not +00:00) for consistency
    # with the generic-webhook channel's `ts` field.
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", str(payload["timestamp"]))
    assert str(payload["timestamp"]).endswith("Z")


def test_severity_map_covers_all_levels() -> None:
    """info → info, warn → warning, error → error."""
    assert _build_event(routing_key="r", level=NotificationLevel.INFO, message="m")["payload"]["severity"] == "info"
    assert _build_event(routing_key="r", level=NotificationLevel.WARN, message="m")["payload"]["severity"] == "warning"
    assert _build_event(routing_key="r", level=NotificationLevel.ERROR, message="m")["payload"]["severity"] == "error"


def test_summary_truncates_at_1024_chars() -> None:
    """Events API hard-limits `summary` at 1024 chars. We truncate
    BEFORE sending to keep the channel well-behaved against
    pathologically int error strings."""
    long_msg = "x" * 5000
    body = _build_event(routing_key="r", level=NotificationLevel.ERROR, message=long_msg)
    assert len(body["payload"]["summary"]) == 1024  # type: ignore[arg-type]


# ── notify() over httpx (mocked via respx) ───────────────────────────


@respx.mock
async def test_notify_posts_to_events_api_with_routing_key() -> None:
    route = respx.post(EVENTS_API_URL).mock(return_value=httpx.Response(202))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.ERROR, "boom")
    finally:
        await ch.close()

    body = json.loads(route.calls[0].request.read())
    assert body["routing_key"] == "r0utingk3y"
    assert body["event_action"] == "trigger"
    assert body["payload"]["severity"] == "error"
    assert body["payload"]["summary"] == "boom"


@respx.mock
async def test_post_swallows_4xx() -> None:
    respx.post(EVENTS_API_URL).mock(return_value=httpx.Response(400, text="bad routing_key"))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.ERROR, "hi")  # must not raise
    finally:
        await ch.close()


@respx.mock
async def test_post_swallows_5xx() -> None:
    respx.post(EVENTS_API_URL).mock(return_value=httpx.Response(503, text="upstream"))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.WARN, "hi")  # must not raise
    finally:
        await ch.close()


@respx.mock
async def test_post_swallows_network_exception() -> None:
    respx.post(EVENTS_API_URL).mock(side_effect=httpx.ConnectError("network down"))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.ERROR, "hi")  # must not raise
    finally:
        await ch.close()


def test_constructor_does_not_open_httpx_client() -> None:
    ch = _channel()
    assert ch._client is None


async def test_close_without_use_is_safe() -> None:
    ch = _channel()
    await ch.close()
    assert ch._client is None


@respx.mock
async def test_client_initialized_lazily_on_first_post() -> None:
    respx.post(EVENTS_API_URL).mock(return_value=httpx.Response(202))
    ch = _channel()
    assert ch._client is None
    try:
        await ch.notify(NotificationLevel.INFO, "hello")
        assert ch._client is not None  # opened on first post
    finally:
        await ch.close()
    assert ch._client is None  # close() released it
