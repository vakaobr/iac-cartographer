"""Stdout / stderr JSON Lines notification channel.

Emits one structured JSON line per notification to a configured
stream — useful for:

  * **CI runs** that don't have access to chat platforms but DO have
    log capture (GitHub Actions, GitLab CI, Jenkins, etc.).
  * **Air-gapped deployments** where outbound HTTP to chat / pager /
    SMTP isn't permitted but a log aggregator picks up stdout.
  * **Local dev / smoke tests** where you want to see notifications
    without configuring a real destination.

No I/O cost beyond a `print()` — no HTTP client, no SDK, no
credentials, no network. The channel uses the same payload schema as
the generic webhook channel so downstream log-parsing tooling can
treat both interchangeably:

    {"schema": "iac-cartographer.notification.v1",
     "level": "info" | "warn" | "error",
     "message": "...",
     "ts": "2026-05-26T10:30:00Z",
     "source": "iac-cartographer"}

`stream` config selects stdout (default) vs stderr. Stderr is the
right choice when stdout is reserved for machine-parseable pipeline
output (Markdown publisher dumps, the renderer's banner-SHA logs,
etc.).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Literal, TextIO

from iac_cartographer.notifications.base import NotificationChannel, NotificationLevel

logger = logging.getLogger("iac_cartographer.notifications.stdout")

# Same payload-schema sentinel the generic webhook channel uses, so
# log-aggregator parsers can treat both kinds interchangeably.
PAYLOAD_SCHEMA = "iac-cartographer.notification.v1"

_STREAMS: dict[str, TextIO] = {
    "stdout": sys.stdout,
    "stderr": sys.stderr,
}


class StdoutChannel(NotificationChannel):
    """Print JSON Lines notifications to a TextIO stream."""

    name = "stdout"

    def __init__(self, *, stream: Literal["stdout", "stderr"] = "stdout") -> None:
        # We resolve the stream literal to the actual file object at
        # construction so tests can monkeypatch `sys.stdout` /
        # `sys.stderr` and have the channel pick up the patched value
        # — store the literal, look up on each notify().
        self._stream_name = stream

    def _stream(self) -> TextIO:
        # Late-bind so monkeypatched sys.stdout / sys.stderr (pytest's
        # `capsys` fixture, the user's own log capture, etc.) take
        # effect. Cheaper than caching: a dict lookup per call.
        if self._stream_name == "stderr":
            return sys.stderr
        return sys.stdout

    async def notify(self, level: NotificationLevel, message: str) -> None:
        line = json.dumps(
            {
                "schema": PAYLOAD_SCHEMA,
                "level": level.value,
                "message": message,
                "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "source": "iac-cartographer",
            },
            ensure_ascii=False,
        )
        try:
            # `print` is synchronous — no await needed. Wrapped in the
            # async `notify` coroutine to match the NotificationChannel
            # contract. Print is the only I/O that happens; there's
            # nothing to block the event loop meaningfully.
            print(line, file=self._stream(), flush=True)
        except Exception:
            # Defence-in-depth: stdout/stderr write CAN fail (closed
            # FD, redirected to a full disk, etc.). Log + swallow.
            logger.warning("stdout: %s write raised", level.value, exc_info=True)
