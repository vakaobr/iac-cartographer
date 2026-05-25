"""Tests for the ConfluencePublisher adapter — focused on adapter wiring,
not the underlying ConfluenceClient HTTP behaviour (covered by test_confluence.py)."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from iac_cartographer.confluence import UpsertResult
from iac_cartographer.models import (
    BedrockNarrative,
    ConfluenceConfig,
    ProviderRef,
    RepoInventory,
    RepoMetadata,
    TerraformSummary,
)
from iac_cartographer.publishers.confluence import ConfluencePublisher


def _inventory(name: str = "acme/iac/main-cluster") -> RepoInventory:
    return RepoInventory(
        meta=RepoMetadata(
            host="gitlab",
            full_name=name,
            clone_url=f"https://x.test/{name}.git",
            web_url=f"https://x.test/{name}",
            default_branch="main",
            last_commit_sha="a" * 40,
            last_commit_at=datetime(2026, 5, 22, tzinfo=UTC),
        ),
        summary=TerraformSummary(
            providers=[ProviderRef(name="aws")],
            resources=[],
            resource_counts_by_type={},
        ),
        narrative=BedrockNarrative(purpose="Provides observability infrastructure for the cluster."),
    )


def _config() -> ConfluenceConfig:
    return ConfluenceConfig(
        site="acme.atlassian.net",
        space_key="DEVOPS",
        parent_page_id_ssm_path="/ignored/in/this/test",
    )


def _mock_client(*, space_id: str = "SPACE123") -> MagicMock:
    """Build a ConfluenceClient-shaped mock. The publisher only uses
    `session()` (async context manager), `get_space_id_by_key`, `upsert`,
    and `upsert_existing_page`."""
    client = MagicMock()
    session = MagicMock(name="httpx_session")

    @contextlib.asynccontextmanager
    async def _session_cm() -> Any:
        yield session

    client.session = lambda: _session_cm()
    client.get_space_id_by_key = AsyncMock(return_value=space_id)
    client.upsert = AsyncMock(return_value=UpsertResult(page_id="P1", action="created", version=1))
    client.upsert_existing_page = AsyncMock(
        return_value=UpsertResult(page_id="PARENT", action="updated", version=2),
    )
    return client


@pytest.mark.asyncio
async def test_publisher_resolves_space_id_once_on_enter() -> None:
    client = _mock_client(space_id="SPACE_X")
    pub = ConfluencePublisher(client, _config(), parent_page_id="PARENT")

    async with pub:
        pass

    client.get_space_id_by_key.assert_awaited_once_with(client.get_space_id_by_key.await_args.args[0], "DEVOPS")


@pytest.mark.asyncio
async def test_publish_child_passes_through_to_client_upsert() -> None:
    client = _mock_client()
    pub = ConfluencePublisher(client, _config(), parent_page_id="PARENT")
    inv = _inventory()

    async with pub:
        result = await pub.publish_child(
            inv,
            sha="abc12345",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )

    assert result.page_id == "P1"
    assert result.action == "created"
    client.upsert.assert_awaited_once()
    kwargs = client.upsert.await_args.kwargs
    assert kwargs["space_id"] == "SPACE123"
    assert kwargs["parent_id"] == "PARENT"
    assert kwargs["current_sha"] == "abc12345"
    assert kwargs["title"]  # build_child returned a title
    assert isinstance(kwargs["adf_body"], dict)


@pytest.mark.asyncio
async def test_publish_overview_updates_parent_in_place() -> None:
    client = _mock_client()
    pub = ConfluencePublisher(client, _config(), parent_page_id="PARENT")
    inv = _inventory()

    async with pub:
        result = await pub.publish_overview(
            [inv],
            {"acme/iac/main-cluster": "P1"},
            sha="deadbeef",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url="https://ci.test/job/9",
        )

    assert result.page_id == "PARENT"
    assert result.action == "updated"
    client.upsert_existing_page.assert_awaited_once()
    kwargs = client.upsert_existing_page.await_args.kwargs
    assert kwargs["page_id"] == "PARENT"
    assert kwargs["current_sha"] == "deadbeef"
    # `upsert` (the same-title-rejection path) must NOT be used for the overview.
    client.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_publish_child_without_aenter_raises() -> None:
    client = _mock_client()
    pub = ConfluencePublisher(client, _config(), parent_page_id="PARENT")

    with pytest.raises(AssertionError, match="async with"):
        await pub.publish_child(
            _inventory(),
            sha="abc12345",
            updated_at=datetime(2026, 5, 22, tzinfo=UTC),
            pipeline_url=None,
        )
