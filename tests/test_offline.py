"""offline checks: the spec patches, identity resolution, and the delete guard.

nothing here touches the network; the live checks are in the README's
smoke test and the PRI-549 report."""

from pathlib import Path

import httpx
import pytest

from mcp_mattermost.generic_tools import build_operation_server, rank_operations
from mcp_mattermost.identity import (
    IdentityError,
    SisterIdentity,
    assert_token_speaks_as_sister,
    resolve_identity,
    token_for_sister,
)
from mcp_mattermost.spec import index_operations, load_spec

BASE_URL = "https://mattermost.example.test"


@pytest.fixture(scope="module")
def spec():
    return load_spec(BASE_URL)


def test_every_operation_has_a_unique_id(spec):
    operations = index_operations(spec)
    assert len(operations) == 528
    assert "SearchFiles" in operations and "SearchFilesAcrossTeams" in operations
    assert "FlagAPost" in operations


def test_misindented_media_type_is_repaired(spec):
    response = spec["paths"]["/api/v4/users/{user_id}/custom_profile_attributes"]["get"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"]["type"] == "array"


async def test_fastmcp_builds_a_tool_for_every_operation(spec):
    server = build_operation_server(spec, httpx.AsyncClient(base_url=BASE_URL))
    tool_names = {tool.name for tool in await server.list_tools()}
    assert set(index_operations(spec)) <= tool_names


def test_find_ranks_the_obvious_operation_first(spec):
    operations = index_operations(spec)
    assert rank_operations(operations, "patch post")[0].operation_id == "PatchPost"
    assert rank_operations(operations, "") == []


def write_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "voices.yaml"
    manifest.write_text("machines:\n  HOST-ROSE: rose\n  HOST-LILY: lily\n")
    return manifest


def test_identity_resolves_host_to_sister_to_token(tmp_path):
    environment = {
        "MCP_MATTERMOST_VOICES_MANIFEST": str(write_manifest(tmp_path)),
        "MCP_MATTERMOST_HOST_ID": "HOST-LILY",
        "MCP_MATTERMOST_TOKEN_ROSE": "rose-token",
        "MCP_MATTERMOST_TOKEN_LILY": "lily-token",
    }
    identity = resolve_identity(environment)
    assert (identity.sister, identity.token) == ("lily", "lily-token")


def test_unknown_host_fails_closed(tmp_path):
    environment = {"MCP_MATTERMOST_VOICES_MANIFEST": str(write_manifest(tmp_path)), "MCP_MATTERMOST_HOST_ID": "HOST-VIOLET"}
    with pytest.raises(IdentityError):
        resolve_identity(environment)


def test_empty_token_fails_closed():
    # opencode turns an unset {env:VAR} into "", so empty is the realistic broken shape
    with pytest.raises(IdentityError):
        token_for_sister("rose", {"MCP_MATTERMOST_TOKEN_ROSE": "  "})


def test_token_for_the_wrong_sister_is_refused():
    with pytest.raises(IdentityError):
        assert_token_speaks_as_sister(SisterIdentity("lily", "HOST-LILY", "t"), "rose")
    assert_token_speaks_as_sister(SisterIdentity("rose", "HOST-ROSE", "t"), "rose")
