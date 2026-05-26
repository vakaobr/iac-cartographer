"""Tests for the Opsgenie Alerts API notification channel.

Verifies:
  * Payload schema (message / description / priority / source / details).
  * Level → priority mapping (info → P5, warn → P3, error → P1).
  * `message` field truncated at the 130-char API limit; full text
    preserved in `description`.
  * `Authorization: GenieKey <key>` header (Opsgenie-specific scheme,
    NOT regular Bearer).
  * US (default) vs EU region routing — different hostname.
  * Standard swallow-errors + lazy-init + safe-close contract.
"""

from __future__ import annotations

import json

import httpx
import respx

from iac_cartographer.models import OpsgenieCredentials
from iac_cartographer.notifications import NotificationLevel
from iac_cartographer.notifications.opsgenie import OpsgenieChannel, _build_alert

US_URL = "https://api.opsgenie.com/v2/alerts"
EU_URL = "https://api.eu.opsgenie.com/v2/alerts"


def _channel(region: str = "us") -> OpsgenieChannel:
    return OpsgenieChannel(OpsgenieCredentials(api_key="og-k3y"), region=region)  # type: ignore[arg-type]


# ── Pure payload shape ────────────────────────────────────────────────


def test_build_alert_envelope_shape() -> None:
    body = _build_alert(level=NotificationLevel.ERROR, message="kaboom")
    assert body["message"] == "kaboom"
    assert body["description"] == "kaboom"
    assert body["priority"] == "P1"
    assert body["source"] == "iac-cartographer"
    assert body["details"] == {"level": "error"}


def test_priority_map_covers_all_levels() -> None:
    """info → P5 (silent queue), warn → P3, error → P1 (page)."""
    assert _build_alert(level=NotificationLevel.INFO, message="m")["priority"] == "P5"
    assert _build_alert(level=NotificationLevel.WARN, message="m")["priority"] == "P3"
    assert _build_alert(level=NotificationLevel.ERROR, message="m")["priority"] == "P1"


def test_message_truncates_at_130_chars() -> None:
    """Opsgenie's `message` is hard-capped at 130 chars; full text
    survives in `description` (15000-char limit)."""
    long_msg = "x" * 500
    body = _build_alert(level=NotificationLevel.ERROR, message=long_msg)
    assert len(body["message"]) == 130  # type: ignore[arg-type]
    assert body["description"] == long_msg  # full text preserved


# ── notify() over httpx (mocked via respx) ───────────────────────────


@respx.mock
async def test_notify_posts_to_us_region_by_default() -> None:
    route = respx.post(US_URL).mock(return_value=httpx.Response(202, json={}))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.ERROR, "boom")
    finally:
        await ch.close()

    assert route.called
    body = json.loads(route.calls[0].request.read())
    assert body["priority"] == "P1"
    assert body["message"] == "boom"


@respx.mock
async def test_notify_targets_eu_host_when_region_is_eu() -> None:
    """EU customers MUST get routed to api.eu.opsgenie.com — the two
    planes are not linked, so a US-host request with an EU key
    would 401."""
    route = respx.post(EU_URL).mock(return_value=httpx.Response(202, json={}))
    ch = _channel(region="eu")
    try:
        await ch.notify(NotificationLevel.ERROR, "boom")
    finally:
        await ch.close()

    assert route.called


@respx.mock
async def test_authorization_header_uses_geniekey_scheme() -> None:
    route = respx.post(US_URL).mock(return_value=httpx.Response(202, json={}))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.INFO, "hi")
    finally:
        await ch.close()

    auth = route.calls[0].request.headers["Authorization"]
    assert auth == "GenieKey og-k3y"


@respx.mock
async def test_post_swallows_4xx() -> None:
    respx.post(US_URL).mock(return_value=httpx.Response(401, text="wrong region or bad key"))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.ERROR, "hi")  # must not raise
    finally:
        await ch.close()


@respx.mock
async def test_post_swallows_5xx() -> None:
    respx.post(US_URL).mock(return_value=httpx.Response(503, text="upstream"))
    ch = _channel()
    try:
        await ch.notify(NotificationLevel.WARN, "hi")  # must not raise
    finally:
        await ch.close()


@respx.mock
async def test_post_swallows_network_exception() -> None:
    respx.post(US_URL).mock(side_effect=httpx.ConnectError("network down"))
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
    respx.post(US_URL).mock(return_value=httpx.Response(202, json={}))
    ch = _channel()
    assert ch._client is None
    try:
        await ch.notify(NotificationLevel.INFO, "hello")
        assert ch._client is not None
    finally:
        await ch.close()
    assert ch._client is None
