"""full api parity in three tools: find an operation, describe it, call it.

exposing all 528 spec operations as tools would cost ~238k tokens of tool
definitions on every session (measured 2026-10-04, PRI-549 report), and
opencode loads every mcp tool schema eagerly. so FastMCP.from_openapi still
builds every operation, but into a private server that is never exposed;
mattermost_call dispatches into it by operationId. describe returns the
exact input schema that call validates against, so the two can never drift.

DELETE operations are refused unless confirmDelete is true (peter,
2026-10-04): the rose and lily bots can delete posts and leave channels,
and before this server those operations were only unreachable by not
existing.
"""

import json
import re
from typing import Annotated, Any, Optional

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType
from fastmcp.utilities.openapi.models import HTTPRoute
from pydantic import Field

from .spec import Operation

FIND_RESULT_LIMIT = 15
DESCRIPTION_PREVIEW_CHARACTERS = 1500


def build_operation_server(spec: dict[str, Any], client: httpx.AsyncClient) -> FastMCP:
    """every operation as a tool on a private server. output validation is off:
    the spec's response schemas are wishful in places, and a validation error
    on a successful call would hide a real result from the agent."""

    def every_route_is_a_tool(route: HTTPRoute, mcp_type: MCPType) -> MCPType:
        return MCPType.TOOL

    return FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="mattermost-operations",
        route_map_fn=every_route_is_a_tool,
        validate_output=False,
    )


def rank_operations(operations: dict[str, Operation], query: str) -> list[Operation]:
    """plain keyword scoring: id and summary words weigh most, then tags and
    path, then description. good enough for 528 well-named operations, and
    predictable, which matters more than clever here."""
    words = [word for word in re.findall(r"[a-z0-9]+", query.lower()) if word]
    if not words:
        return []

    def score(operation: Operation) -> int:
        identifier_words = " ".join(re.findall(r"[A-Z]?[a-z0-9]+", operation.operation_id)).lower()
        heavy = f"{identifier_words} {operation.summary.lower()}"
        medium = f"{' '.join(operation.tags).lower()} {operation.path.lower()}"
        light = operation.description.lower()
        total = 0
        for word in words:
            total += 5 * (word in heavy) + 2 * (word in medium) + (word in light)
        matched_every_word = all(word in f"{heavy} {medium} {light}" for word in words)
        return total + (10 if matched_every_word else 0)

    scored = [(score(operation), operation) for operation in operations.values()]
    return [operation for points, operation in sorted(scored, key=lambda pair: -pair[0]) if points > 0]


async def register_generic_tools(mcp: FastMCP, spec: dict[str, Any], operations: dict[str, Operation], client: httpx.AsyncClient) -> None:
    operation_server = build_operation_server(spec, client)
    internal_tool_names = {tool.name for tool in await operation_server.list_tools()}
    unreachable = sorted(set(operations) - internal_tool_names)
    if unreachable:
        raise RuntimeError(f"operations with no internal tool (fastmcp renamed or dropped them): {unreachable[:10]}")

    @mcp.tool(name="mattermost_find_operation")
    async def find_operation(
        query: Annotated[
            str,
            Field(description="Keywords describing what you want to do, e.g. 'edit post', 'channel members', 'scheduled post', 'bookmark'"),
        ],
        limit: Annotated[Optional[int], Field(description=f"Max results (default {FIND_RESULT_LIMIT})")] = None,
    ) -> list[dict[str, Any]]:
        """Search every Mattermost REST v4 operation (528, the server's full API at v11.5.1) for anything the other mattermost_* tools do not cover. Returns operationIds to pass to mattermost_describe_operation and mattermost_call."""
        ranked = rank_operations(operations, query)[: limit or FIND_RESULT_LIMIT]
        return [
            {
                "operationId": operation.operation_id,
                "method": operation.method,
                "path": operation.path,
                "summary": operation.summary,
                "tags": list(operation.tags),
            }
            for operation in ranked
        ]

    @mcp.tool(name="mattermost_describe_operation")
    async def describe_operation(
        operationId: Annotated[str, Field(description="An operationId from mattermost_find_operation")],
    ) -> dict[str, Any]:
        """The exact arguments mattermost_call accepts for one operation (path, query, and body parameters flattened into one object), plus its method, path, and documentation."""
        operation = _require_operation(operations, operationId)
        tool = await operation_server.get_tool(operationId)
        return {
            "operationId": operation.operation_id,
            "method": operation.method,
            "path": operation.path,
            "summary": operation.summary,
            "description": operation.description[:DESCRIPTION_PREVIEW_CHARACTERS],
            "arguments_schema": tool.to_mcp_tool().inputSchema,
            "requires_confirm_delete": operation.method == "DELETE",
        }

    @mcp.tool(name="mattermost_call")
    async def call(
        operationId: Annotated[str, Field(description="An operationId from mattermost_find_operation")],
        arguments: Annotated[
            Optional[dict[str, Any]],
            Field(
                description="Arguments matching mattermost_describe_operation's arguments_schema (path, query, and body fields in one flat object)"
            ),
        ] = None,
        confirmDelete: Annotated[bool, Field(description="Required true for DELETE operations; they are refused otherwise")] = False,
    ) -> Any:
        """Call any Mattermost REST v4 operation by operationId. Use mattermost_describe_operation first to learn its arguments. DELETE operations require confirmDelete=true."""
        operation = _require_operation(operations, operationId)
        if operation.method == "DELETE" and not confirmDelete:
            raise ValueError(f"{operationId} is {operation.method} {operation.path}; refused without confirmDelete=true")
        result = await operation_server.call_tool(operationId, arguments or {})
        if result.structured_content is not None:
            return result.structured_content
        texts = [block.text for block in result.content if getattr(block, "text", None) is not None]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return joined


def _require_operation(operations: dict[str, Operation], operation_id: str) -> Operation:
    operation = operations.get(operation_id)
    if operation is None:
        raise ValueError(f"unknown operationId '{operation_id}'; search with mattermost_find_operation")
    return operation
