"""Email (SMTP) notification channel.

Sends multipart/alternative messages with a plain-text fallback and an
HTML body that renders severity as a coloured header. Tuned for the
operator inbox shape — short, scannable subject line carrying the
severity + first ~60 chars of the message; the full message lands in
the body.

Transport: `aiosmtplib`, lazy-imported on first `notify()` so the base
install doesn't pay for the SDK when email isn't in use. Install with:

    pip install 'iac-cartographer[email]'

Auth: SMTP username + password from the `iac-cartographer/email`
secret. Most managed providers fit this shape (Postmark, SendGrid,
Mailgun, AWS SES SMTP credentials, internal Postfix relays).

TLS: STARTTLS by default (port 587). Set `use_tls: false` only when
talking to an internal relay that's already on an authenticated
network (in-cluster Postfix without TLS, etc.) — never on the public
internet.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel

if TYPE_CHECKING:
    from iac_cartographer.models import EmailCredentials

logger = logging.getLogger("iac_cartographer.notifications.email")

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_SUBJECT_PREFIX = "[iac-cartographer]"

# HTML colours per severity — match the operator-facing convention of
# the Teams Adaptive Card mapping (good / warning / attention) so a
# notification looks consistent across channels.
_LEVEL_META: dict[NotificationLevel, tuple[str, str, str]] = {
    # (emoji, html-colour, plain-text-label)
    NotificationLevel.INFO: ("✅", "#1f883d", "INFO"),
    NotificationLevel.WARN: ("⚠️", "#bf8700", "WARN"),
    NotificationLevel.ERROR: ("❌", "#cf222e", "ERROR"),
}


class _AiosmtplibImportError(ImportError):
    """Raised when aiosmtplib isn't installed at first notify()."""


def _build_message(
    *,
    level: NotificationLevel,
    message: str,
    from_address: str,
    to_addresses: list[str],
    subject_prefix: str,
) -> EmailMessage:
    """Compose the multipart/alternative message.

    Pulled out so tests can inspect the rendered email without a live
    SMTP server. The HTML body is intentionally minimal — single
    coloured header + the message inside a `<pre>` for whitespace
    preservation. Operators reading on a phone get a readable layout
    without any external CSS / images / fonts.
    """
    emoji, colour, label = _LEVEL_META[level]

    # Subject: prefix + level + first ~60 chars. Caps subject length
    # so it stays scannable in inbox previews. Truncation with "…"
    # signals the body has more.
    truncated = message if len(message) <= 60 else message[:57] + "…"
    subject = f"{subject_prefix}[{label}] {truncated}"

    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = ", ".join(to_addresses)
    msg["Subject"] = subject

    plain = f"{label}: {message}\n\n— iac-cartographer"
    html = (
        '<!DOCTYPE html><html><body style="font-family:system-ui,sans-serif;">'
        f'<div style="background:{colour};color:#fff;padding:8px 12px;'
        f'border-radius:4px;font-weight:600;">{emoji} {label}</div>'
        f'<pre style="white-space:pre-wrap;font-family:ui-monospace,monospace;'
        f'margin:12px 0;">{_html_escape(message)}</pre>'
        '<div style="color:#666;font-size:12px;">— iac-cartographer</div>'
        "</body></html>"
    )
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return msg


def _html_escape(s: str) -> str:
    """Minimal HTML escape — defence against the rare case where a
    pipeline message contains user-influenced data (e.g. a repo name
    or commit message embedded in an error string). Inline because the
    rendered output is <pre>-wrapped and we don't need full
    `html.escape()`-quality coverage."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class EmailChannel(NotificationChannel):
    """SMTP-backed notification channel."""

    name = "email"

    def __init__(
        self,
        creds: EmailCredentials,
        *,
        smtp_host: str,
        smtp_port: int = 587,
        from_address: str,
        to_addresses: list[str],
        use_tls: bool = True,
        subject_prefix: str = DEFAULT_SUBJECT_PREFIX,
    ) -> None:
        self._username = creds.username
        self._password = creds.password
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._from = from_address
        self._to = list(to_addresses)
        self._use_tls = use_tls
        self._subject_prefix = subject_prefix

    async def notify(self, level: NotificationLevel, message: str) -> None:
        try:
            smtp_cls = self._aiosmtplib_smtp_class()
        except _AiosmtplibImportError:
            logger.warning(
                "email: aiosmtplib is not installed — run `pip install "
                "'iac-cartographer[email]'` to enable the email channel "
                "(skipping %s notification)",
                level.value,
            )
            return

        msg = _build_message(
            level=level,
            message=message,
            from_address=self._from,
            to_addresses=self._to,
            subject_prefix=self._subject_prefix,
        )

        try:
            # `start_tls=True` is the STARTTLS path on port 587 (the
            # modern submission port). For port 465 (legacy implicit
            # TLS) callers would need a different flag; keeping the
            # initial channel surface simple — only STARTTLS supported.
            await smtp_cls.send(
                msg,
                hostname=self._smtp_host,
                port=self._smtp_port,
                username=self._username,
                password=self._password,
                start_tls=self._use_tls,
                timeout=DEFAULT_TIMEOUT_S,
            )
        except Exception:
            logger.warning("email: %s send raised", level.value, exc_info=True)

    def _aiosmtplib_smtp_class(self) -> Any:
        """Lazy import — returns the aiosmtplib module itself so the
        caller can use `.send(...)` against it."""
        try:
            import aiosmtplib
        except ImportError as exc:
            raise _AiosmtplibImportError("email channel requires `pip install 'iac-cartographer[email]'`") from exc
        return aiosmtplib
