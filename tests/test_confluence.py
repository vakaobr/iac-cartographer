"""Phase 7 tests for iac_cartographer.confluence — find-or-create, upsert, 409 retry."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from iac_cartographer.confluence import ConfluenceClient, UpsertResult, _extract_next_cursor
from iac_cartographer.constants import ConfluenceError
from iac_cartographer.models import ConfluenceCredentials
from iac_cartographer.renderer import build_banner

SITE = "acme.atlassian.net"
BASE = f"https://{SITE}/wiki/api/v2"


def _client() -> ConfluenceClient:
    return ConfluenceClient(SITE, ConfluenceCredentials(email="bot@acme.example.com", api_token="ATATT"))


def _adf_with_banner(sha: str) -> dict[str, Any]:
    banner = build_banner(sha, datetime(2026, 5, 22, tzinfo=UTC), None)
    return {"type": "doc", "version": 1, "content": [banner]}


def _page_payload(page_id: str, title: str, version: int, body_sha: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if body_sha is not None:
        adf = _adf_with_banner(body_sha)
        body = {"atlas_doc_format": {"representation": "atlas_doc_format", "value": json.dumps(adf)}}
    return {
        "id": page_id,
        "title": title,
        "version": {"number": version},
        "body": body,
    }


# ─── _extract_next_cursor ───────────────────────────────────────────────


def test_extract_next_cursor_from_links_next() -> None:
    payload = {"_links": {"next": "/wiki/api/v2/pages?cursor=abc123&limit=100"}}
    headers = httpx.Headers()
    assert _extract_next_cursor(payload, headers) == "abc123"


def test_extract_next_cursor_returns_none_when_no_next() -> None:
    assert _extract_next_cursor({"_links": {}}, httpx.Headers()) is None
    assert _extract_next_cursor({}, httpx.Headers()) is None


def test_extract_next_cursor_from_link_header() -> None:
    headers = httpx.Headers({"Link": '<...?cursor=xyz999>; rel="next"'})
    assert _extract_next_cursor({}, headers) == "xyz999"


# ─── Auth header ────────────────────────────────────────────────────────


def test_auth_header_is_basic_base64() -> None:
    """Legacy (unscoped) Atlassian API tokens require HTTP Basic auth with
    `email:api_token` base64-encoded. Scoped tokens (the 2024+ "with scopes"
    variant) are OAuth-app-bound and were unworkable for this pipeline — see
    the module docstring for the diagnostic chain on 2026-05-25."""
    c = _client()
    expected = base64.b64encode(b"bot@acme.example.com:ATATT").decode("ascii")
    assert c._headers["Authorization"] == f"Basic {expected}"


# ─── get_space_id_by_key ───────────────────────────────────────────────


@respx.mock
async def test_get_space_id_by_key_caches() -> None:
    route = respx.get(f"{BASE}/spaces", params={"keys": "ENG", "limit": 1}).mock(
        return_value=httpx.Response(200, json={"results": [{"id": "12345", "key": "ENG"}]})
    )
    c = _client()
    async with c.session() as session:
        assert await c.get_space_id_by_key(session, "ENG") == "12345"
        # Second call — cached, no extra HTTP
        assert await c.get_space_id_by_key(session, "ENG") == "12345"
    assert route.call_count == 1


@respx.mock
async def test_get_space_id_by_key_not_found_raises() -> None:
    respx.get(f"{BASE}/spaces").mock(return_value=httpx.Response(200, json={"results": []}))
    c = _client()
    async with c.session() as session:
        with pytest.raises(ConfluenceError, match="not found"):
            await c.get_space_id_by_key(session, "Missing")


@respx.mock
async def test_get_space_id_by_key_401_raises_auth_error() -> None:
    respx.get(f"{BASE}/spaces").mock(return_value=httpx.Response(401, json={"message": "unauth"}))
    c = _client()
    async with c.session() as session:
        with pytest.raises(ConfluenceError, match="auth failed"):
            await c.get_space_id_by_key(session, "ENG")


# ─── find_child_by_title ───────────────────────────────────────────────


@respx.mock
async def test_find_child_by_title_returns_match() -> None:
    respx.get(f"{BASE}/pages/PARENT/children", params={"limit": 100}).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "P1", "title": "other"},
                    {"id": "P2", "title": "Terraform/IaC Inventory (auto-generated)"},
                ]
            },
        )
    )
    respx.get(f"{BASE}/pages/P2").mock(
        return_value=httpx.Response(
            200,
            json=_page_payload("P2", "Terraform/IaC Inventory (auto-generated)", version=3, body_sha="abc123"),
        )
    )
    c = _client()
    async with c.session() as session:
        page = await c.find_child_by_title(session, "PARENT", "Terraform/IaC Inventory (auto-generated)")
    assert page is not None
    assert page.id == "P2"
    assert page.version == 3
    assert page.body_adf is not None


@respx.mock
async def test_find_child_by_title_returns_none_when_absent() -> None:
    respx.get(f"{BASE}/pages/PARENT/children").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "P1", "title": "other"}]})
    )
    c = _client()
    async with c.session() as session:
        result = await c.find_child_by_title(session, "PARENT", "absent")
    assert result is None


@respx.mock
async def test_find_child_by_title_paginates() -> None:
    # Use side_effect so the two responses fire in order, regardless of params.
    # (Overlapping param-based mocks confused respx's matcher and caused an
    # infinite loop — the MAX_PAGES guard in find_child_by_title now backstops
    # that case, but this test only exercises happy-path pagination.)
    route = respx.get(f"{BASE}/pages/PARENT/children").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "results": [{"id": "P1", "title": "x"}],
                    "_links": {"next": "/wiki/api/v2/pages/PARENT/children?cursor=nx&limit=100"},
                },
            ),
            httpx.Response(200, json={"results": [{"id": "P2", "title": "found"}]}),
        ]
    )
    respx.get(f"{BASE}/pages/P2").mock(
        return_value=httpx.Response(200, json=_page_payload("P2", "found", version=1, body_sha="abc"))
    )
    c = _client()
    async with c.session() as session:
        result = await c.find_child_by_title(session, "PARENT", "found")
    assert result is not None
    assert result.id == "P2"
    assert route.call_count == 2  # both pages fetched


@respx.mock
async def test_find_child_by_title_terminates_on_repeated_cursor() -> None:
    """If the upstream API returns the same cursor twice in a row, we treat the
    walk as exhausted rather than spinning forever."""
    route = respx.get(f"{BASE}/pages/PARENT/children").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": "Px", "title": "other"}],
                "_links": {"next": "/wiki/api/v2/pages/PARENT/children?cursor=STUCK"},
            },
        )
    )
    c = _client()
    async with c.session() as session:
        result = await c.find_child_by_title(session, "PARENT", "never-matches")
    assert result is None
    # First call (no cursor) + one call with cursor=STUCK, then we detect the repeat.
    assert route.call_count == 2


# ─── get_page ──────────────────────────────────────────────────────────


@respx.mock
async def test_get_page_parses_adf_body() -> None:
    respx.get(f"{BASE}/pages/P1").mock(
        return_value=httpx.Response(200, json=_page_payload("P1", "T", version=5, body_sha="def"))
    )
    c = _client()
    async with c.session() as session:
        page = await c.get_page(session, "P1")
    assert page.id == "P1"
    assert page.title == "T"
    assert page.version == 5
    assert page.body_adf is not None
    assert page.body_adf["type"] == "doc"


@respx.mock
async def test_get_page_404_raises() -> None:
    respx.get(f"{BASE}/pages/missing").mock(return_value=httpx.Response(404, json={"message": "nope"}))
    c = _client()
    async with c.session() as session:
        with pytest.raises(ConfluenceError, match="not found"):
            await c.get_page(session, "missing")


@respx.mock
async def test_get_page_handles_unparseable_body() -> None:
    respx.get(f"{BASE}/pages/P1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "P1",
                "title": "T",
                "version": {"number": 1},
                "body": {"atlas_doc_format": {"value": "not json {{"}},
            },
        )
    )
    c = _client()
    async with c.session() as session:
        page = await c.get_page(session, "P1")
    assert page.body_adf is None  # gracefully degraded


# ─── upsert ────────────────────────────────────────────────────────────


@respx.mock
async def test_upsert_creates_when_missing() -> None:
    respx.get(f"{BASE}/pages/PARENT/children").mock(return_value=httpx.Response(200, json={"results": []}))
    respx.post(f"{BASE}/pages").mock(return_value=httpx.Response(200, json={"id": "NEW", "version": {"number": 1}}))
    c = _client()
    async with c.session() as session:
        result = await c.upsert(
            session,
            space_id="S1",
            parent_id="PARENT",
            title="new-page",
            adf_body=_adf_with_banner("abc"),
            current_sha="abc",
        )
    assert isinstance(result, UpsertResult)
    assert result.action == "created"
    assert result.page_id == "NEW"


@respx.mock
async def test_upsert_skips_when_sha_matches() -> None:
    respx.get(f"{BASE}/pages/PARENT/children").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "P1", "title": "the-page"}]})
    )
    respx.get(f"{BASE}/pages/P1").mock(
        return_value=httpx.Response(200, json=_page_payload("P1", "the-page", version=7, body_sha="abc"))
    )
    # No PUT route — if we tried to PUT, respx would raise unmocked
    c = _client()
    async with c.session() as session:
        result = await c.upsert(
            session,
            space_id="S1",
            parent_id="PARENT",
            title="the-page",
            adf_body=_adf_with_banner("abc"),
            current_sha="abc",
        )
    assert result.action == "unchanged"
    assert result.page_id == "P1"
    assert result.version == 7


@respx.mock
async def test_upsert_updates_when_sha_differs() -> None:
    respx.get(f"{BASE}/pages/PARENT/children").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "P1", "title": "the-page"}]})
    )
    respx.get(f"{BASE}/pages/P1").mock(
        return_value=httpx.Response(200, json=_page_payload("P1", "the-page", version=3, body_sha="OLD"))
    )
    respx.put(f"{BASE}/pages/P1").mock(return_value=httpx.Response(200))
    c = _client()
    async with c.session() as session:
        result = await c.upsert(
            session,
            space_id="S1",
            parent_id="PARENT",
            title="the-page",
            adf_body=_adf_with_banner("NEW"),
            current_sha="NEW",
        )
    assert result.action == "updated"
    assert result.version == 4  # incremented


@respx.mock
async def test_upsert_retries_once_on_409() -> None:
    respx.get(f"{BASE}/pages/PARENT/children").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "P1", "title": "x"}]})
    )
    # First get → version 3. After 409 we re-GET → version 4.
    get_route = respx.get(f"{BASE}/pages/P1").mock(
        side_effect=[
            httpx.Response(200, json=_page_payload("P1", "x", version=3, body_sha="OLD")),
            httpx.Response(200, json=_page_payload("P1", "x", version=4, body_sha="OLD2")),
        ]
    )
    put_route = respx.put(f"{BASE}/pages/P1").mock(side_effect=[httpx.Response(409), httpx.Response(200)])
    c = _client()
    async with c.session() as session:
        result = await c.upsert(
            session,
            space_id="S1",
            parent_id="PARENT",
            title="x",
            adf_body=_adf_with_banner("NEW"),
            current_sha="NEW",
        )
    assert result.action == "updated"
    assert result.version == 5  # second update incremented from 4
    assert put_route.call_count == 2
    assert get_route.call_count == 2


@respx.mock
async def test_upsert_persistent_409_raises() -> None:
    respx.get(f"{BASE}/pages/PARENT/children").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "P1", "title": "x"}]})
    )
    respx.get(f"{BASE}/pages/P1").mock(
        return_value=httpx.Response(200, json=_page_payload("P1", "x", version=3, body_sha="OLD"))
    )
    respx.put(f"{BASE}/pages/P1").mock(return_value=httpx.Response(409))
    c = _client()
    async with c.session() as session:
        with pytest.raises(ConfluenceError, match="persistent 409"):
            await c.upsert(
                session,
                space_id="S1",
                parent_id="PARENT",
                title="x",
                adf_body=_adf_with_banner("NEW"),
                current_sha="NEW",
            )


@respx.mock
async def test_upsert_raises_on_401_anywhere() -> None:
    respx.get(f"{BASE}/pages/PARENT/children").mock(return_value=httpx.Response(401))
    c = _client()
    async with c.session() as session:
        with pytest.raises(ConfluenceError, match="auth failed"):
            await c.upsert(
                session,
                space_id="S1",
                parent_id="PARENT",
                title="x",
                adf_body=_adf_with_banner("NEW"),
                current_sha="NEW",
            )


@respx.mock
async def test_upsert_create_propagates_failure() -> None:
    respx.get(f"{BASE}/pages/PARENT/children").mock(return_value=httpx.Response(200, json={"results": []}))
    respx.post(f"{BASE}/pages").mock(return_value=httpx.Response(500, json={"message": "boom"}))
    c = _client()
    async with c.session() as session:
        with pytest.raises(ConfluenceError, match="create-page failed"):
            await c.upsert(
                session,
                space_id="S1",
                parent_id="PARENT",
                title="x",
                adf_body=_adf_with_banner("a"),
                current_sha="a",
            )


# ─── upsert_existing_page ──────────────────────────────────────────────


@respx.mock
async def test_upsert_existing_page_updates_when_sha_differs() -> None:
    """The overview's parent-page path: hit the page by ID and PUT a new version."""
    respx.get(f"{BASE}/pages/PARENT").mock(
        return_value=httpx.Response(200, json=_page_payload("PARENT", "Overview", version=4, body_sha="OLD"))
    )
    respx.put(f"{BASE}/pages/PARENT").mock(return_value=httpx.Response(200))
    c = _client()
    async with c.session() as session:
        result = await c.upsert_existing_page(
            session,
            page_id="PARENT",
            title="Overview",
            adf_body=_adf_with_banner("NEW"),
            current_sha="NEW",
        )
    assert result.action == "updated"
    assert result.version == 5  # incremented


@respx.mock
async def test_upsert_existing_page_skips_when_sha_matches() -> None:
    """Banner-SHA short-circuit applies to the in-place parent path too."""
    respx.get(f"{BASE}/pages/PARENT").mock(
        return_value=httpx.Response(200, json=_page_payload("PARENT", "Overview", version=4, body_sha="abc"))
    )
    # No PUT mocked — if we tried to PUT, respx would raise unmocked.
    c = _client()
    async with c.session() as session:
        result = await c.upsert_existing_page(
            session,
            page_id="PARENT",
            title="Overview",
            adf_body=_adf_with_banner("abc"),
            current_sha="abc",
        )
    assert result.action == "unchanged"
    assert result.page_id == "PARENT"


@respx.mock
async def test_upsert_existing_page_404_raises() -> None:
    """If the parent page is gone, surface that — don't silently turn it into a create."""
    respx.get(f"{BASE}/pages/MISSING").mock(return_value=httpx.Response(404, json={"message": "gone"}))
    c = _client()
    async with c.session() as session:
        with pytest.raises(ConfluenceError, match="not found"):
            await c.upsert_existing_page(
                session,
                page_id="MISSING",
                title="Overview",
                adf_body=_adf_with_banner("a"),
                current_sha="a",
            )


@respx.mock
async def test_upsert_treats_no_banner_as_changed() -> None:
    """A page without a banner (e.g. manually seeded stub) must be updated."""
    respx.get(f"{BASE}/pages/PARENT/children").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "P1", "title": "x"}]})
    )
    respx.get(f"{BASE}/pages/P1").mock(
        return_value=httpx.Response(200, json=_page_payload("P1", "x", version=1, body_sha=None))
    )
    respx.put(f"{BASE}/pages/P1").mock(return_value=httpx.Response(200))
    c = _client()
    async with c.session() as session:
        result = await c.upsert(
            session,
            space_id="S1",
            parent_id="PARENT",
            title="x",
            adf_body=_adf_with_banner("abc"),
            current_sha="abc",
        )
    assert result.action == "updated"
