"""offline checks: the spec patches, identity resolution, and the delete guard.

nothing here touches the network; the live checks are in the README's
smoke test and the PRI-549 report."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from mcp_mattermost.api import MattermostError, post_priority, readable_timestamps
from mcp_mattermost.generic_tools import build_operation_server, rank_operations
from mcp_mattermost.identity import (
    IdentityError,
    SisterIdentity,
    assert_token_speaks_as_sister,
    resolve_identity,
    token_for_sister,
)
from mcp_mattermost.scheduling import epoch_milliseconds_from_iso, flatten_scheduled_posts, replacement_message, with_self_mention
from mcp_mattermost.spec import PATH_TEMPLATE_PARAMETER, SPEC_VERSION, index_operations, load_spec

BASE_URL = "https://mattermost.example.test"


@pytest.fixture(scope="module")
def spec():
    return load_spec(BASE_URL)


def test_every_operation_has_a_unique_id(spec):
    operations = index_operations(spec)
    # v11.7.14; bump alongside openapi.version when the spec is rebuilt
    assert SPEC_VERSION == "v11.7.14"
    assert len(operations) == 545
    assert "SearchFiles" in operations and "SearchFilesAcrossTeams" in operations
    # v11.5.1 shipped the content-flagging operations without ids (we derived
    # FlagAPost from its summary); v11.7.14 names them upstream
    assert "PostCFPostFlag" in operations


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


def test_every_path_template_parameter_is_declared(spec):
    undeclared = []
    for path, path_item in spec["paths"].items():
        for method in ("get", "post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            declared = {
                parameter["name"]
                for parameter in [*(path_item.get("parameters") or []), *(operation.get("parameters") or [])]
                if isinstance(parameter, dict) and parameter.get("in") == "path"
            }
            undeclared += [f"{method} {path} {{{name}}}" for name in PATH_TEMPLATE_PARAMETER.findall(path) if name not in declared]
    assert undeclared == []


async def test_scheduled_post_list_takes_its_team_id(spec):
    # the 2026-11-02 bug: the url went out with a literal {team_id}
    server = build_operation_server(spec, httpx.AsyncClient(base_url=BASE_URL))
    tool = await server.get_tool("GetUserScheduledPosts")
    assert "team_id" in tool.to_mcp_tool().inputSchema["required"]


async def test_scheduled_post_update_accepts_create_at(spec):
    server = build_operation_server(spec, httpx.AsyncClient(base_url=BASE_URL))
    tool = await server.get_tool("UpdateScheduledPost")
    assert "create_at" in tool.to_mcp_tool().inputSchema["properties"]


NOW = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)


def test_schedule_time_needs_an_offset_and_a_future():
    assert epoch_milliseconds_from_iso("2026-11-09T09:00:00-08:00", NOW) == 1794243600000
    assert epoch_milliseconds_from_iso("2026-11-09T17:00:00Z", NOW) == 1794243600000
    with pytest.raises(MattermostError, match="no UTC offset"):
        epoch_milliseconds_from_iso("2026-11-09T09:00:00", NOW)
    with pytest.raises(MattermostError, match="in the past"):
        epoch_milliseconds_from_iso("2026-11-01T09:00:00-08:00", NOW)
    with pytest.raises(MattermostError, match="not an ISO 8601"):
        epoch_milliseconds_from_iso("next tuesday", NOW)


def test_wake_mentions_the_author_once():
    assert with_self_mention("check CNPG", "lily") == "@lily check CNPG"
    assert with_self_mention("@Lily check CNPG", "lily") == "@Lily check CNPG"


def test_updating_a_wake_up_keeps_it_a_wake_up():
    assert replacement_message("@lily check CNPG", "check etcd", "lily", None) == "@lily check etcd"
    assert replacement_message("plain note", "new note", "lily", None) == "new note"
    assert replacement_message("@lily check CNPG", "just a note", "lily", False) == "just a note"
    assert replacement_message("plain note", "check etcd", "lily", True) == "@lily check etcd"


def test_priority_always_carries_all_three_keys():
    # ScheduledPost.ToPost refuses a priority map missing any key, at send time
    assert post_priority(None, False, False) is None
    assert post_priority("urgent", True, True) == {"priority": "urgent", "requested_ack": True, "persistent_notifications": True}
    assert post_priority(None, True, False) == {"priority": "", "requested_ack": True, "persistent_notifications": False}
    with pytest.raises(MattermostError, match="requires priority 'urgent'"):
        post_priority("important", False, True)


def test_scheduled_posts_flatten_soonest_first_with_readable_times():
    response = {"team-id": [{"id": "b", "scheduled_at": 2000}], "directChannels": [{"id": "a", "scheduled_at": 1000, "processed_at": 0}]}
    flattened = flatten_scheduled_posts(response)
    assert [post["id"] for post in flattened] == ["a", "b"]
    readable = readable_timestamps(flattened[0])
    assert readable["scheduled_at"].startswith("1970-01-01T00:00:01") and readable["processed_at"] is None


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


def test_schedule_result_reminds_to_link_each_named_issue():
    from mcp_mattermost.scheduling import dossier_reminder

    reminder = dossier_reminder("uahcx8o38pgqpptt11a6cbr8my", "@lily PRI-322 re-measure (PRI-1203 watch, PRI-322)")
    assert 'value="uahcx8o38pgqpptt11a6cbr8my"' in reminder
    assert "names PRI-322, PRI-1203;" in reminder
    assert "several issues" in reminder


def test_schedule_result_without_an_issue_points_at_the_current_one():
    from mcp_mattermost.scheduling import dossier_reminder

    assert "the issue this session is working on" in dossier_reminder("x" * 26, "@lily say hi")


def test_schedule_result_offers_monitoring_only_for_merged_work():
    from mcp_mattermost.scheduling import dossier_reminder

    reminder = dossier_reminder("x" * 26, "@lily verify PRI-1 rollout")
    assert 'status="monitoring"' in reminder
    assert "every PR for the issue is merged" in reminder
    assert "still unmerged, it is not monitoring" in reminder
