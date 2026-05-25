"""Env-var SecretsProvider.

Lookup rules:

  * `get_secret("iac-cartographer/confluence")` → env var
    `IAC_CARTOGRAPHER_SECRET_CONFLUENCE` (value is JSON-decoded).
  * `get_parameter("/iac-cartographer/confluence-parent-id")` → env var
    `IAC_CARTOGRAPHER_PARAM_CONFLUENCE_PARENT_ID` (value used as-is).

The transformation: strip a leading `iac-cartographer/` (or `/`) prefix,
uppercase, then replace any non-alphanumeric character with `_`. So
`iac-cartographer/gitlab` becomes `IAC_CARTOGRAPHER_SECRET_GITLAB`,
matching what an operator would expect to type by hand.

Optional `.env` autoload: pass `dotenv_path` to the constructor to load
`KEY=value` pairs from a file before reading env vars. Existing env-var
values take precedence over the file — same semantics as docker-compose
and most other dotenv-aware tools. No external dependency; the parser
is inline (handles quoted values, comments, blank lines).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from iac_cartographer.constants import CartographerError
from iac_cartographer.secrets.base import SecretsProvider

logger = logging.getLogger("iac_cartographer.secrets.env")

_SECRET_PREFIX = "IAC_CARTOGRAPHER_SECRET_"  # noqa: S105 — env var name, not a value
_PARAM_PREFIX = "IAC_CARTOGRAPHER_PARAM_"
# Strip the conventional `iac-cartographer/` prefix from logical names
# before mangling — keeps env var names readable.
_LOGICAL_PREFIX_RE = re.compile(r"^/?iac-cartographer/")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


class EnvSecretsError(CartographerError):
    """Raised when the env var for a requested secret / parameter is unset."""


class EnvSecretsProvider(SecretsProvider):
    """Reads secrets + parameters from process environment variables."""

    name = "env"

    def __init__(self, *, dotenv_path: str | Path | None = None) -> None:
        if dotenv_path is not None:
            _load_dotenv(Path(dotenv_path))

    def get_secret(self, name: str) -> dict[str, Any]:
        var = _SECRET_PREFIX + _mangle(name)
        raw = os.environ.get(var)
        if raw is None:
            raise EnvSecretsError(
                f"env secrets backend: required env var {var!r} is not set (maps from logical name {name!r})"
            )
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EnvSecretsError(f"env secrets backend: env var {var!r} is not valid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise EnvSecretsError(
                f"env secrets backend: env var {var!r} must decode to a JSON object, got {type(decoded).__name__}"
            )
        logger.info("env: fetched secret %s via %s (length=%d)", name, var, len(raw))
        return decoded

    def get_parameter(self, name: str) -> str:
        var = _PARAM_PREFIX + _mangle(name)
        raw = os.environ.get(var)
        if raw is None:
            raise EnvSecretsError(
                f"env secrets backend: required env var {var!r} is not set (maps from logical name {name!r})"
            )
        logger.info("env: fetched parameter %s via %s (length=%d)", name, var, len(raw))
        return raw


def _mangle(name: str) -> str:
    """Translate a logical name into the env-var suffix.

    `iac-cartographer/confluence`              → `CONFLUENCE`
    `/iac-cartographer/confluence-parent-id`   → `CONFLUENCE_PARENT_ID`
    `something-without-prefix`                 → `SOMETHING_WITHOUT_PREFIX`
    """
    stripped = _LOGICAL_PREFIX_RE.sub("", name)
    return _NON_ALNUM_RE.sub("_", stripped).strip("_").upper()


def _load_dotenv(path: Path) -> None:
    """Minimal `.env` parser — populates `os.environ` for missing keys.

    Supports `KEY=value`, `KEY="value"`, `KEY='value'`, blank lines, and
    `# comments`. Lines that don't match the `KEY=value` shape are
    silently skipped (no point failing the whole run for a stray line).

    Pre-existing env vars take precedence — same convention as
    docker-compose's `--env-file`."""
    if not path.exists():
        logger.warning("env: .env file %s not found; skipping autoload", path)
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("env: could not read .env file %s: %s", path, exc)
        return

    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip optional surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    logger.info("env: loaded %d new vars from %s", count, path)
