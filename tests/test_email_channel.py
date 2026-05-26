"""Tests for the SMTP email notification channel.

Verifies:
  * `_build_message` produces a valid multipart/alternative shape with
    severity-coloured HTML header + plain-text fallback + scannable
    subject line.
  * Subject truncation past 60 chars (with `…` suffix).
  * Severity → colour + emoji mapping.
  * `EmailChannel.notify` calls `aiosmtplib.send` with the expected
    SMTP kwargs and the rendered message — mocked via
    `unittest.mock.AsyncMock` to avoid a live SMTP server.
  * The channel logs + skips when `aiosmtplib` is missing (defence
    against the optional-dep extra not being installed).
  * Standard error-swallow + safe-close contract.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

from iac_cartographer.models import EmailCredentials
from iac_cartographer.notifications import NotificationLevel
from iac_cartographer.notifications.email import (
    _LEVEL_META,
    EmailChannel,
    _build_message,
)


def _channel(**overrides: object) -> EmailChannel:
    defaults: dict[str, object] = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "from_address": "noreply@example.com",
        "to_addresses": ["ops@example.com"],
        "use_tls": True,
    }
    defaults.update(overrides)
    return EmailChannel(
        EmailCredentials(username="u", password="p"),
        **defaults,  # type: ignore[arg-type]
    )


# ── Pure rendering ────────────────────────────────────────────────────


def test_build_message_subject_carries_prefix_level_and_message() -> None:
    msg = _build_message(
        level=NotificationLevel.WARN,
        message="be careful",
        from_address="noreply@example.com",
        to_addresses=["ops@example.com"],
        subject_prefix="[iac-cartographer]",
    )
    subject = msg["Subject"]
    assert "[iac-cartographer]" in subject
    assert "[WARN]" in subject
    assert "be careful" in subject


def test_build_message_subject_truncates_past_60_chars() -> None:
    long_msg = "x" * 200
    msg = _build_message(
        level=NotificationLevel.INFO,
        message=long_msg,
        from_address="noreply@example.com",
        to_addresses=["ops@example.com"],
        subject_prefix="[iac]",
    )
    # The body of the subject (after `[iac][INFO] `) is the first 57
    # chars of the message + `…`.
    assert msg["Subject"].endswith("…")
    # Confirm we didn't dump the entire 200-char message into the subject.
    assert len(msg["Subject"]) < 100


def test_build_message_is_multipart_alternative_with_plain_and_html() -> None:
    msg = _build_message(
        level=NotificationLevel.ERROR,
        message="kaboom",
        from_address="noreply@example.com",
        to_addresses=["ops@example.com"],
        subject_prefix="[iac]",
    )
    # `EmailMessage.iter_parts()` yields the alternative parts after
    # `add_alternative()` was called.
    payloads = {part.get_content_type(): part.get_content() for part in msg.iter_parts()}
    assert "text/plain" in payloads
    assert "text/html" in payloads
    assert "kaboom" in payloads["text/plain"]
    assert "kaboom" in payloads["text/html"]


def test_html_body_carries_severity_colour() -> None:
    """Each level gets its own colour from _LEVEL_META."""
    for level in NotificationLevel:
        _, colour, _ = _LEVEL_META[level]
        msg = _build_message(
            level=level,
            message="m",
            from_address="noreply@example.com",
            to_addresses=["ops@example.com"],
            subject_prefix="[iac]",
        )
        html = next(part.get_content() for part in msg.iter_parts() if part.get_content_type() == "text/html")
        assert colour in html


def test_html_body_escapes_angle_brackets_in_message() -> None:
    """Defence-in-depth: pipeline error strings sometimes contain
    repo names / commit messages with `<` / `>` / `&` characters."""
    msg = _build_message(
        level=NotificationLevel.ERROR,
        message="oops <script>x</script>",
        from_address="noreply@example.com",
        to_addresses=["ops@example.com"],
        subject_prefix="[iac]",
    )
    html = next(part.get_content() for part in msg.iter_parts() if part.get_content_type() == "text/html")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_multiple_recipients_join_to_one_to_header() -> None:
    msg = _build_message(
        level=NotificationLevel.INFO,
        message="m",
        from_address="noreply@example.com",
        to_addresses=["a@example.com", "b@example.com"],
        subject_prefix="[iac]",
    )
    assert msg["To"] == "a@example.com, b@example.com"


# ── notify() with aiosmtplib mocked ───────────────────────────────────


async def test_notify_calls_aiosmtplib_send_with_expected_kwargs() -> None:
    fake_aiosmtplib = type(sys)("fake_aiosmtplib")
    fake_aiosmtplib.send = AsyncMock(return_value=None)

    with patch.dict(sys.modules, {"aiosmtplib": fake_aiosmtplib}):
        ch = _channel(subject_prefix="[iac]")
        await ch.notify(NotificationLevel.INFO, "hello world")

    assert fake_aiosmtplib.send.await_count == 1
    _, kwargs = fake_aiosmtplib.send.await_args
    assert kwargs["hostname"] == "smtp.example.com"
    assert kwargs["port"] == 587
    assert kwargs["username"] == "u"
    assert kwargs["password"] == "p"
    assert kwargs["start_tls"] is True


async def test_notify_skips_when_aiosmtplib_missing() -> None:
    """ImportError on aiosmtplib → log + skip (no raise)."""
    # Force the import to fail by injecting a None placeholder.
    with patch.dict(sys.modules, {"aiosmtplib": None}):
        ch = _channel()
        # Must not raise; the channel logs and returns.
        await ch.notify(NotificationLevel.WARN, "hello")


async def test_notify_swallows_smtp_exception() -> None:
    fake_aiosmtplib = type(sys)("fake_aiosmtplib")
    fake_aiosmtplib.send = AsyncMock(side_effect=ConnectionError("smtp down"))

    with patch.dict(sys.modules, {"aiosmtplib": fake_aiosmtplib}):
        ch = _channel()
        await ch.notify(NotificationLevel.ERROR, "hi")  # must not raise


def test_constructor_stores_config_without_opening_smtp() -> None:
    """Construction is pure — no SMTP handshake until notify() runs."""
    ch = _channel()
    assert ch._smtp_host == "smtp.example.com"
    assert ch._smtp_port == 587


async def test_close_is_noop() -> None:
    """No connection pool to release — base-class default close() applies."""
    ch = _channel()
    await ch.close()  # must not raise


# Sanity check that the module-level metadata covers all three levels.
def test_level_meta_covers_all_levels() -> None:
    for level in NotificationLevel:
        assert level in _LEVEL_META
        emoji, colour, label = _LEVEL_META[level]
        assert emoji
        assert colour.startswith("#")
        assert label == level.value.upper()


pytest.mark.asyncio_mode = "auto"  # documents the conftest default
