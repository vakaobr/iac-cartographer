"""Exception hierarchy for iac-cartographer.

All errors raised internally inherit from `CartographerError` so the CLI's
top-level handler can catch them with a single `except` and map to exit code 2
(known error). Anything else is exit code 3 (unhandled).

Naming convention: concrete subclasses deliberately omit the `-Error` suffix
where the bare name reads better in a traceback (e.g. `BedrockError: ...`
reads cleaner than `BedrockErrorError: ...`). Ruff N818 is silenced for this
module in `pyproject.toml`.
"""

from __future__ import annotations


class CartographerError(Exception):
    """Base class for all iac-cartographer errors."""


class ConfigError(CartographerError):
    """SSM parameter or local YAML config is missing / malformed."""


class MissingSecretError(CartographerError):
    """A required Secrets Manager entry was not found or could not be decoded."""


class DiscoveryError(CartographerError):
    """GitLab or GitHub repo discovery failed in a way that prevents progress."""


class CloneError(CartographerError):
    """`git clone --depth=1` failed for a specific repo."""


class ExtractionError(CartographerError):
    """`terraform-docs` invocation failed or returned malformed JSON."""


class BedrockError(CartographerError):
    """Bedrock invocation failed after retries (throttling, parse failure, etc.)."""


class RenderError(CartographerError):
    """ADF assembly failed (programmer error — should not happen in normal runs)."""


class ConfluenceError(CartographerError):
    """Confluence v2 API call failed (auth, persistent 409, network)."""
