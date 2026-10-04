"""the typed core: the tools agents use every day, under their original names.

names and argument shapes match @dakatan/mcp-mattermost exactly (camelCase,
comma-separated id lists) so prompts written against the old server keep
working. three tools are new: file attachments on create_post, file
download (PRI-923), and whoami (PRI-549).
"""

import asyncio
from pathlib import Path
from typing import Annotated, Any, Optional

from fastmcp import FastMCP
from pydantic import Field

from .api import Mattermost, MattermostError, readable_post_list, readable_timestamps, split_identifiers
from .identity import SisterIdentity

DEFAULT_DOWNLOAD_DIRECTORY = Path.home() / ".cache/mcp-mattermost/files"
UNREAD_LIMIT_AFTER = 30

SEARCH_POSTS_DESCRIPTION = """Search posts by term with support for advanced search modifiers. Without any modifiers, performs a general search across all accessible content. Supported modifiers include:

- `from:username` - Find posts from specific users
- `in:channel` - Find posts in specific channels (by name or ID)
- `before:YYYY-MM-DD` - Find posts before a date
- `after:YYYY-MM-DD` - Find posts after a date
- `on:YYYY-MM-DD` - Find posts on a specific date
- `-term` - Exclude posts containing the term
- `"exact phrase"` - Search for exact phrases using quotes
- `term*` - Wildcard search (asterisk at end only)
- `#hashtag` - Search for hashtags

Modifiers can be combined. Example: `meeting in:town-square from:john after:2023-01-01`"""


def register_core_tools(mcp: FastMCP, mattermost: Mattermost, identity: SisterIdentity) -> None:
    @mcp.tool(name="mattermost_whoami")
    async def whoami() -> dict[str, Any]:
        """Who this server posts as: the sister this machine resolves to (from sttts voices.yaml) and the Mattermost account its token authenticates as. They are verified equal at startup."""
        return {
            "sister": identity.sister,
            "username": mattermost.me.get("username"),
            "user_id": mattermost.me.get("id"),
            "is_bot": mattermost.me.get("is_bot"),
            "team": mattermost.team_name,
            "team_id": mattermost.team_id,
            "host_id": identity.host_id,
        }

    @mcp.tool(name="mattermost_search_posts", description=SEARCH_POSTS_DESCRIPTION)
    async def search_posts(
        terms: Annotated[str, Field(description="Search term with optional modifiers. Without modifiers, performs a general search.")],
        page: Annotated[Optional[int], Field(description="Page number")] = None,
        perPage: Annotated[Optional[int], Field(description="Number of posts per page")] = None,
    ) -> dict[str, Any]:
        result = await mattermost.request(
            "POST",
            f"/api/v4/teams/{mattermost.team_id}/posts/search",
            json={"terms": terms, "is_or_search": False, "page": page or 0, "per_page": perPage or 100},
        )
        return readable_post_list(result)

    @mcp.tool(name="mattermost_get_posts")
    async def get_posts(
        postId: Annotated[str, Field(description="Comma splitted array of post ID")],
    ) -> list[dict[str, Any]]:
        """Get posts by post ID"""
        posts = await asyncio.gather(*(mattermost.request("GET", f"/api/v4/posts/{post_id}") for post_id in split_identifiers(postId)))
        return [readable_timestamps(post) for post in posts]

    @mcp.tool(name="mattermost_get_posts_unread")
    async def get_posts_unread(
        channelId: Annotated[str, Field(description="Channel ID to get unread posts from")],
    ) -> dict[str, Any]:
        """Get unread posts in a channel for the current user"""
        result = await mattermost.request(
            "GET",
            f"/api/v4/users/{mattermost.me['id']}/channels/{channelId}/posts/unread",
            params={"limit_after": UNREAD_LIMIT_AFTER, "limit_before": 0, "skipFetchThreads": "true"},
        )
        return readable_post_list(result)

    @mcp.tool(name="mattermost_create_post")
    async def create_post(
        channelId: Annotated[str, Field(description="Channel ID")],
        message: Annotated[str, Field(description="Message content")],
        rootId: Annotated[Optional[str], Field(description="Post ID to reply to")] = None,
        filePaths: Annotated[
            Optional[list[str]],
            Field(description="Absolute paths of local files to attach (up to 10). Each is uploaded to the channel first."),
        ] = None,
    ) -> dict[str, Any]:
        """Create a new post in a channel, optionally replying in a thread and attaching local files"""
        file_ids = await mattermost.upload_files(channelId, filePaths) if filePaths else []
        body: dict[str, Any] = {"channel_id": channelId, "message": message}
        if rootId:
            body["root_id"] = rootId
        if file_ids:
            body["file_ids"] = file_ids
        return readable_timestamps(await mattermost.request("POST", "/api/v4/posts", json=body))

    @mcp.tool(name="mattermost_download_file")
    async def download_file(
        fileId: Annotated[str, Field(description="File ID, as found in a post's file_ids or metadata.files")],
        directory: Annotated[Optional[str], Field(description="Directory to save under (default ~/.cache/mcp-mattermost/files)")] = None,
    ) -> dict[str, Any]:
        """Download a post attachment to local disk and return its path, so it can be read (images, pdfs, text) with local tools"""
        return await mattermost.download_file(fileId, Path(directory) if directory else DEFAULT_DOWNLOAD_DIRECTORY)

    @mcp.tool(name="mattermost_get_posts_thread")
    async def get_posts_thread(
        rootId: Annotated[str, Field(description="Post ID of the thread parent")],
        perPage: Annotated[Optional[int], Field(description="Number of posts per page")] = None,
        fromPost: Annotated[Optional[str], Field(description="Post ID to start from")] = None,
    ) -> dict[str, Any]:
        """Get all posts in a thread"""
        params: dict[str, Any] = {}
        if perPage:
            params["perPage"] = perPage
        if fromPost:
            params["fromPost"] = fromPost
        return readable_post_list(await mattermost.request("GET", f"/api/v4/posts/{rootId}/thread", params=params))

    @mcp.tool(name="mattermost_pin_post")
    async def pin_post(postId: Annotated[str, Field(description="Post ID to pin")]) -> Any:
        """Pin a post to a channel"""
        return await mattermost.request("POST", f"/api/v4/posts/{postId}/pin")

    @mcp.tool(name="mattermost_unpin_post")
    async def unpin_post(postId: Annotated[str, Field(description="Post ID to unpin")]) -> Any:
        """Unpin a post from a channel"""
        return await mattermost.request("POST", f"/api/v4/posts/{postId}/unpin")

    @mcp.tool(name="mattermost_get_pinned_posts")
    async def get_pinned_posts(
        channelId: Annotated[str, Field(description="Channel ID to get pinned posts from")],
    ) -> dict[str, Any]:
        """Get all pinned posts in a channel"""
        return readable_post_list(await mattermost.request("GET", f"/api/v4/channels/{channelId}/pinned"))

    @mcp.tool(name="mattermost_search_channels")
    async def search_channels(
        term: Annotated[str, Field(description="Search term")],
        page: Annotated[Optional[int], Field(description="Page number")] = None,
        perPage: Annotated[Optional[int], Field(description="Number of channels per page")] = None,
    ) -> Any:
        """Search channels by term"""
        result = await mattermost.request(
            "POST",
            "/api/v4/channels/search",
            params={"system_console": "false"},
            json={"term": term, "team_ids": [mattermost.team_id], "page": page or 0, "per_page": perPage or 100},
        )
        channels = result.get("channels", []) if isinstance(result, dict) else result
        return [readable_timestamps(channel) for channel in channels]

    @mcp.tool(name="mattermost_get_channels")
    async def get_channels(
        channelId: Annotated[
            Optional[str], Field(description="Comma splitted array of channel IDs, which channel ID or channel name is required")
        ] = None,
        name: Annotated[
            Optional[str], Field(description="Comma splitted array of channel names, which channel ID or channel name is required")
        ] = None,
    ) -> list[dict[str, Any]]:
        """Get channels by channel ID or name"""
        if channelId:
            paths = [f"/api/v4/channels/{channel_id}" for channel_id in split_identifiers(channelId)]
        elif name:
            paths = [f"/api/v4/teams/{mattermost.team_id}/channels/name/{channel}" for channel in split_identifiers(name)]
        else:
            raise MattermostError("Please provide channel ID or channel name")
        channels = await asyncio.gather(*(mattermost.request("GET", path) for path in paths))
        return [readable_timestamps(channel) for channel in channels]

    @mcp.tool(name="mattermost_get_my_channels")
    async def get_my_channels() -> list[dict[str, Any]]:
        """Get channels that the current user is a member of"""
        channels = await mattermost.request("GET", f"/api/v4/users/me/teams/{mattermost.team_id}/channels")
        return [readable_timestamps(channel) for channel in channels if channel.get("type") in ("O", "P")]

    @mcp.tool(name="mattermost_get_users")
    async def get_users(
        username: Annotated[
            Optional[str], Field(description="Comma splitted array of usernames, which username or user ID is required")
        ] = None,
        userId: Annotated[
            Optional[str], Field(description="Comma splitted array of user ID, which username or user ID is required")
        ] = None,
    ) -> list[dict[str, Any]]:
        """Get users by username or user ID"""
        if userId:
            paths = [f"/api/v4/users/{user_id}" for user_id in split_identifiers(userId)]
        elif username:
            paths = [f"/api/v4/users/username/{user}" for user in split_identifiers(username)]
        else:
            raise MattermostError("Please provide username or user ID")
        users = await asyncio.gather(*(mattermost.request("GET", path) for path in paths))
        return [readable_timestamps(user) for user in users]

    @mcp.tool(name="mattermost_search_users")
    async def search_users(term: Annotated[str, Field(description="Search term")]) -> list[dict[str, Any]]:
        """Search users by term"""
        users = await mattermost.request("POST", "/api/v4/users/search", json={"term": term, "team_id": mattermost.team_id})
        return [readable_timestamps(user) for user in users]

    @mcp.tool(name="mattermost_add_reaction")
    async def add_reaction(
        postId: Annotated[str, Field(description="Post ID to add the reaction to")],
        emojiName: Annotated[str, Field(description="Comma splitted of array of name of the emoji to use as reaction")],
    ) -> list[dict[str, Any]]:
        """Add a reaction (emoji) to a post"""
        reactions = await asyncio.gather(
            *(
                mattermost.request(
                    "POST",
                    "/api/v4/reactions",
                    json={"user_id": mattermost.me["id"], "post_id": postId, "emoji_name": emoji},
                )
                for emoji in split_identifiers(emojiName)
            )
        )
        return [readable_timestamps(reaction) for reaction in reactions]

    @mcp.tool(name="mattermost_remove_reaction")
    async def remove_reaction(
        postId: Annotated[str, Field(description="Post ID to remove the reaction from")],
        emojiName: Annotated[str, Field(description="Comma splitted array of name of the emoji reaction to remove")],
    ) -> list[Any]:
        """Remove a reaction (emoji) from a post"""
        return list(
            await asyncio.gather(
                *(
                    mattermost.request("DELETE", f"/api/v4/users/{mattermost.me['id']}/posts/{postId}/reactions/{emoji}")
                    for emoji in split_identifiers(emojiName)
                )
            )
        )

    @mcp.tool(name="mattermost_get_reactions")
    async def get_reactions(postId: Annotated[str, Field(description="Post ID to get reactions for")]) -> list[dict[str, Any]]:
        """Get all reactions for a post"""
        reactions = await mattermost.request("GET", f"/api/v4/posts/{postId}/reactions")
        return [readable_timestamps(reaction) for reaction in reactions or []]
