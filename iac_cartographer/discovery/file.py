"""File-based discovery — read a curated list of repos from disk.

Use case: environments where the operator already has a source of truth
for which repos to inventory (a docs index, a Backstage catalog, an
internal CMDB export, …) and doesn't want iac-cartographer to enumerate
the VCS host. Common patterns:

  * Air-gapped runs where VCS API access is unavailable.
  * Pinned subset of repos for a focused publish (e.g. "only platform
    team's repos this week").
  * Self-hosted VCS without an API this tool supports (Gitea, Forgejo,
    Codeberg, …) — operators bring the metadata themselves.

Input format (YAML, list of records):

```yaml
- host: github                                  # or "gitlab" / "bitbucket" / "other"
  full_name: acme/main-cluster
  clone_url: https://github.com/acme/main-cluster.git
  web_url: https://github.com/acme/main-cluster
  default_branch: main
  last_commit_sha: a1b2c3d4e5f6...
  last_commit_at: 2026-05-22T12:34:56Z
  last_commit_author: Alice                      # optional
```

The same shape as `RepoMetadata` — we just route the YAML through
`RepoMetadata.model_validate` so all the strictness gates apply.

JSON is also accepted (top-level array). Detection is by extension.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from iac_cartographer.constants import DiscoveryError
from iac_cartographer.discovery.base import DiscoverySource
from iac_cartographer.models import RepoMetadata

logger = logging.getLogger("iac_cartographer.discovery.file")


class FileDiscovery(DiscoverySource):
    """Load `RepoMetadata` records from a YAML / JSON file on disk."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self.name = f"file:{self._path.name}"

    async def discover(self) -> list[RepoMetadata]:
        # No network → no async work; the `async def` keeps the contract
        # uniform with the other sources so the orchestrator can gather
        # them together.
        if not self._path.exists():
            raise DiscoveryError(f"repos file not found: {self._path}")

        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DiscoveryError(f"could not read repos file {self._path}: {exc}") from exc

        try:
            parsed = _parse_payload(raw, self._path.suffix.lower())
        except (yaml.YAMLError, json.JSONDecodeError) as exc:
            raise DiscoveryError(f"repos file {self._path} is not valid YAML/JSON: {exc}") from exc

        if not isinstance(parsed, list):
            raise DiscoveryError(
                f"repos file {self._path} must contain a top-level list of repo records, got {type(parsed).__name__}"
            )

        out: list[RepoMetadata] = []
        for i, record in enumerate(parsed):
            if not isinstance(record, dict):
                raise DiscoveryError(
                    f"repos file {self._path}: entry {i} must be a mapping, got {type(record).__name__}"
                )
            try:
                out.append(RepoMetadata.model_validate(record))
            except Exception as exc:
                raise DiscoveryError(f"repos file {self._path}: entry {i} failed schema validation: {exc}") from exc

        logger.info("file: %s → %d repos", self._path, len(out))
        return out


def _parse_payload(raw: str, suffix: str) -> Any:
    if suffix == ".json":
        return json.loads(raw)
    # Default to YAML — handles `.yaml`, `.yml`, and anything else
    # (YAML is a superset of JSON, so a JSON file with a `.txt` extension
    # still parses correctly).
    return yaml.safe_load(raw)
