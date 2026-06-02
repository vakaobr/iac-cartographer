"""Tests for the stdout / stderr JSON Lines notification channel.

Verifies:
  * One JSON line per notification with the schema-v1 payload shape
    (matches the generic webhook channel's wire format).
  * stdout (default) vs stderr stream selection.
  * Late-binding to `sys.stdout` / `sys.stderr` so pytest's `capsys`
    monkey-patching captures the output.
  * Standard severity literal in the `level` field.
  * Exception swallow on stream write failure.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from unittest.mock import patch

from iac_cartographer.notifications import NotificationLevel
from iac_cartographer.notifications.stdout import (
    PAYLOAD_SCHEMA,
    StdoutChannel,
)

# ── Default stream = stdout ───────────────────────────────────────────


async def test_writes_one_json_line_per_notify_to_stdout(capsys) -> None:
    ch = StdoutChannel()
    await ch.notify(NotificationLevel.INFO, "hello world")

    captured = capsys.readouterr()
    # Single line on stdout, none on stderr.
    assert captured.err == ""
    line = captured.out.strip()
    body = json.loads(line)
    assert body["schema"] == PAYLOAD_SCHEMA
    assert body["level"] == "info"
    assert body["message"] == "hello world"
    assert body["source"] == "iac-cartographer"
    assert body["ts"].endswith("Z")


async def test_each_level_serialises_to_its_string(capsys) -> None:
    ch = StdoutChannel()
    await ch.notify(NotificationLevel.INFO, "i")
    await ch.notify(NotificationLevel.WARN, "w")
    await ch.notify(NotificationLevel.ERROR, "e")

    out = capsys.readouterr().out.strip().splitlines()
    levels = [json.loads(line)["level"] for line in out]
    assert levels == ["info", "warn", "error"]


# ── stderr selection ──────────────────────────────────────────────────


async def test_stream_stderr_writes_to_stderr_not_stdout(capsys) -> None:
    ch = StdoutChannel(stream="stderr")
    await ch.notify(NotificationLevel.ERROR, "kaboom")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "kaboom" in captured.err


# ── Late-binding to sys.stdout / sys.stderr ──────────────────────────


async def test_late_binds_to_monkeypatched_sys_stdout() -> None:
    """Each notify() looks up sys.stdout fresh — captures
    monkey-patched stream replacements correctly."""
    ch = StdoutChannel()

    fake_stream = StringIO()
    with patch.object(sys, "stdout", fake_stream):
        await ch.notify(NotificationLevel.INFO, "captured")

    output = fake_stream.getvalue().strip()
    assert json.loads(output)["message"] == "captured"


# ── Error swallow ─────────────────────────────────────────────────────


async def test_notify_swallows_stream_write_exception() -> None:
    """A failing write (closed FD, full disk, …) MUST not propagate."""
    ch = StdoutChannel()

    class _ExplodingStream:
        def write(self, _: str) -> int:
            raise OSError("disk full")

        def flush(self) -> None:
            pass

    with patch.object(sys, "stdout", _ExplodingStream()):
        await ch.notify(NotificationLevel.ERROR, "hi")  # must not raise


# ── Construction is pure ──────────────────────────────────────────────


def test_constructor_does_no_io() -> None:
    """Construction never touches the stream — only notify() does."""
    StdoutChannel()
    StdoutChannel(stream="stderr")


async def test_close_is_noop() -> None:
    """No resource to release; base-class default close() applies."""
    ch = StdoutChannel()
    await ch.close()  # must not raise


# ── Human-readable text format (#77) ──────────────────────────────────


async def test_text_format_emits_readable_single_line(capsys) -> None:
    ch = StdoutChannel(format="text")
    await ch.notify(NotificationLevel.ERROR, "something went wrong")

    captured = capsys.readouterr()
    assert captured.err == ""
    line = captured.out.strip()
    assert line == "[iac-cartographer][ERROR] something went wrong"


async def test_text_format_renders_each_level_in_upper_case(capsys) -> None:
    ch = StdoutChannel(format="text")
    await ch.notify(NotificationLevel.INFO, "i")
    await ch.notify(NotificationLevel.WARN, "w")
    await ch.notify(NotificationLevel.ERROR, "e")

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        "[iac-cartographer][INFO] i",
        "[iac-cartographer][WARN] w",
        "[iac-cartographer][ERROR] e",
    ]


async def test_default_format_is_jsonl_when_unset(capsys) -> None:
    """Regression: omitting `format` must preserve the JSONL behaviour
    that existing log-aggregator pipelines rely on."""
    ch = StdoutChannel()
    await ch.notify(NotificationLevel.INFO, "still json")

    line = capsys.readouterr().out.strip()
    body = json.loads(line)  # raises if not valid JSON
    assert body["message"] == "still json"
    assert body["level"] == "info"


async def test_text_format_respects_stderr_stream(capsys) -> None:
    """`format` and `stream` are independent — text mode on stderr works."""
    ch = StdoutChannel(stream="stderr", format="text")
    await ch.notify(NotificationLevel.WARN, "on stderr")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "[iac-cartographer][WARN] on stderr"
