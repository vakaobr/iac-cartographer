"""Tests for FileDiscovery — YAML/JSON repo lists from disk."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from iac_cartographer.constants import DiscoveryError
from iac_cartographer.discovery import FileDiscovery

if TYPE_CHECKING:
    from pathlib import Path


_VALID_RECORD = {
    "host": "github",
    "full_name": "acme/main-cluster",
    "clone_url": "https://github.com/acme/main-cluster.git",
    "web_url": "https://github.com/acme/main-cluster",
    "default_branch": "main",
    "last_commit_sha": "a" * 40,
    "last_commit_at": "2026-05-22T12:34:56Z",
    "last_commit_author": "Alice",
}


@pytest.mark.asyncio
async def test_file_discovery_loads_yaml(tmp_path: Path) -> None:
    p = tmp_path / "repos.yaml"
    p.write_text(
        "- host: github\n"
        "  full_name: acme/main-cluster\n"
        "  clone_url: https://github.com/acme/main-cluster.git\n"
        "  web_url: https://github.com/acme/main-cluster\n"
        "  default_branch: main\n"
        "  last_commit_sha: " + "a" * 40 + "\n"
        "  last_commit_at: 2026-05-22T12:34:56Z\n",
        encoding="utf-8",
    )
    repos = await FileDiscovery(p).discover()
    assert len(repos) == 1
    assert repos[0].full_name == "acme/main-cluster"
    assert repos[0].host == "github"


@pytest.mark.asyncio
async def test_file_discovery_loads_json(tmp_path: Path) -> None:
    p = tmp_path / "repos.json"
    p.write_text(json.dumps([_VALID_RECORD]), encoding="utf-8")
    repos = await FileDiscovery(p).discover()
    assert len(repos) == 1
    assert repos[0].host == "github"


@pytest.mark.asyncio
async def test_file_discovery_supports_other_host(tmp_path: Path) -> None:
    p = tmp_path / "repos.yaml"
    record = {**_VALID_RECORD, "host": "other", "full_name": "self/hosted-repo"}
    p.write_text(json.dumps([record]), encoding="utf-8")
    repos = await FileDiscovery(p).discover()
    assert repos[0].host == "other"


@pytest.mark.asyncio
async def test_file_discovery_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="not found"):
        await FileDiscovery(tmp_path / "nope.yaml").discover()


@pytest.mark.asyncio
async def test_file_discovery_rejects_non_list_payload(tmp_path: Path) -> None:
    p = tmp_path / "repos.yaml"
    p.write_text("key: value\n", encoding="utf-8")
    with pytest.raises(DiscoveryError, match="top-level list"):
        await FileDiscovery(p).discover()


@pytest.mark.asyncio
async def test_file_discovery_rejects_invalid_record(tmp_path: Path) -> None:
    p = tmp_path / "repos.yaml"
    p.write_text("- host: github\n  full_name: missing-other-fields\n", encoding="utf-8")
    with pytest.raises(DiscoveryError, match="entry 0 failed schema validation"):
        await FileDiscovery(p).discover()


@pytest.mark.asyncio
async def test_file_discovery_rejects_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "repos.yaml"
    p.write_text(": this is :: : not valid yaml\n", encoding="utf-8")
    with pytest.raises(DiscoveryError, match="not valid YAML/JSON"):
        await FileDiscovery(p).discover()


@pytest.mark.asyncio
async def test_file_discovery_name_includes_filename(tmp_path: Path) -> None:
    p = tmp_path / "my-repos.yaml"
    p.write_text("[]", encoding="utf-8")
    # The orchestrator uses `source.name` in log lines; verify it's
    # specific enough to disambiguate multiple file sources.
    src = FileDiscovery(p)
    assert src.name == "file:my-repos.yaml"
