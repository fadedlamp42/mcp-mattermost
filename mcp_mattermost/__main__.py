"""entry point: establish identity, verify it against the server, then serve.

environment:
    MCP_MATTERMOST_URL              server origin (required)
    MCP_MATTERMOST_TEAM_NAME        team the core tools act in (required)
    MCP_MATTERMOST_TOKEN_<SISTER>   one per sister, e.g. MCP_MATTERMOST_TOKEN_ROSE
    MCP_MATTERMOST_VOICES_MANIFEST  override path to sttts voices.yaml (optional)

every startup check fails closed: the process exits nonzero with the reason
on stderr, opencode logs it, and the session simply has no mattermost tools.
"""

import asyncio
import os
import sys

import httpx
from fastmcp import FastMCP

from .api import Mattermost
from .core_tools import register_core_tools
from .generic_tools import register_generic_tools
from .identity import IdentityError, assert_token_speaks_as_sister, resolve_identity
from .spec import index_operations, load_spec

INSTRUCTIONS = (
    "Mattermost for the prism-dynamics team. The mattermost_* tools cover daily work "
    "(posts, threads, attachments, reactions, channels, users). For anything else in the "
    "Mattermost API, use mattermost_find_operation -> mattermost_describe_operation -> mattermost_call."
)


def required_environment(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise IdentityError(f"{name} is required")
    return value


async def build_server() -> FastMCP:
    identity = resolve_identity()
    base_url = required_environment("MCP_MATTERMOST_URL").rstrip("/")
    team_name = required_environment("MCP_MATTERMOST_TEAM_NAME")

    client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {identity.token}"},
        timeout=60.0,
    )
    mattermost = Mattermost(client=client, team_name=team_name)
    await mattermost.initialize()
    assert_token_speaks_as_sister(identity, mattermost.me["username"])

    spec = load_spec(base_url)
    operations = index_operations(spec)

    mcp = FastMCP(name="Mattermost", instructions=INSTRUCTIONS)
    register_core_tools(mcp, mattermost, identity)
    await register_generic_tools(mcp, spec, operations, client)
    print(
        f"mcp-mattermost: speaking as {identity.sister} ({len(operations)} operations reachable)",
        file=sys.stderr,
    )
    return mcp


async def main() -> None:
    try:
        mcp = await build_server()
    except (IdentityError, httpx.HTTPError, RuntimeError) as error:
        print(f"mcp-mattermost: refusing to start: {error}", file=sys.stderr)
        sys.exit(1)
    await mcp.run_stdio_async(show_banner=False)


if __name__ == "__main__":
    asyncio.run(main())
